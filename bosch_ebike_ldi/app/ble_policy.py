"""
Pure decision helpers for running the bridge on a SHARED Bluetooth adapter.

The upstream bridge (possm/ha-bosch-ebike-pibridge) assumes a dedicated Pi,
where every BLE device on the adapter is an eBike. On Home Assistant OS the
same adapter also serves HA's own Bluetooth integration, which connects to
thermometers, locks, plugs, ... These helpers keep the bridge's hands off
those devices.

Everything here is plain Python with no D-Bus import, so it is unit tested
on any machine.
"""
from __future__ import annotations

import re

# Bosch LDI GATT service (spec V1.0). The bike exposes it; the bridge solicits it.
LDI_SERVICE_UUID = "0000eb20-eaa2-11e9-81b4-2a2ae2dbcce4"

# BlueZ device object paths look like /org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF
_DEVICE_PATH = re.compile(r"/dev_((?:[0-9A-Fa-f]{2}_){5}[0-9A-Fa-f]{2})$")

# Device1 properties that matter to the bridge. Everything else (RSSI,
# ManufacturerData, ServiceData ...) changes many times a second while HA
# scans, and must be ignored cheaply.
RELEVANT_DEVICE_PROPS = frozenset({"Connected", "ServicesResolved"})


def address_from_path(path: str) -> str | None:
    """Return 'AA:BB:CC:DD:EE:FF' for a BlueZ device path, or None.

    Reading the address from the path avoids a blocking D-Bus round trip for
    every PropertiesChanged signal.
    """
    match = _DEVICE_PATH.search(str(path))
    if not match:
        return None
    return match.group(1).replace("_", ":").upper()


def is_relevant_change(changed: dict) -> bool:
    """True if a Device1 PropertiesChanged signal is worth looking at."""
    return any(key in changed for key in RELEVANT_DEVICE_PROPS)


def advertises_ldi(uuids) -> bool:
    """True if a device's UUID list contains the Bosch LDI service.

    BlueZ caches the remote GATT service UUIDs of bonded devices in
    Device1.UUIDs, so a bike bonded before a restart can be recognised
    without connecting to it.
    """
    return any(str(u).lower() == LDI_SERVICE_UUID for u in (uuids or []))


def should_accept_pairing(address: str | None, pairing_open: bool,
                          known_bikes: set[str]) -> bool:
    """Decide whether the Just-Works agent may accept a pairing request.

    The upstream agent accepts everything. On a shared HA host that would let
    any nearby device bond with the server, so only accept:
      * while the user-opened pairing window is running, or
      * re-pairing from a bike we already know.
    """
    if pairing_open:
        return True
    return address is not None and address.upper() in known_bikes


BOSCH_COMPANY_ID = 0x02A6


def parse_bosch_advert(payload: bytes) -> dict:
    """Decode the manufacturer data of a Bosch smart-system advertisement.

    `payload` excludes the company ID (BlueZ already splits it off). Known
    layout of the 6-byte form: [2] component-ID version, [3] component ID,
    [4] data-format version, [5] pairable when equal to 1. Bytes 0-1 have no
    known meaning. Anything else is kept as raw hex.
    """
    info = {"raw": payload.hex(), "length": len(payload)}
    if len(payload) >= 6:
        info.update(component_id_version=payload[2], component_id=payload[3],
                    data_format_version=payload[4], pairable=payload[5] == 1)
    return info


def adapter_path_for(adapter_paths: list[str], wanted: str) -> str | None:
    """Pick the BlueZ adapter object path to use.

    `wanted` is an adapter name such as 'hci0', or 'auto' for the first one.
    """
    paths = sorted(str(p) for p in adapter_paths)
    if not paths:
        return None
    if wanted in ("", "auto"):
        return paths[0]
    for path in paths:
        if path.rsplit("/", 1)[-1] == wanted:
            return path
    return None
