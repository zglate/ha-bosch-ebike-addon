"""Tests for the Home Assistant MQTT discovery and state messages."""
import json

import jinja2
import pytest

import ha_mqtt

BIKE = "AA:BB:CC:DD:EE:FF"
SLUG = "aa_bb_cc_dd_ee_ff"
AVAIL = f"bosch_ebike/{SLUG}/availability"


class FakeClient:
    """Records publishes instead of talking to a broker."""

    def __init__(self, *args, **kwargs):
        self.published = {}

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published[topic] = (payload, retain)

    def __getattr__(self, name):
        # username_pw_set, will_set, connect_async, loop_start, subscribe ...
        return lambda *args, **kwargs: None


@pytest.fixture
def publisher(monkeypatch):
    monkeypatch.setattr(ha_mqtt.mqtt, "Client", FakeClient)
    pub = ha_mqtt.HaMqttPublisher(broker="core-mosquitto", version="0.2.0")
    pub.publish_discovery(BIKE, "Range VLT CX")
    return pub


def config_for(publisher, component, key):
    topic = f"homeassistant/{component}/bosch_ebike_{SLUG}_{key}/config"
    payload, retain = publisher._client.published[topic]
    assert retain, "discovery must be retained so HA finds it after a restart"
    return json.loads(payload)


def render(template, state):
    """Render a value_template the way HA does (value_json = parsed payload)."""
    return jinja2.Environment().from_string(template).render(value_json=state).strip()


def test_bike_sensors_are_discovered(publisher):
    battery = config_for(publisher, "sensor", "battery_soc")
    assert battery["unit_of_measurement"] == "%"
    assert battery["device_class"] == "battery"
    assert battery["state_topic"] == f"bosch_ebike/{SLUG}/state"
    assert battery["device"]["name"] == "Range VLT CX"

    power = config_for(publisher, "sensor", "rider_power")
    assert (power["unit_of_measurement"], power["device_class"]) == ("W", "power")

    odometer = config_for(publisher, "sensor", "odometer_km")
    assert odometer["state_class"] == "total_increasing"


@pytest.mark.parametrize("key", ["speed_kmh", "cadence", "rider_power", "ambient_lux"])
def test_live_sensors_go_unavailable_when_bike_disconnects(publisher, key):
    assert config_for(publisher, "sensor", key)["availability_topic"] == AVAIL


@pytest.mark.parametrize("key", ["battery_soc", "odometer_km", "energy_remaining_wh",
                                 "last_seen"])
def test_lasting_sensors_keep_their_last_value(publisher, key):
    assert "availability_topic" not in config_for(publisher, "sensor", key)


def test_estimated_energy_and_last_seen_sensors(publisher):
    energy = config_for(publisher, "sensor", "energy_remaining_wh")
    assert (energy["unit_of_measurement"], energy["device_class"]) == ("Wh", "energy_storage")
    last_seen = config_for(publisher, "sensor", "last_seen")
    assert last_seen["device_class"] == "timestamp"
    assert "unit_of_measurement" not in last_seen


def test_sensor_template_renders_null_as_unknown(publisher):
    template = config_for(publisher, "sensor", "charge_eta_80_min")["value_template"]
    assert render(template, {"charge_eta_80_min": None}) == ""
    assert render(template, {"charge_eta_80_min": 42}) == "42"


def test_system_locked_is_inverted_for_the_lock_class(publisher):
    # HA lock class: ON = unlocked, OFF = locked.
    locked = config_for(publisher, "binary_sensor", "system_locked")
    assert locked["device_class"] == "lock"
    assert render(locked["value_template"], {"system_locked": True}) == "OFF"
    assert render(locked["value_template"], {"system_locked": False}) == "ON"


def test_charger_field_is_a_plug_not_charging(publisher):
    charger = config_for(publisher, "binary_sensor", "charger_connected")
    assert charger["name"] == "Charger Connected"
    assert charger["device_class"] == "plug"
    assert "availability_topic" not in charger
    assert render(charger["value_template"], {"charger_connected": True}) == "ON"


def test_diagnostic_tool_is_not_reported_as_a_problem(publisher):
    diag = config_for(publisher, "binary_sensor", "diagnosis_active")
    assert diag["name"] == "Diagnostic Tool Connected"
    assert "device_class" not in diag


def test_bike_light_has_no_ambient_light_class(publisher):
    light = config_for(publisher, "binary_sensor", "bike_light")
    assert "device_class" not in light
    assert light["availability_topic"] == AVAIL


@pytest.mark.parametrize("state", [{}, {"bike_light": None}])
def test_missing_binary_value_is_unknown_not_off(publisher, state):
    light = config_for(publisher, "binary_sensor", "bike_light")
    assert render(light["value_template"], state) == "None"


def test_connected_sensor_follows_availability_topic(publisher):
    connected = config_for(publisher, "binary_sensor", "connected")
    assert connected["state_topic"] == AVAIL
    assert connected["payload_on"] == "online"


def test_discovery_is_published_once_per_bike(publisher):
    publisher._client.published.clear()
    publisher.publish_discovery(BIKE, "Range VLT CX")
    assert publisher._client.published == {}


def test_state_is_retained_json(publisher):
    publisher.publish_state(BIKE, {"battery_soc": 87, "odometer_km": 1234.5})
    payload, retain = publisher._client.published[f"bosch_ebike/{SLUG}/state"]
    assert retain
    assert json.loads(payload) == {"battery_soc": 87, "odometer_km": 1234.5}


def test_bridge_device_shows_add_on_and_version(publisher):
    publisher.publish_bridge_discovery()
    payload, _ = publisher._client.published[
        "homeassistant/binary_sensor/bosch_ebike_bridge_online/config"]
    device = json.loads(payload)["device"]
    assert device["manufacturer"] == "Home Assistant add-on"
    assert device["sw_version"] == "0.2.0"


def test_forget_button_is_discovered_and_calls_back(publisher):
    pressed = []
    publisher.publish_forget_discovery(lambda: pressed.append(True))
    payload, _ = publisher._client.published[
        "homeassistant/button/bosch_ebike_bridge_forget/config"]
    button = json.loads(payload)
    assert button["entity_category"] == "config"

    class Msg:
        topic = button["command_topic"]
    publisher._on_message(None, None, Msg())
    assert pressed == [True]
