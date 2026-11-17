"""
Home Assistant MQTT discovery + state publishing for the Bosch eBike bridge.

Each connected bike is represented as a separate HA device.  Discovery topics
are published once per bike (retained) so entities appear automatically.
State updates are published as a single JSON payload; each sensor uses a
value_template to extract its field.
"""
from __future__ import annotations
import json
import re
import logging
from typing import NamedTuple

import paho.mqtt.client as mqtt

from datapoints import ENTITIES

log = logging.getLogger(__name__)

# ── Sensor definitions ────────────────────────────────────────────────────────
# ADD-ON: definitions are named tuples instead of positional tuples, and carry
# two extra flags:
#   live    – only meaningful while the bike is connected. These entities go
#             "unavailable" when it disconnects, so a parked bike doesn't keep
#             showing 25 km/h. Others (SoC, odometer ...) keep their last value.
#   invert  – publish ON when the field is false (see "System Locked").


class Sensor(NamedTuple):
    key: str
    name: str
    unit: str | None
    device_class: str | None
    state_class: str | None
    icon: str | None = None
    live: bool = False


class BinarySensor(NamedTuple):
    key: str
    name: str
    device_class: str | None
    icon: str | None = None
    live: bool = False
    invert: bool = False


SENSOR_DEFS: list[Sensor] = [
    Sensor("speed_kmh",   "Speed",              "km/h", "speed",       "measurement", live=True),
    Sensor("cadence",     "Cadence",            "rpm",  None,          "measurement", "mdi:bike", live=True),
    Sensor("rider_power", "Rider Power",        "W",    "power",       "measurement", live=True),
    Sensor("ambient_lux", "Ambient Brightness", "lx",   "illuminance", "measurement", live=True),
    Sensor("battery_soc", "Battery SoC",        "%",    "battery",     "measurement"),
    Sensor("odometer_km", "Odometer",           "km",   "distance",    "total_increasing"),
    # ADD-ON: SoC × nominal pack energy (option battery_wh). An estimate, not
    # a measurement — Bosch's LDI doesn't send energy.
    Sensor("energy_remaining_wh", "Estimated Remaining Energy", "Wh",
           "energy_storage", "measurement", "mdi:battery-heart-variant"),
    # ADD-ON: when the bike last sent data; keeps its value while parked.
    Sensor("last_seen", "Last Seen", None, "timestamp", None),
    Sensor("charge_eta_80_min",  "Charge Time to 80%",  "min", "duration", "measurement",
           "mdi:battery-charging-80", live=True),
    Sensor("charge_eta_100_min", "Charge Time to 100%", "min", "duration", "measurement",
           "mdi:battery-charging-100", live=True),
]

# ADD-ON: labels and device classes corrected against Bosch's LDI field list
# and Home Assistant's binary sensor semantics.
BINARY_SENSOR_DEFS: list[BinarySensor] = [
    BinarySensor("connected", "Connected", "connectivity"),
    # HA's "light" class means *ambient light detected*; this is the bike's lamp.
    BinarySensor("bike_light", "Light", None, "mdi:car-light-high", live=True),
    # HA's "lock" class: ON = unlocked. The field is true when locked, so invert.
    BinarySensor("system_locked", "System Locked", "lock", live=True, invert=True),
    # Bosch: "Charger verbunden" — the charger is plugged in, not "charging".
    BinarySensor("charger_connected", "Charger Connected", "plug"),
    BinarySensor("light_reserve", "Light Reserve", "battery", live=True),
    # Bosch: "DiagnosticTool verbunden" — a dealer tool, not a fault.
    BinarySensor("diagnosis_active", "Diagnostic Tool Connected", None, "mdi:tools",
                 live=True),
    BinarySensor("in_motion", "In Motion", "moving", live=True),
]


# The bridge itself, as one HA device. The identifier is kept from upstream so
# entities created by the Pi version keep their history if someone migrates.
BRIDGE_DEVICE = {
    "identifiers": ["bosch_ebike_pi_bridge"],
    "name": "Bosch eBike Bridge",
    "manufacturer": "Home Assistant add-on",
    "model": "BLE → MQTT bridge",
}


