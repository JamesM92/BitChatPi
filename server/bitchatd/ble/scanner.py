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
        self._connecting.add(addr)
        try:
            client = BleakClient(
                device,
                disconnected_callback=lambda c: self._on_disconnected(c.address),
            )
            await client.connect()
            log.info("CENTRAL connected to %s", addr)

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
            log.debug("CENTRAL connect failed #%d for %s (backoff %.0fs): %s",
                      count, addr, backoff, e)
        finally:
            self._connecting.discard(addr)

    def _on_disconnected(self, addr: str) -> None:
        self._clients.pop(addr, None)

        connected_at = self._connected_at.pop(addr, None)
        duration = time.monotonic() - connected_at if connected_at is not None else 0.0

        if duration < self._RAPID_DISCONNECT_SECS:
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

    async def disconnect_all_central(self) -> None:
        """Release all outbound central connections (called when peripheral fragment write starts)."""
        addrs = list(self._clients.keys())
        for addr in addrs:
            client = self._clients.pop(addr, None)
            if client is None:
                continue
            try:
                await client.disconnect()
            except Exception:
                pass
        if addrs:
            log.info("CENTRAL released %d connection(s) — peripheral fragment write in progress", len(addrs))

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

    @property
    def connected_count(self) -> int:
        return len(self._clients)
