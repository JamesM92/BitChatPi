"""
BLE GATT client (central role) — scans for BitChat peers and connects.

BitChat is a symmetric mesh: every node both advertises (peripheral) AND
scans/connects (central). This module handles the central side: discovering
peers advertising the BitChat service UUID, connecting, and subscribing to
their characteristic notifications so we receive their outgoing packets.

On Linux/BlueZ, bleak's backend uses dbus-fast internally; it shares the
same asyncio event loop and coexists with our dbus_fast GattServer.
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Callable, Optional, Set

from bleak import BleakScanner, BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

log = logging.getLogger(__name__)

WriteCallback = Callable[[bytes, "Optional[str]"], None]


class BleScanner:
    """
    Scans for BitChat peripherals, connects, and subscribes to their
    characteristic notifications. All received payloads are forwarded to
    on_receive — same callback used by GattServer.WriteValue so the rest of
    the stack is agnostic about which BLE path a packet arrived on.
    """

    def __init__(
        self,
        service_uuid: str,
        char_uuid: str,
        on_receive: WriteCallback,
        on_peer_disconnected: "Optional[Callable[[str], None]]" = None,
        get_peripheral_addrs: "Optional[Callable[[], Set[str]]]" = None,
    ) -> None:
        self._service_uuid = service_uuid.lower()
        self._char_uuid    = char_uuid.lower()
        self._on_receive   = on_receive
        self._on_peer_disconnected = on_peer_disconnected
        self._get_peripheral_addrs = get_peripheral_addrs
        self._clients: dict[str, BleakClient] = {}  # address → client
        self._connecting: set[str] = set()
        self._last_seen: dict[str, BLEDevice] = {}   # address → device for reconnect
        self._connected_at: dict[str, float] = {}    # address → time connection established
        self._fail_count: dict[str, int] = {}        # consecutive rapid-disconnect count
        self._skip_until: dict[str, float] = {}      # address → backoff expiry timestamp
        self._scanner: BleakScanner | None = None
        self._running = False
        self._paused_until: float = 0.0              # suppress new connections until this time

    _RAPID_DISCONNECT_SECS = 5.0   # connection shorter than this counts as a failure
    _MAX_BACKOFF_SECS       = 300  # 5 minutes ceiling

    async def start(self) -> None:
        self._running = True
        self._scanner = BleakScanner(
            detection_callback=self._on_device_detected,
            service_uuids=[self._service_uuid],
        )
        await self._scanner.start()
        log.info("BLE scanner started  service=%s", self._service_uuid)

    async def stop(self) -> None:
        self._running = False
        self._last_seen.clear()
        self._skip_until.clear()
        if self._scanner:
            await self._scanner.stop()
            self._scanner = None
        for client in list(self._clients.values()):
            try:
                await client.disconnect()
            except Exception:
                pass
        self._clients.clear()

    def _on_device_detected(self, device: BLEDevice, adv: AdvertisementData) -> None:
        addr = device.address
        self._last_seen[addr] = device
        if addr in self._clients or addr in self._connecting:
            return
        if time.monotonic() < self._paused_until:
            return  # scanner paused while receiving peripheral fragment writes
        skip = self._skip_until.get(addr, 0)
        if skip and time.monotonic() < skip:
            return
        if self._get_peripheral_addrs and addr.upper() in self._get_peripheral_addrs():
            return  # device is already writing to us as peripheral — skip central connection
        asyncio.get_running_loop().create_task(self._connect(device))

    async def _connect(self, device: BLEDevice) -> None:
        addr = device.address
        if addr in self._clients or addr in self._connecting:
            return
        if time.monotonic() < self._paused_until:
            return  # scanner paused while receiving peripheral fragment writes
        self._connecting.add(addr)
        client: BleakClient | None = None
        try:
            client = BleakClient(
                device,
                disconnected_callback=lambda c: self._on_disconnected(c.address),
            )
            await client.connect()
            log.info("CENTRAL connected to %s", addr)

            # Enumerate services to catch stale-cache failures and diagnose
            # characteristic UUID mismatches. client.services is populated by
            # connect() in bleak 3.x (get_services() was removed).
            services = client.services  # BleakGATTServiceCollection
            bitchat_svc = services.get_service(self._service_uuid)
            if bitchat_svc is None:
                svc_uuids = [s.uuid for s in services]
                raise RuntimeError(
                    f"BitChat service {self._service_uuid} missing — "
                    f"found: {svc_uuids or '(none)'}"
                )
            char_uuids = [c.uuid for c in bitchat_svc.characteristics]
            if self._char_uuid not in char_uuids:
                raise RuntimeError(
                    f"BitChat char {self._char_uuid} missing from service — "
                    f"chars found: {char_uuids}"
                )

            def _notify(sender: int, data: bytearray) -> None:
                self._on_receive(bytes(data), None)

            await client.start_notify(self._char_uuid, _notify)
            self._clients[addr] = client
            self._connected_at[addr] = time.monotonic()
            self._fail_count.pop(addr, None)   # reset on successful subscribe
            log.info("CENTRAL subscribed to notifications from %s", addr)
        except Exception as e:
            count = self._fail_count.get(addr, 0) + 1
            self._fail_count[addr] = count
            backoff = min(self._MAX_BACKOFF_SECS, 10 * (3 ** (count - 1)))
            self._skip_until[addr] = time.monotonic() + backoff
            # "service missing — found: [0x1800, 0x1801]" is expected for iOS
            # background mode; downgrade to INFO so it doesn't flood WARNING logs.
            msg = str(e)
            is_background_ios = (
                "missing — found:" in msg and
                all(u in msg for u in ("00001800", "00001801")) and
                self._service_uuid not in msg.split("found:")[-1]
            )
            lvl = logging.INFO if is_background_ios else logging.WARNING
            log.log(lvl, "CENTRAL connect/subscribe failed #%d for %s (backoff %.0fs): %s",
                    count, addr, backoff, e)
            # On repeated failures, remove the device from BlueZ's GATT cache so the
            # next attempt gets a fresh service discovery instead of stale entries.
            if count >= 2:
                asyncio.ensure_future(self._remove_bluez_device(addr))
            # Explicitly disconnect to release the stale BlueZ GATT handle so the
            # next attempt gets a fresh service discovery.  The _on_disconnected
            # callback checks whether addr was ever added to self._clients; since
            # it wasn't (subscription failed), it skips the rapid-disconnect backoff
            # escalation and just honours the backoff we already set above.
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        finally:
            self._connecting.discard(addr)

    @staticmethod
    async def _remove_bluez_device(addr: str) -> None:
        """Remove a device from the BlueZ GATT cache to force fresh discovery."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl", "remove", addr,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            log.info("BlueZ device cache cleared for %s", addr)
        except Exception as e:
            log.debug("bluetoothctl remove %s failed: %s", addr, e)

    def _on_disconnected(self, addr: str) -> None:
        was_established = addr in self._clients
        self._clients.pop(addr, None)

        connected_at = self._connected_at.pop(addr, None)
        duration = time.monotonic() - connected_at if connected_at is not None else 0.0

        if not was_established:
            # Disconnect from a failed _connect attempt (start_notify never succeeded).
            # Backoff was already set in the except block — just schedule the retry.
            pass
        elif duration < self._RAPID_DISCONNECT_SECS:
            count = self._fail_count.get(addr, 0) + 1
            self._fail_count[addr] = count
            # Exponential backoff: 10s, 30s, 90s, 300s, 300s, ...
            backoff = min(self._MAX_BACKOFF_SECS, 10 * (3 ** (count - 1)))
            self._skip_until[addr] = time.monotonic() + backoff
            log.debug("CENTRAL rapid disconnect #%d from %s — backoff %.0fs",
                      count, addr, backoff)
        else:
            self._fail_count.pop(addr, None)
            self._skip_until.pop(addr, None)
            log.info("CENTRAL peer disconnected: %s (after %.0fs)", addr, duration)

        if self._on_peer_disconnected is not None:
            try:
                self._on_peer_disconnected(addr)
            except Exception:
                pass

        # Reconnect if still running, device known, and not in backoff.
        device = self._last_seen.get(addr)
        if device and self._running:
            skip = self._skip_until.get(addr, 0)
            delay = max(10.0, skip - time.monotonic()) if skip else 10.0
            try:
                loop = asyncio.get_running_loop()
                loop.call_later(delay, lambda: loop.create_task(self._connect(device)))
            except RuntimeError:
                pass

    def disconnect_all_central(self) -> None:
        """
        Immediately release all outbound central connections and suppress new
        ones for 2 minutes.

        Called synchronously when a large peripheral fragment set starts.
        Physical BLE teardown is fire-and-forget (background tasks); this method
        returns in O(1) so it never blocks the event loop.

        _paused_until is always updated (even when _clients is already empty)
        so that back-to-back fragment transfers don't let the pause expire and
        allow the scanner to reconnect mid-transfer — which causes BLE connection
        parameter renegotiation that disrupts the in-progress peripheral writes.
        """
        # Always refresh the pause window, even with no active central clients.
        self._paused_until = time.monotonic() + 120
        if not self._clients:
            return
        clients = dict(self._clients)
        self._clients.clear()
        log.info("CENTRAL released %d connection(s) — peripheral fragment write in progress",
                 len(clients))
        loop = asyncio.get_running_loop()
        for client in clients.values():
            loop.create_task(self._bg_disconnect(client))

    async def _bg_disconnect(self, client: BleakClient) -> None:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=30.0)
        except Exception:
            pass

    async def send(self, data: bytes) -> None:
        """Write data to all peripherals we are connected to as central."""
        skip = self._get_peripheral_addrs() if self._get_peripheral_addrs else set()
        for addr, client in list(self._clients.items()):
            if addr.upper() in skip:
                continue  # avoid dual-connection RF contention
            try:
                await client.write_gatt_char(self._char_uuid, data, response=False)
            except Exception as e:
                log.warning("CENTRAL write to %s failed: %s", addr, e)
                err = str(e)
                # D-Bus UnknownObject means the GATT handle is stale and the
                # disconnect callback will never fire — force cleanup now.
                if "UnknownObject" in err or "org.bluez.Error" in err:
                    self._clients.pop(addr, None)
                    asyncio.get_running_loop().create_task(self._bg_disconnect(client))

    @property
    def connected_count(self) -> int:
        return len(self._clients)
