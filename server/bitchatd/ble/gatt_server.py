"""
Pure dbus-fast GATT server for BlueZ — replaces bless for reliable notifications.

BlueZ GATT server flow:
  1. Export ObjectManager at APP_PATH, GattService1 at SVC_PATH, GattCharacteristic1 at CHAR_PATH.
  2. Call GattManager1.RegisterApplication(APP_PATH, {}) so BlueZ discovers them via GetManagedObjects.
  3. To send a notification: set self._value and call emit_properties_changed({"Value": new_bytes}).
     BlueZ monitors PropertiesChanged on registered char paths and forwards as BLE notifications
     to subscribed centrals.

Key lesson: BlueZ only sends BLE notifications if a central has already called StartNotify()
(i.e. written 0x0100 to the CCCD). Android BitChat does this during connection setup.
"""
from __future__ import annotations
import logging
from typing import Callable, Optional, TYPE_CHECKING

import asyncio

from dbus_fast.aio import MessageBus
from dbus_fast import BusType, Variant
from dbus_fast.service import ServiceInterface, method as dbus_method
from dbus_fast.service import dbus_property
from dbus_fast.constants import PropertyAccess

log = logging.getLogger(__name__)

_BLUEZ_BUS    = "org.bluez"
_ADAPTER_OBJ  = "/org/bluez/hci0"
_GATT_MGR_IF  = "org.bluez.GattManager1"

_APP_PATH  = "/org/bitchat/gatt"
_SVC_PATH  = "/org/bitchat/gatt/service0"
_CHAR_PATH = "/org/bitchat/gatt/service0/char0"

# device_path is the D-Bus object path of the writing device, e.g.
# /org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF — None when called from scanner notifications
WriteCallback = Callable[[bytes, "Optional[str]"], None]


class _GattApplication(ServiceInterface):
    """org.freedesktop.DBus.ObjectManager — lets BlueZ discover services via GetManagedObjects."""

    def __init__(self, service_uuid: str, char_uuid: str) -> None:
        super().__init__("org.freedesktop.DBus.ObjectManager")
        self._service_uuid = service_uuid
        self._char_uuid    = char_uuid

    @dbus_method(name="GetManagedObjects")
    def get_managed_objects(self) -> "a{oa{sa{sv}}}":
        return {
            _SVC_PATH: {
                "org.bluez.GattService1": {
                    "UUID":    Variant("s", self._service_uuid),
                    "Primary": Variant("b", True),
                }
            },
            _CHAR_PATH: {
                "org.bluez.GattCharacteristic1": {
                    "UUID":    Variant("s", self._char_uuid),
                    "Service": Variant("o", _SVC_PATH),
                    "Flags":   Variant("as", [
                        "read",
                        "write",
                        "write-without-response",
                        "notify",
                    ]),
                }
            },
        }


class _GattService(ServiceInterface):
    """org.bluez.GattService1"""

    def __init__(self, uuid: str) -> None:
        super().__init__("org.bluez.GattService1")
        self._uuid = uuid

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> "b":
        return True


class _GattCharacteristic(ServiceInterface):
    """
    org.bluez.GattCharacteristic1

    Maintains a _notifying flag set by BlueZ via StartNotify/StopNotify.
    Call notify(data) to push a value to subscribed centrals.
    """

    def __init__(self, uuid: str, service_path: str, on_write: WriteCallback,
                 on_start_notify: "Optional[Callable[[], None]]" = None) -> None:
        super().__init__("org.bluez.GattCharacteristic1")
        self._uuid     = uuid
        self._svc_path = service_path
        self._value    = bytes()
        self._notifying: bool = False
        self._on_write = on_write
        self._on_start_notify = on_start_notify
        self.connected_device_paths: set[str] = set()  # D-Bus paths seen via WriteValue

    # ── D-Bus properties ──────────────────────────────────────────────────────

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":
        return self._svc_path

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        return ["read", "write", "write-without-response", "notify"]

    @dbus_property(access=PropertyAccess.READ)
    def Notifying(self) -> "b":
        return self._notifying

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":
        return self._value

    # ── D-Bus methods ─────────────────────────────────────────────────────────

    @dbus_method()
    def ReadValue(self, options: "a{sv}") -> "ay":
        return self._value

    @dbus_method()
    def WriteValue(self, value: "ay", options: "a{sv}"):
        data = bytes(value)
        device_path: Optional[str] = None
        dev_variant = options.get("device")
        if dev_variant is not None:
            device_path = dev_variant.value
            self.connected_device_paths.add(device_path)
        log.debug("WriteValue %d bytes  device=%s  opts=%s",
                  len(data), device_path, list(options.keys()))
        try:
            self._on_write(data, device_path)
        except Exception:
            log.exception("on_write raised")

    @dbus_method()
    def StartNotify(self):
        self._notifying = True
        log.info("StartNotify — central subscribed to notifications")
        if self._on_start_notify is not None:
            try:
                self._on_start_notify()
            except Exception:
                log.exception("on_start_notify raised")

    @dbus_method()
    def StopNotify(self):
        self._notifying = False
        log.info("StopNotify — central unsubscribed")

    # ── Notification helper ───────────────────────────────────────────────────

    def notify(self, data: bytes) -> None:
        """Push data to all subscribed centrals via BlueZ PropertiesChanged."""
        self._value = data
        self.emit_properties_changed({"Value": data})
        log.debug("PropertiesChanged emitted %d bytes  notifying=%s", len(data), self._notifying)


