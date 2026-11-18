"""
What the bike (and the Flow app's component list) can read about this add-on
when it connects for live data: a standard Bluetooth Device Information
service, served by BlueZ on the add-on's behalf. Read-only.

Without it the Flow app lists the add-on as "Accessory" with every field
blank. Which Flow field shows which characteristic isn't documented, so each
holds a distinct, recognisable value (see VALUES below) to map them by
looking.
"""
from __future__ import annotations

import logging

import dbus
import dbus.service

log = logging.getLogger(__name__)

GATT_MANAGER = "org.bluez.GattManager1"
SERVICE_IFACE = "org.bluez.GattService1"
CHAR_IFACE = "org.bluez.GattCharacteristic1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
APP_PATH = "/org/bosch_ebike/info"
DEVICE_INFORMATION = "0000180a-0000-1000-8000-00805f9b34fb"


DEFAULTS = {"manufacturer": "Home Assistant", "model": "BHA2026", "hardware": "HA-4200-PI"}


def values(version: str | None, adapter_address: str | None,
           identity: dict | None = None) -> list[tuple[str, str, str | bytes]]:
    """(UUID, label, value) for each Device Information characteristic.

    `identity` (from the options) may set manufacturer, model, hardware and
    serial; an empty serial means "HA-" + the end of the Pi's Bluetooth
    address. Firmware and software are always the add-on's version.
    """
    ident = {**DEFAULTS, **{k: v for k, v in (identity or {}).items() if v}}
    tail = (adapter_address or "").replace(":", "")[-6:].upper() or "000000"
    v = version or "dev"
    # Seen in the Flow app (2026-11-18): model -> "Part number", serial ->
    # "Serial number", firmware or software -> "Software version"; manufacturer
    # and hardware aren't shown; "Product code" came from none of these.
    # EXPERIMENT (0.7.6): the remaining standard fields carry markers so the
    # one that feeds "Product code" shows itself, and firmware / software
    # differ by ".0" to tell which feeds "Software version".
    return [
        ("00002a29-0000-1000-8000-00805f9b34fb", "manufacturer", ident["manufacturer"][:40]),
        ("00002a24-0000-1000-8000-00805f9b34fb", "model", ident["model"][:40]),
        ("00002a25-0000-1000-8000-00805f9b34fb", "serial", (ident.get("serial") or f"HA-{tail}")[:40]),
        ("00002a27-0000-1000-8000-00805f9b34fb", "hardware", ident["hardware"][:40]),
        ("00002a26-0000-1000-8000-00805f9b34fb", "firmware", v),
        ("00002a28-0000-1000-8000-00805f9b34fb", "software", f"{v}.0"),
        ("00002a23-0000-1000-8000-00805f9b34fb", "system id", "SYSID-B1"),
        ("00002a2a-0000-1000-8000-00805f9b34fb", "ieee", "IEEE-C1"),
        ("00002a50-0000-1000-8000-00805f9b34fb", "pnp id", bytes.fromhex("010a0b0c0d0e0f")),
    ]


class _Characteristic(dbus.service.Object):
    def __init__(self, bus, path: str, service_path: str, uuid: str, value) -> None:
        super().__init__(bus, path)
        self.path = path
        self._value = value if isinstance(value, bytes) else value.encode()
        self.props = {CHAR_IFACE: {"Service": dbus.ObjectPath(service_path),
                                   "UUID": uuid, "Flags": dbus.Array(["read"], signature="s")}}

    @dbus.service.method(PROPS_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        return self.props.get(interface, {})

    @dbus.service.method(CHAR_IFACE, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        offset = int(options.get("offset", 0))
        return dbus.Array(self._value[offset:], signature="y")


class _Service(dbus.service.Object):
    def __init__(self, bus, path: str, chars: list[str]) -> None:
        super().__init__(bus, path)
        self.path = path
        self.props = {SERVICE_IFACE: {"UUID": DEVICE_INFORMATION, "Primary": True,
                                      "Characteristics": dbus.Array(
                                          [dbus.ObjectPath(c) for c in chars], signature="o")}}

    @dbus.service.method(PROPS_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        return self.props.get(interface, {})


class Application(dbus.service.Object):
    """The GATT application BlueZ serves: one Device Information service."""

    def __init__(self, bus, version: str | None, adapter_address: str | None,
                 identity: dict | None = None) -> None:
        super().__init__(bus, APP_PATH)
        service_path = f"{APP_PATH}/service0"
        self.objects = []
        char_paths = []
        for i, (uuid, _label, value) in enumerate(values(version, adapter_address, identity)):
            path = f"{service_path}/char{i}"
            self.objects.append(_Characteristic(bus, path, service_path, uuid, value))
            char_paths.append(path)
        self.objects.insert(0, _Service(bus, service_path, char_paths))

    @dbus.service.method(OM_IFACE, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        return {dbus.ObjectPath(o.path): o.props for o in self.objects}

    def remove(self) -> None:
        for o in self.objects:
            try:
                o.remove_from_connection()
            except Exception:
                pass
        try:
            self.remove_from_connection()
        except Exception:
            pass


def register(bus, adapter_path: str, version: str | None, adapter_address: str | None,
             old: Application | None = None, identity: dict | None = None) -> Application:
    """Serve the Device Information service (asynchronously; failures only log)."""
    if old is not None:
        old.remove()
    app = Application(bus, version, adapter_address, identity)
    dbus.Interface(bus.get_object("org.bluez", adapter_path), GATT_MANAGER).RegisterApplication(
        dbus.ObjectPath(APP_PATH), {},
        reply_handler=lambda: log.info("Device information served to the bike: %s",
                                       ", ".join(f"{label} {value.hex() if isinstance(value, bytes) else value}"
                                                 for _u, label, value
                                                 in values(version, adapter_address, identity))),
        error_handler=lambda e: log.warning("Device information not served: %s", e))
    return app
