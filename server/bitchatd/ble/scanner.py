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
from typing import Callable, Optional

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
    ) -> None:
        self._service_uuid = service_uuid.lower()
        self._char_uuid    = char_uuid.lower()
        self._on_receive   = on_receive
        self._clients: dict[str, BleakClient] = {}  # address → client
        self._connecting: set[str] = set()
        self._scanner: BleakScanner | None = None
        self._running = False

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
        if addr in self._clients or addr in self._connecting:
            return
        loop = asyncio.get_event_loop()
        loop.create_task(self._connect(device))

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
            log.info("CENTRAL subscribed to notifications from %s", addr)
        except Exception as e:
            log.warning("CENTRAL connect/subscribe %s failed: %s", addr, e)
        finally:
            self._connecting.discard(addr)

    def _on_disconnected(self, addr: str) -> None:
        self._clients.pop(addr, None)
        log.info("CENTRAL peer disconnected: %s", addr)

    @property
    def connected_count(self) -> int:
        return len(self._clients)
