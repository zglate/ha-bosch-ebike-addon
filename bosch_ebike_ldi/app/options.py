"""
Translate Home Assistant add-on options into the bridge's config dict.

The Supervisor writes the user's options to /data/options.json. MQTT
credentials come from the Mosquitto add-on via bashio (run.sh exports them as
environment variables), unless the user typed their own broker in the options.

The returned dict has the same shape as the upstream config.yaml, so
bridge.py stays close to upstream.
"""
from __future__ import annotations

import json
from typing import Mapping

DEFAULTS = {
    "bridge_name": "Home Assistant Reader",
    "adapter": "hci0",
    "private_advertising": True,
    "bikes": [],
    "log_level": "info",
    "battery_wh": 800,    # editable in the add-on options; per-bike override
    # Exploration (v1 reads and logs everything it safely can).
    "mcsp": True,          # MCSP session when the bike exposes it
    "mcsp_connect": True,  # connect to the advertising bike as a central
    "mcsp_sweep": True,    # read every readable datapoint after start-up
    "gatt_listen": True,   # listen on every notifying characteristic
    "raw_entities": False,  # one hidden HA entity per decoded datapoint (debugging)
    "capture": True,       # debug capture + report in /share/bosch_ebike
    "open_pairing_on_start": False,   # pairing only via the "Start pairing" button
    "mcsp_reconnect_hours": 6,        # after releasing the bike, wait this long
    # Firmware notifications: compare with Bosch's published versions daily.
    "firmware_check": True,
    "notify_full": True,                # notification when the battery is full
    # What the add-on tells the bike about itself (the Flow app's component
    # list). Empty serial = "HA-" + the end of the Pi's Bluetooth address.
    "accessory_manufacturer": "Home Assistant",
    "accessory_model": "BHA2026",
    "accessory_hardware": "HA-4200-PI",
    "accessory_serial": "",
    "firmware_page": "https://www.bosch-ebike.com/ca/service/software-update",
}
EXPLORER_OPTIONS = ("mcsp", "mcsp_connect", "mcsp_sweep", "gatt_listen",
                    "raw_entities", "capture", "open_pairing_on_start", "firmware_check",
                    "notify_full")


class ConfigError(ValueError):
    """Raised when the options cannot produce a working configuration."""


class MqttMissing(ConfigError):
    """No MQTT broker yet: neither in the options nor registered by Mosquitto."""


def build_config(options: Mapping, env: Mapping) -> dict:
    """Merge add-on options and the discovered MQTT service into one config."""
    opts = {**DEFAULTS, **{k: v for k, v in options.items() if v is not None}}

    # A broker typed in the options wins over the auto-discovered one.
    if opts.get("mqtt_host"):
        mqtt = {
            "broker": opts["mqtt_host"],
            "port": int(opts.get("mqtt_port") or 1883),
            "username": opts.get("mqtt_username") or None,
            "password": opts.get("mqtt_password") or None,
        }
    elif env.get("MQTT_HOST"):
        mqtt = {
            "broker": env["MQTT_HOST"],
            "port": int(env.get("MQTT_PORT") or 1883),
            "username": env.get("MQTT_USERNAME") or None,
            "password": env.get("MQTT_PASSWORD") or None,
        }
    else:
        raise MqttMissing(
            "No MQTT broker found. Install and start the Mosquitto broker "
            "add-on, or set mqtt_host in this add-on's options."
        )
    mqtt["base_topic"] = "bosch_ebike"

    battery_wh = _battery_wh(opts["battery_wh"])
    if battery_wh is None:
        raise ConfigError("battery_wh is required: the battery's nominal Wh, e.g. 800.")

    bikes = []
    for bike in opts["bikes"] or []:
        address = str(bike.get("address", "")).strip().upper()
        if not address:
            continue
        bikes.append({"address": address, "name": bike.get("name") or None,
                      "battery_wh": _battery_wh(bike.get("battery_wh"))})

    return {
        "bridge_name": opts["bridge_name"],
        "adapter": opts["adapter"],
        "private_advertising": bool(opts["private_advertising"]),
        "log_level": str(opts["log_level"]).lower(),
        "battery_wh": battery_wh,
        "version": env.get("ADDON_VERSION") or None,
        "bikes": [_drop_empty_name(b) for b in bikes],
        "mqtt": mqtt,
        **{name: bool(opts[name]) for name in EXPLORER_OPTIONS},
        "mcsp_reconnect_hours": max(0.0, float(opts["mcsp_reconnect_hours"])),
        "firmware_page": str(opts["firmware_page"]),
        "accessory": {"manufacturer": str(opts["accessory_manufacturer"] or "").strip(),
                      "model": str(opts["accessory_model"] or "").strip(),
                      "hardware": str(opts["accessory_hardware"] or "").strip(),
                      "serial": str(opts["accessory_serial"] or "").strip()},
    }


def _drop_empty_name(bike: dict) -> dict:
    """Upstream auto-names a bike when 'name' is absent, so omit empty values."""
    return {k: v for k, v in bike.items() if v is not None}


def _battery_wh(value) -> int | None:
    """Nominal battery energy in Wh, or None when left blank (per-bike)."""
    if value in (None, ""):
        return None
    wh = int(value)
    if wh <= 0:
        raise ConfigError("battery_wh must be the battery's nominal Wh, e.g. 800.")
    return wh


def battery_wh_for(config: dict, address: str) -> int:
    """The battery size for one bike: its own setting, else the add-on's."""
    for bike in config.get("bikes", []):
        if bike["address"] == address.upper() and bike.get("battery_wh"):
            return bike["battery_wh"]
    return config.get("battery_wh")


def load_options(path: str) -> dict:
    """Read the Supervisor's options file; a missing file means 'all defaults'."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