class GattServer:
    """
    Thin wrapper: start() registers the GATT app with BlueZ.
    Call send(data) to push bytes to connected centrals.
    """

    def __init__(self) -> None:
        self._bus:  Optional[MessageBus]       = None
        self._char: Optional[_GattCharacteristic] = None

    async def start(
        self,
        service_uuid:    str,
        char_uuid:       str,
        on_write:        WriteCallback,
        on_start_notify: "Optional[Callable[[], None]]" = None,
        adapter_path:    str = _ADAPTER_OBJ,
    ) -> None:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self._bus = bus

        app  = _GattApplication(service_uuid, char_uuid)
        svc  = _GattService(service_uuid)
        char = _GattCharacteristic(char_uuid, _SVC_PATH, on_write, on_start_notify)
        self._char = char

        bus.export(_APP_PATH,  app)
        bus.export(_SVC_PATH,  svc)
        bus.export(_CHAR_PATH, char)

        intro = await bus.introspect(_BLUEZ_BUS, adapter_path)
        proxy = bus.get_proxy_object(_BLUEZ_BUS, adapter_path, intro)
        mgr   = proxy.get_interface(_GATT_MGR_IF)

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                await mgr.call_register_application(_APP_PATH, {})
                log.info("GATT application registered  service=%s  char=%s",
                         service_uuid, char_uuid)
                return
            except Exception as exc:
                last_exc = exc
                log.warning("GATT registration failed (attempt %d/3): %s — retrying in 2 s",
                            attempt + 1, exc)
                await asyncio.sleep(2)

        bus.disconnect()
        raise RuntimeError(f"Could not register GATT application after 3 attempts: {last_exc}")

    @property
    def notifying(self) -> bool:
        """True if at least one central has subscribed to notifications."""
        return self._char is not None and self._char._notifying

    def send(self, data: bytes) -> None:
        """Push raw bytes to subscribed centrals."""
        if self._char is None:
            log.warning("send() called before start()")
            return
        self._char.notify(data)

    @property
    def connected_device_paths(self) -> set[str]:
        """D-Bus device paths seen via WriteValue — usable for reverse BleakClient connects."""
        if self._char is None:
            return set()
        return self._char.connected_device_paths

    @property
    def connected_addresses(self) -> set[str]:
        """MAC addresses (AA:BB:CC:DD:EE:FF) derived from connected_device_paths."""
        result: set[str] = set()
        for path in self.connected_device_paths:
            parts = path.rsplit("/dev_", 1)
            if len(parts) == 2:
                result.add(parts[1].replace("_", ":").upper())
        return result

    def remove_device(self, address: str) -> None:
        """Remove a device from tracked paths when it disconnects (address like AA:BB:CC:DD:EE:FF)."""
        if self._char is None:
            return
        path = "/org/bluez/hci0/dev_" + address.upper().replace(":", "_")
        self._char.connected_device_paths.discard(path)

    async def disconnect_all_connected(self) -> None:
        """Disconnect all devices that have written to the characteristic.

        Called by the subscription watchdog when a device is actively writing
        but has not subscribed to notifications — indicating stale iOS CCCD
        cache.  Forcing a disconnect causes the phone to reconnect fresh and
        re-subscribe.
        """
        if self._bus is None:
            return
        paths = set(self.connected_device_paths)
        for device_path in paths:
            try:
                intro = await self._bus.introspect(_BLUEZ_BUS, device_path)
                proxy = self._bus.get_proxy_object(_BLUEZ_BUS, device_path, intro)
                dev   = proxy.get_interface("org.bluez.Device1")
                await dev.call_disconnect()
                log.info("Disconnected %s to force GATT re-subscription", device_path)
            except Exception as e:
                log.debug("disconnect %s: %s", device_path, e)

    async def stop(self, adapter_path: str = _ADAPTER_OBJ) -> None:
        if self._bus is None:
            return
        try:
            intro = await self._bus.introspect(_BLUEZ_BUS, adapter_path)
            proxy = self._bus.get_proxy_object(_BLUEZ_BUS, adapter_path, intro)
            mgr   = proxy.get_interface(_GATT_MGR_IF)
            await mgr.call_unregister_application(_APP_PATH)
        except Exception as e:
            log.debug("UnregisterApplication: %s", e)
        finally:
            self._bus.disconnect()
            self._bus = None
