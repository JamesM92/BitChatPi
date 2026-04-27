"""
BlueZ LE advertisement registration via LEAdvertisingManager1 D-Bus API.

Key lessons from btmon debugging:
  - BlueZ reads advertisement properties via @dbus_property, not a custom GetAll shim.
  - Without correct properties, BlueZ sends ADV_NONCONN_IND with zero-length data.
  - Type must be "peripheral" for connectable ADV_IND advertising.
"""
from __future__ import annotations
import asyncio
import logging

from dbus_fast.aio import MessageBus
from dbus_fast import BusType
from dbus_fast.service import ServiceInterface, method as dbus_method
from dbus_fast.service import dbus_property
from dbus_fast.constants import PropertyAccess

log = logging.getLogger(__name__)

_BLUEZ_BUS   = "org.bluez"
_ADAPTER_OBJ = "/org/bluez/hci0"
_ADV_PATH    = "/org/bitchatpi/advertisement0"
_ADV_MGR     = "org.bluez.LEAdvertisingManager1"


class _LEAdvertisement(ServiceInterface):
    """
    BlueZ LEAdvertisement1 implementation.
    Properties MUST use @dbus_property — BlueZ ignores GetAll shims.
    """

    def __init__(self, service_uuid: str, local_name: str) -> None:
        super().__init__("org.bluez.LEAdvertisement1")
        self._type         = "peripheral"   # connectable ADV_IND
        self._service_uuids = [service_uuid]
        self._local_name   = local_name
        self._includes     = ["tx-power"]

    @dbus_method()
    def Release(self):
        log.debug("LEAdvertisement released by BlueZ")

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":
        return self._type

    @dbus_property(access=PropertyAccess.READ)
    def ServiceUUIDs(self) -> "as":
        return self._service_uuids

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":
        return self._local_name

    @dbus_property(access=PropertyAccess.READ)
    def Includes(self) -> "as":
        return self._includes

    @dbus_property(access=PropertyAccess.READ)
    def MinInterval(self) -> "u":
        return 150  # ms — BlueZ default is 1280ms which is too slow for peer discovery

    @dbus_property(access=PropertyAccess.READ)
    def MaxInterval(self) -> "u":
        return 150


async def start_le_advertisement(
    service_uuid: str,
    local_name: str,
    adapter_path: str = _ADAPTER_OBJ,
) -> "MessageBus":
    """
    Register a connectable LE advertisement that includes service_uuid in the
    scan data (ADV_IND). Returns the MessageBus — keep it alive while advertising.
    Call stop_le_advertisement(bus) to clean up.
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    adv = _LEAdvertisement(service_uuid, local_name)
    bus.export(_ADV_PATH, adv)

    intro = await bus.introspect(_BLUEZ_BUS, adapter_path)
    proxy = bus.get_proxy_object(_BLUEZ_BUS, adapter_path, intro)
    mgr   = proxy.get_interface(_ADV_MGR)

    # Retry up to 3 times as a safety net for transient BlueZ delays.
    # Persistent slot conflicts are cleared by the adapter power cycle in daemon.py.
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            await mgr.call_register_advertisement(_ADV_PATH, {})
            log.info("LE advertisement registered: name=%s  service=%s",
                     local_name, service_uuid)
            return bus
        except Exception as exc:
            last_exc = exc
            log.warning("Advertisement registration failed (attempt %d/3): %s — retrying in 2 s",
                        attempt + 1, exc)
            await asyncio.sleep(2)

    bus.disconnect()
    raise RuntimeError(f"Could not register advertisement after 3 attempts: {last_exc}")


async def stop_le_advertisement(bus: "MessageBus", adapter_path: str = _ADAPTER_OBJ) -> None:
    try:
        intro = await bus.introspect(_BLUEZ_BUS, adapter_path)
        proxy = bus.get_proxy_object(_BLUEZ_BUS, adapter_path, intro)
        mgr   = proxy.get_interface(_ADV_MGR)
        await mgr.call_unregister_advertisement(_ADV_PATH)
        log.info("LE advertisement unregistered")
    except Exception as e:
        log.debug("Unregister: %s", e)
    finally:
        bus.disconnect()
