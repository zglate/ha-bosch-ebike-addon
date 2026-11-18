"""Tests for turning add-on options into the bridge config."""
import json

import pytest

from options import ConfigError, MqttMissing, battery_wh_for, build_config, load_options

MOSQUITTO = {"MQTT_HOST": "core-mosquitto", "MQTT_PORT": "1883",
             "MQTT_USERNAME": "addons", "MQTT_PASSWORD": "secret"}


def test_defaults_with_mosquitto_service():
    config = build_config({}, MOSQUITTO)
    assert config == {
        "bridge_name": "HA eBike Bridge",
        "adapter": "hci0",
        "private_advertising": True,
        "log_level": "info",
        "battery_wh": 800,
        "version": None,
        "bikes": [],
        "mqtt": {"broker": "core-mosquitto", "port": 1883, "username": "addons",
                 "password": "secret", "base_topic": "bosch_ebike"},
        "mcsp": True, "mcsp_connect": True, "mcsp_sweep": True,
        "gatt_listen": True, "raw_entities": False, "capture": True,
        "open_pairing_on_start": False, "mcsp_reconnect_hours": 6.0,
        "firmware_check": True, "notify_full": True,
        "firmware_page": "https://www.bosch-ebike.com/ca/service/software-update",
    }


def test_explorer_options_can_be_switched_off():
    config = build_config({"mcsp_connect": False, "capture": False}, MOSQUITTO)
    assert config["mcsp_connect"] is False and config["capture"] is False
    assert config["mcsp"] is True


def test_version_comes_from_the_add_on():
    assert build_config({}, {**MOSQUITTO, "ADDON_VERSION": "0.2.0"})["version"] == "0.2.0"


def test_battery_size_must_be_positive():
    assert build_config({"battery_wh": 625}, MOSQUITTO)["battery_wh"] == 625
    with pytest.raises(ConfigError, match="battery_wh"):
        build_config({"battery_wh": 0}, MOSQUITTO)
    with pytest.raises(ConfigError, match="battery_wh"):
        build_config({"bikes": [{"address": "AA:BB:CC:DD:EE:FF", "battery_wh": -1}]},
                     MOSQUITTO)


def test_battery_size_per_bike_overrides_the_add_on_setting():
    config = build_config({"battery_wh": 800, "bikes": [
        {"address": "aa:bb:cc:dd:ee:ff", "name": "Range", "battery_wh": 625},
        {"address": "11:22:33:44:55:66", "name": "Other"},
    ]}, MOSQUITTO)
    assert battery_wh_for(config, "AA:BB:CC:DD:EE:FF") == 625
    assert battery_wh_for(config, "11:22:33:44:55:66") == 800   # no own setting
    assert battery_wh_for(config, "99:99:99:99:99:99") == 800   # unlisted bike


def test_manual_broker_overrides_service():
    config = build_config({"mqtt_host": "10.0.0.5", "mqtt_port": 8883}, MOSQUITTO)
    assert config["mqtt"]["broker"] == "10.0.0.5"
    assert config["mqtt"]["port"] == 8883
    assert config["mqtt"]["username"] is None


def test_no_broker_is_a_clear_error():
    with pytest.raises(MqttMissing, match="Mosquitto"):
        build_config({}, {})


def test_bikes_are_normalised():
    options = {"bikes": [{"address": "aa:bb:cc:dd:ee:ff", "name": "Range"},
                         {"address": "11:22:33:44:55:66"},
                         {"address": ""}]}
    assert build_config(options, MOSQUITTO)["bikes"] == [
        {"address": "AA:BB:CC:DD:EE:FF", "name": "Range"},
        {"address": "11:22:33:44:55:66"},
    ]


def test_empty_service_credentials_become_none():
    env = {"MQTT_HOST": "core-mosquitto", "MQTT_PORT": "", "MQTT_USERNAME": "",
           "MQTT_PASSWORD": ""}
    mqtt = build_config({}, env)["mqtt"]
    assert mqtt["port"] == 1883
    assert mqtt["username"] is None and mqtt["password"] is None


def test_load_options(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"adapter": "hci1"}), encoding="utf-8")
    assert load_options(str(path)) == {"adapter": "hci1"}
    assert load_options(str(tmp_path / "missing.json")) == {}