def binary_template(key: str, invert: bool = False) -> str:
    """ADD-ON: binary sensor template where a missing/null value is unknown.

    Upstream rendered anything falsy as OFF, so "no data" looked like "off".
    HA's MQTT binary sensor treats the payload "None" as unknown.
    """
    on, off = ("OFF", "ON") if invert else ("ON", "OFF")
    return (
        "{% set v = value_json.get('" + key + "') %}"
        "{{ 'None' if v is none else ('" + on + "' if v else '" + off + "') }}"
    )


def _json_default(value):
    """Decoded datapoints can hold raw bytes (unknown fields): publish hex."""
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


def to_json(value) -> str:
    return json.dumps(value, default=_json_default)


def _slug(bike_id: str) -> str:
    """Convert a BLE address like AB:CD:EF:01:23:45 to ab_cd_ef_01_23_45."""
    return bike_id.replace(":", "_").lower()


class HaMqttPublisher:
    """Thread-safe MQTT publisher with HA auto-discovery support."""

    def __init__(
        self,
        broker: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        base_topic: str = "bosch_ebike",
        version: str | None = None,
    ) -> None:
        self._base = base_topic.rstrip("/")
        # Add-on version shown on the bridge device in HA.
        self._bridge_device = dict(BRIDGE_DEVICE)
        if version:
            self._bridge_device["sw_version"] = version
        # Bridge controls (buttons and dropdowns), by command topic.
        self._controls: dict[str, dict] = {}
        # Per-bike device details (model, serial, firmware) once the bike says.
        self._device_info: dict[str, dict] = {}
        self._discovered: set[str] = set()
        # Topic the broker watches for the bridge's own online/offline state.
        self._bridge_avail_topic = f"{self._base}/bridge/availability"
        self._bridge_discovered = False

        # Pairing controls: command topic the HA button presses, state topic
        # for the "Pairing active" binary sensor, and the callback to invoke.
        self._pairing_cmd_topic = f"{self._base}/bridge/pairing/set"
        self._pairing_state_topic = f"{self._base}/bridge/pairing/state"
        self._pairing_cb = None
        self._pairing_discovered = False
        self._pairing_last_state = "off"   # last published pairing-active state

        self._client = mqtt.Client(
            client_id="bosch-ebike-bridge",
            clean_session=False,
            protocol=mqtt.MQTTv311,
        )
        if username:
            self._client.username_pw_set(username, password)

        # Last Will & Testament: if the bridge dies (power loss, crash, network
        # drop) the broker publishes "offline" on our behalf, so Home Assistant
        # sees the Bridge connectivity sensor flip to "off" automatically.
        self._client.will_set(
            self._bridge_avail_topic, payload="offline", qos=1, retain=True
        )

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.connect_async(broker, port, keepalive=60)
        self._client.loop_start()

    # ── Internal callbacks ────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc: int) -> None:
        if rc == 0:
            log.info("MQTT connected")
            # Re-publish discovery for all known bikes after reconnect
            self._discovered.clear()
            self._bridge_discovered = False
            self._pairing_discovered = False
            # Announce the bridge itself as online (counterpart to the LWT).
            self.publish_bridge_discovery()
            self._client.publish(
                self._bridge_avail_topic, "online", qos=1, retain=True
            )
            # Re-subscribe to the command topics after (re)connect.
            self._client.subscribe(self._pairing_cmd_topic)
            for topic in list(self._controls):      # run() may still be adding
                self._client.subscribe(topic)
                self._publish_control(topic)
            # Re-publish pairing discovery + state if a callback was registered
            # (connect_async may complete after publish_pairing_discovery ran).
            if self._pairing_cb is not None:
                self.publish_pairing_discovery(self._pairing_cb)
                self._client.publish(
                    self._pairing_state_topic, self._pairing_last_state, retain=True
                )
        else:
            log.error("MQTT connect failed (rc=%d)", rc)

    def _on_disconnect(self, client, userdata, rc: int) -> None:
        if rc != 0:
            log.warning("MQTT disconnected unexpectedly (rc=%d), will retry", rc)

    def _on_message(self, client, userdata, msg) -> None:
        if msg.topic == self._pairing_cmd_topic:
            label, callback, args = "Pairing", self._pairing_cb, ()
        elif msg.topic in self._controls:
            control = self._controls[msg.topic]
            label, callback = control["name"], control["callback"]
            args = (msg.payload.decode(errors="replace"),) if control["kind"] == "select" else ()
        else:
            return
        if callback is None:
            return
        log.info("%s pressed via HA %s", label, args[0] if args else "")
        try:
            callback(*args)
        except Exception as exc:
            log.error("%s callback failed: %s", label, exc)

    # ── Bridge controls: buttons and dropdowns ───────────────────────────────

    def add_button(self, key: str, name: str, icon: str, callback,
                   category: str | None = None) -> None:
        """A button on the bridge device; `callback()` runs when pressed."""
        topic = f"{self._base}/bridge/{key}/set"
        self._controls[topic] = {"kind": "button", "key": key, "name": name, "icon": icon,
                                 "callback": callback, "category": category}
        self._client.subscribe(topic)
        self._publish_control(topic)

    def add_select(self, key: str, name: str, icon: str, options: list[str],
                   callback) -> None:
        """A dropdown on the bridge device; `callback(option)` on a choice."""
        topic = f"{self._base}/bridge/{key}/set"
        self._controls[topic] = {"kind": "select", "key": key, "name": name, "icon": icon,
                                 "callback": callback, "category": None,
                                 "options": list(options), "selected": options[0]}
        self._client.subscribe(topic)
        self._publish_control(topic)

    def set_select(self, key: str, options: list[str], selected: str) -> None:
        """New options and/or selection for a dropdown."""
        topic = f"{self._base}/bridge/{key}/set"
        control = self._controls[topic]
        changed = control["options"] != options
        control["options"], control["selected"] = list(options), selected
        if changed:
            self._publish_control(topic)
        else:
            self._client.publish(f"{self._base}/bridge/{key}/state", selected, retain=True)

    def _publish_control(self, topic: str) -> None:
        c = self._controls[topic]
        unique_id = f"bosch_ebike_bridge_{c['key']}"
        config = {"name": c["name"], "unique_id": unique_id, "command_topic": topic,
                  "icon": c["icon"], "availability_topic": self._bridge_avail_topic,
                  "device": self._bridge_device}
        if c["category"]:
            config["entity_category"] = c["category"]
        if c["kind"] == "button":
            config["payload_press"] = "PRESS"
        else:
            config["options"] = c["options"]
            config["state_topic"] = f"{self._base}/bridge/{c['key']}/state"
        self._client.publish(f"homeassistant/{c['kind']}/{unique_id}/config",
                             json.dumps(config), retain=True)
        if c["kind"] == "select":
            self._client.publish(config["state_topic"], c["selected"], retain=True)

    # ── Status sensors ───────────────────────────────────────────────────────

    def publish_setup_status(self, state: str, attributes: dict) -> None:
        """Bridge "Setup Status": where setup stands and what to do next."""
        topic = f"{self._base}/bridge/setup"
        if "setup" not in self._discovered:
            self._discovered.add("setup")
            self._client.publish(
                "homeassistant/sensor/bosch_ebike_bridge_setup/config", json.dumps({
                    "name": "Setup Status", "unique_id": "bosch_ebike_bridge_setup",
                    "state_topic": topic, "value_template": "{{ value_json.state }}",
                    "json_attributes_topic": topic,
                    "json_attributes_template": "{{ value_json.attributes | tojson }}",
                    "icon": "mdi:bike-fast", "device": self._bridge_device}), retain=True)
        self._client.publish(topic, json.dumps({"state": state, "attributes": attributes},
                                               default=str), retain=True)

    def publish_range_discovery(self, bike_id: str, bike_name: str, modes: list) -> None:
        """One range sensor per assist mode (the bike's own mode names)."""
        key = f"range:{bike_id}:{'|'.join(modes)}"
        if key in self._discovered:
            return
        self._discovered.add(key)
        slug = _slug(bike_id)
        for mode in modes:
            # Topic-safe ID: "TOUR+" would put an MQTT wildcard into the topic.
            mode_id = re.sub(r"[^a-z0-9]+", "_", mode.lower()).strip("_") or "mode"
            unique_id = f"bosch_ebike_{slug}_range_{mode_id}"
            mode_js = json.dumps(mode)
            self._client.publish(f"homeassistant/sensor/{unique_id}/config", json.dumps({
                "name": f"Range {mode}", "unique_id": unique_id,
                "state_topic": f"{self._base}/{slug}/mcsp",
                "value_template": ("{% set r = value_json.get('range_by_mode') or {} %}"
                                   "{{ r.get(" + mode_js + ", '') }}"),
                "unit_of_measurement": "km", "device_class": "distance",
                "state_class": "measurement", "icon": "mdi:map-marker-distance",
                "device": self._bike_device(slug, bike_name, bike_id)}), retain=True)

    def remove_bike(self, bike_id: str, table: dict) -> None:
        """Remove a bike's entities (and device) from Home Assistant."""
        slug = _slug(bike_id)
        topics = [f"homeassistant/sensor/bosch_ebike_{slug}_{s.key}/config" for s in SENSOR_DEFS]
        topics += [f"homeassistant/binary_sensor/bosch_ebike_{slug}_{b.key}/config"
                   for b in BINARY_SENSOR_DEFS]
        topics += [f"homeassistant/sensor/bosch_ebike_{slug}_mcsp_{e.key}/config"
                   for e in ENTITIES]
        topics += [f"homeassistant/sensor/bosch_ebike_{slug}_{k}/config"
                   for k in ("status", "mcsp_status", "mcsp_values")]
        topics += [f"homeassistant/update/bosch_ebike_{slug}_firmware_{k}/config"
                   for k in ("drive_unit", "battery", "controller", "head_unit")]
        topics += [f"homeassistant/sensor/bosch_ebike_{slug}_dp_{dp_id:04x}/config"
                   for dp_id in table]
        for topic in topics:
            self._client.publish(topic, "", retain=True)
        self.forget_discovery(bike_id)
        self._device_info.pop(bike_id, None)

    def forget_discovery(self, bike_id: str) -> None:
        """Publish a bike's discovery again next time (e.g. after a rename)."""
        self._discovered = {k for k in self._discovered if bike_id not in k}

    def set_device_info(self, bike_id: str, bike_name: str, info: dict) -> None:
        """Model, serial number and firmware for a bike's HA device."""
        if self._device_info.get(bike_id) == info:
            return
        self._device_info[bike_id] = dict(info)
        # Re-publishing one entity's discovery updates the device registry.
        self._discovered.discard(f"status:{bike_id}")

    def publish_bike_status(self, bike_id: str, bike_name: str, state: str,
                            attributes: dict) -> None:
        """A bike's "Status": connected how, exploring, released until when."""
        slug = _slug(bike_id)
        topic = f"{self._base}/{slug}/status"
        key = f"status:{bike_id}"
        if key not in self._discovered:
            self._discovered.add(key)
            unique_id = f"bosch_ebike_{slug}_status"
            self._client.publish(f"homeassistant/sensor/{unique_id}/config", json.dumps({
                "name": "Status", "unique_id": unique_id, "state_topic": topic,
                "value_template": "{{ value_json.state }}", "json_attributes_topic": topic,
                "json_attributes_template": "{{ value_json.attributes | tojson }}",
                "icon": "mdi:bicycle-electric",
                "device": self._bike_device(slug, bike_name, bike_id)}), retain=True)
        self._client.publish(topic, json.dumps({"state": state, "attributes": attributes},
                                               default=str), retain=True)

    def publish_firmware(self, bike_id: str, bike_name: str, key: str, title: str,
                         payload: dict, available: bool) -> None:
        """An update entity for one component. No install command: metadata only.

        Unavailable while no published version matches: Home Assistant keeps
        the last latest_version otherwise, which could claim "up to date".
        """
        slug = _slug(bike_id)
        topic = f"{self._base}/{slug}/firmware/{key}"
        if f"fw:{bike_id}:{key}" not in self._discovered:
            self._discovered.add(f"fw:{bike_id}:{key}")
            unique_id = f"bosch_ebike_{slug}_firmware_{key}"
            self._client.publish(f"homeassistant/update/{unique_id}/config", json.dumps({
                "name": f"{title} firmware", "unique_id": unique_id, "state_topic": topic,
                "availability_topic": f"{topic}/available",
                "device_class": "firmware",
                "device": self._bike_device(slug, bike_name, bike_id)}), retain=True)
        self._client.publish(topic, json.dumps(payload), retain=True)
        self._client.publish(f"{topic}/available", "online" if available else "offline",
                             retain=True)

    def remove_firmware(self, bike_id: str, key: str) -> None:
        """Remove a component's update entity (firmware check switched off)."""
        slug = _slug(bike_id)
        self._discovered.discard(f"fw:{bike_id}:{key}")
        self._client.publish(
            f"homeassistant/update/bosch_ebike_{slug}_firmware_{key}/config", "", retain=True)

    # ── Public API ────────────────────────────────────────────────────────────

    def publish_bridge_discovery(self) -> None:
        """Publish HA discovery for the bridge's own connectivity sensor.

        Backed by the LWT availability topic: ON while the bridge is running,
        OFF (via the broker's will message) if it loses power, crashes, or drops
        off the network. Lives under its own HA device so it groups separately
        from the bikes.
        """
        if self._bridge_discovered:
            return
        self._bridge_discovered = True

        unique_id = "bosch_ebike_bridge_online"
        payload = {
            "name": "Bridge",
            "unique_id": unique_id,
            # The availability topic IS the state: 'online' → ON, 'offline' → OFF.
            "state_topic": self._bridge_avail_topic,
            "payload_on": "online",
            "payload_off": "offline",
            "device_class": "connectivity",
            "device": self._bridge_device,
        }
        self._client.publish(
            f"homeassistant/binary_sensor/{unique_id}/config",
            json.dumps(payload),
            retain=True,
        )
        log.info("Published HA discovery for bridge connectivity sensor")

    def publish_pairing_discovery(self, callback) -> None:
        """Publish HA discovery for the pairing button + 'Pairing active' sensor.

        The button (an MQTT button entity) publishes to the command topic; the
        binary sensor reflects whether the discoverable pairing window is open.
        `callback` is invoked (no args) when the button is pressed.
        """
        self._pairing_cb = callback
        device = self._bridge_device
        self._client.publish(
            "homeassistant/button/bosch_ebike_bridge_pairing/config",
            json.dumps({
                "name": "Start pairing",
                "unique_id": "bosch_ebike_bridge_pairing",
                "command_topic": self._pairing_cmd_topic,
                "payload_press": "PRESS",
                "icon": "mdi:bluetooth-connect",
                "availability_topic": self._bridge_avail_topic,
                "device": device,
            }),
            retain=True,
        )
        self._client.publish(
            "homeassistant/binary_sensor/bosch_ebike_bridge_pairing_active/config",
            json.dumps({
                "name": "Pairing active",
                "unique_id": "bosch_ebike_bridge_pairing_active",
                "state_topic": self._pairing_state_topic,
                "payload_on": "on",
                "payload_off": "off",
                "device_class": "running",
                "availability_topic": self._bridge_avail_topic,
                "device": device,
            }),
            retain=True,
        )
        self._pairing_discovered = True
        log.info("Published HA discovery for pairing button + status")

    def publish_forget_discovery(self, callback) -> None:
        """Button that removes the bonds of all known bikes (stale bond fix).
        Afterwards the pairing window opens and the bike is added again."""
        self.add_button("forget", "Forget paired bikes", "mdi:bluetooth-off", callback,
                        category="config")

    def publish_pairing_active(self, active: bool) -> None:
        """Publish the current pairing-window state (retained)."""
        self._pairing_last_state = "on" if active else "off"
        self._client.publish(
            self._pairing_state_topic, self._pairing_last_state, retain=True
        )

    def publish_discovery(self, bike_id: str, bike_name: str) -> None:
        """Publish HA MQTT discovery config for all entities of one bike.

        Idempotent – only publishes once per bike_id per session.
        """
        if bike_id in self._discovered:
            return
        self._discovered.add(bike_id)

        slug = _slug(bike_id)
        state_topic = f"{self._base}/{slug}/state"
        avail_topic = f"{self._base}/{slug}/availability"
        device = self._bike_device(slug, bike_name, bike_id)

        for s in SENSOR_DEFS:
            unique_id = f"bosch_ebike_{slug}_{s.key}"
            payload: dict = {
                "name": s.name,
                "unique_id": unique_id,
                "state_topic": state_topic,
                # Render a missing OR null value as '' so HA shows "unknown"
                # rather than the literal string "None" (the charge-ETA fields
                # are null whenever the bike isn't charging).
                "value_template": (
                    "{% set v = value_json." + s.key + " %}"
                    "{{ v if v is not none else '' }}"
                ),
                "device": device,
            }
            if s.unit:
                payload["unit_of_measurement"] = s.unit
            if s.device_class:
                payload["device_class"] = s.device_class
            if s.state_class:
                payload["state_class"] = s.state_class
            if s.icon:
                payload["icon"] = s.icon
            if s.live:
                # ADD-ON: live readings go unavailable while disconnected.
                payload["availability_topic"] = avail_topic
            self._client.publish(
                f"homeassistant/sensor/{unique_id}/config",
                json.dumps(payload),
                retain=True,
            )

        for b in BINARY_SENSOR_DEFS:
            unique_id = f"bosch_ebike_{slug}_{b.key}"

            if b.key == "connected":
                # The 'connected' sensor reflects the bike's BLE connection,
                # which is tracked via the availability topic (online/offline) —
                # NOT a field in the state JSON (the decoder never emits a
                # 'connected' key). Drive the sensor straight from that topic.
                payload = {
                    "name": b.name,
                    "unique_id": unique_id,
                    "state_topic": avail_topic,
                    "payload_on": "online",
                    "payload_off": "offline",
                    "device": device,
                }
            else:
                payload = {
                    "name": b.name,
                    "unique_id": unique_id,
                    "state_topic": state_topic,
                    "value_template": binary_template(b.key, b.invert),
                    "device": device,
                }
                if b.live:
                    payload["availability_topic"] = avail_topic
            if b.device_class:
                payload["device_class"] = b.device_class
            if b.icon:
                payload["icon"] = b.icon
            self._client.publish(
                f"homeassistant/binary_sensor/{unique_id}/config",
                json.dumps(payload),
                retain=True,
            )

        log.info("Published HA discovery for '%s' (%s)", bike_name, bike_id)

    def publish_state(self, bike_id: str, state: dict) -> None:
        """Publish current sensor state for one bike (retained so HA keeps
        last known values across restarts and when the bike is offline)."""
        slug = _slug(bike_id)
        self._client.publish(
            f"{self._base}/{slug}/state",
            json.dumps(state),
            retain=True,
        )

    # ── MCSP (Flow-app channel) entities ─────────────────────────────────────

    def _bike_device(self, slug: str, bike_name: str, bike_id: str) -> dict:
        """The bike's HA device: model, serial and firmware once the bike says."""
        # No placeholder model: every retained discovery config updates the
        # device, and a generic one could overwrite the model the bike reported.
        device = {"identifiers": [f"bosch_ebike_{slug}"], "name": bike_name,
                  "manufacturer": "Bosch", "via_device": "bosch_ebike_pi_bridge"}
        device.update(self._device_info.get(bike_id, {}))
        return device

    def publish_mcsp_discovery(self, bike_id: str, bike_name: str) -> None:
        """Curated MCSP sensors (motor power, battery health ...). Once per bike.

        They share the bike's HA device with the LDI entities but have their
        own unique IDs and state topic, so the two never overwrite each other.
        """
        key = f"mcsp:{bike_id}"
        if key in self._discovered:
            return
        self._discovered.add(key)
        slug = _slug(bike_id)
        state_topic = f"{self._base}/{slug}/mcsp"
        for old in ("remaining_energy_raw", "lifetime_delivered_wh_raw"):    # renamed in 0.6
            self._client.publish(f"homeassistant/sensor/bosch_ebike_{slug}_mcsp_{old}/config",
                                 "", retain=True)
        for e in ENTITIES:
            unique_id = f"bosch_ebike_{slug}_mcsp_{e.key}"
            payload: dict = {
                "name": e.name, "unique_id": unique_id, "state_topic": state_topic,
                "value_template": ("{% set v = value_json.get('" + e.key + "') %}"
                                   "{{ v if v is not none else '' }}"),
                "device": self._bike_device(slug, bike_name, bike_id),
            }
            for field, value in (("unit_of_measurement", e.unit),
                                 ("device_class", e.device_class),
                                 ("state_class", e.state_class)):
                if value:
                    payload[field] = value
            if e.live:
                payload["availability_topic"] = f"{self._base}/{slug}/availability"
            self._client.publish(f"homeassistant/sensor/{unique_id}/config",
                                 json.dumps(payload), retain=True)

    def publish_mcsp_state(self, bike_id: str, state: dict) -> None:
        self._client.publish(f"{self._base}/{_slug(bike_id)}/mcsp", to_json(state),
                             retain=True)

    def publish_mcsp_status(self, bike_id: str, bike_name: str, status: dict) -> None:
        """Diagnostic sensor: MCSP session state, with all counters as attributes."""
        slug = _slug(bike_id)
        topic = f"{self._base}/{slug}/mcsp_status"
        key = f"mcsp_status:{bike_id}"
        if key not in self._discovered:
            self._discovered.add(key)
            unique_id = f"bosch_ebike_{slug}_mcsp_status"
            self._client.publish(f"homeassistant/sensor/{unique_id}/config", json.dumps({
                "name": "MCSP Status", "unique_id": unique_id, "state_topic": topic,
                "value_template": "{{ value_json.state[:255] }}",
                "json_attributes_topic": topic, "entity_category": "diagnostic",
                "icon": "mdi:bluetooth-settings",
                "device": self._bike_device(slug, bike_name, bike_id),
            }), retain=True)
            unique_id = f"bosch_ebike_{slug}_mcsp_values"
            self._client.publish(f"homeassistant/sensor/{unique_id}/config", json.dumps({
                "name": "MCSP Datapoints with Values", "unique_id": unique_id,
                "state_topic": topic, "value_template": "{{ value_json.get('values', 0) }}",
                "entity_category": "diagnostic", "state_class": "measurement",
                "icon": "mdi:counter", "device": self._bike_device(slug, bike_name, bike_id),
            }), retain=True)
        self._client.publish(topic, json.dumps(status, default=str), retain=True)

    def publish_datapoint(self, bike_id: str, bike_name: str, dp_id: int, label: str,
                          unit: str | None, value) -> None:
        """Every decoded datapoint as its own diagnostic entity, disabled by
        default (enable any of them in HA to chart it)."""
        slug = _slug(bike_id)
        hex_id = f"{dp_id:04x}"
        topic = f"{self._base}/{slug}/dp/{hex_id}"
        key = f"dp:{bike_id}:{hex_id}"
        structured = isinstance(value, (dict, list))
        if key not in self._discovered:
            self._discovered.add(key)
            unique_id = f"bosch_ebike_{slug}_dp_{hex_id}"
            payload = {
                "name": f"{label} (0x{dp_id:04X})", "unique_id": unique_id,
                "state_topic": topic, "entity_category": "diagnostic",
                "enabled_by_default": False,
                "device": self._bike_device(slug, bike_name, bike_id),
            }
            if structured:
                payload["value_template"] = "{{ value[:255] }}"
                payload["json_attributes_topic"] = topic
                payload["json_attributes_template"] = (
                    "{{ {'value': value_json} | tojson }}")
            elif unit and isinstance(value, (int, float)) and not isinstance(value, bool):
                payload["unit_of_measurement"] = unit
            self._client.publish(f"homeassistant/sensor/{unique_id}/config",
                                 json.dumps(payload), retain=True)
        payload = to_json(value) if structured else str(value)
        self._client.publish(topic, payload, retain=True)

    def publish_sweep_discovery(self, callback) -> None:
        """Button: read every readable datapoint on every connected bike."""
        self.add_button("sweep", "Run MCSP read sweep", "mdi:magnify-scan", callback,
                        category="diagnostic")

    def publish_availability(self, bike_id: str, online: bool) -> None:
        """Publish online/offline availability for one bike (retained)."""
        slug = _slug(bike_id)
        self._client.publish(
            f"{self._base}/{slug}/availability",
            "online" if online else "offline",
            retain=True,
        )

    def stop(self) -> None:
        # Mark the bridge offline on a clean shutdown (systemctl stop / restart)
        # so HA reflects it immediately rather than waiting for the LWT timeout.
        try:
            self._client.publish(
                self._bridge_avail_topic, "offline", qos=1, retain=True
            ).wait_for_publish(timeout=2)
        except Exception:
            pass
        self._client.loop_stop()
        self._client.disconnect()
