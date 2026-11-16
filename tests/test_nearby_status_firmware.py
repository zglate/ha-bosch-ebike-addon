"""Tests for the nearby-bike registry, status texts and firmware matching."""
from datetime import datetime

import firmware
import status
from nearby import NONE_OPTION, Nearby

A, B = "AA:00:00:00:00:01", "BB:00:00:00:00:02"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# ── Nearby ───────────────────────────────────────────────────────────────────

def test_options_change_only_when_meaningful():
    nearby = Nearby(Clock())
    assert nearby.heard(A, "/p/a", -60, False, False) is True
    assert nearby.heard(A, "/p/a", -71, False, False) is False      # just signal
    assert nearby.heard(A, "/p/a", -71, True, False) is True        # pairing mode
    assert nearby.options() == [NONE_OPTION, f"{A} (ready to pair)"]


def test_label_round_trips_to_the_address():
    nearby = Nearby(Clock())
    nearby.heard(A, "/p/a", -60, True, True)
    option = nearby.options()[1]
    assert option == f"{A} (ready to pair, yours)"
    assert Nearby.address_of(option) == A
    assert Nearby.address_of(NONE_OPTION) is None and Nearby.address_of("") is None


def test_strangers_strongest_first_and_old_bikes_forgotten():
    clock = Clock()
    nearby = Nearby(clock)
    nearby.heard(A, "/p/a", -80, False, False)
    nearby.heard(B, "/p/b", -50, False, False)
    nearby.heard("CC:00:00:00:00:03", "/p/c", -40, False, True)   # ours
    assert [a for a, _ in nearby.strangers()] == [B, A]
    clock.now += 301
    nearby.heard(B, "/p/b", -50, False, False)
    assert nearby.prune() is True
    assert [a for a, _ in nearby.strangers()] == [B]


# ── Status ───────────────────────────────────────────────────────────────────

def test_setup_with_no_bike_explains_both_ways_and_lists_whats_heard():
    strangers = [(A, {"rssi": -55, "pairable": True})]
    state, attrs, note = status.setup_status([], [], strangers, False, 1)
    assert state == "No bike added yet; 1 Bosch bike(s) nearby"
    assert attrs["nearby"] == [{"address": A, "signal_dbm": -55, "ready_to_pair": True}]
    assert "Start pairing" in note and "Pair selected bike" in note
    assert f"`{A}`: signal -55 dBm, **ready to pair**" in note


def test_setup_with_nothing_heard():
    state, _, note = status.setup_status([], [], [], False, 0)
    assert "none heard" in state and "none yet" in note


def test_setup_once_a_bike_is_known_has_no_notification():
    state, _, note = status.setup_status([A], [A], [], False, None)
    assert state == "1 bike(s), 1 connected" and note is None
    assert status.setup_status([A], [], [], False, None)[0] == "1 bike(s), none connected right now"


def test_setup_during_the_pairing_window():
    state, _, _ = status.setup_status([], [], [], True, None)
    assert state.startswith("Pairing window open")


def test_bike_status_texts():
    now = 1_000_000.0
    assert status.bike_status(False, None, None, None, now)[0] == "Not seen yet"
    assert status.bike_status(True, None, None, "x", now)[0] == "Connected (LDI)"
    sweeping = {"state": "sweeping 600 datapoints", "sweep_total": 600, "sweep_done": 150}
    assert status.bike_status(True, sweeping, None, "x", now)[0] == (
        "Exploring (LDI + MCSP): sweeping datapoints, 25 %")
    idle = {"state": "idle (sweep complete)"}
    assert status.bike_status(False, idle, None, "x", now)[0] == "Connected (MCSP)"
    stopped = {"state": "disconnected"}
    state, attrs = status.bike_status(False, stopped, now + 3600, "x", now)
    assert state.startswith("Released for the Flow app; next check after")
    assert attrs["released_until"]
    assert status.bike_status(False, stopped, None, "x", now)[0] == "Not connected"


# ── Firmware ─────────────────────────────────────────────────────────────────

PAGE = """<html><head><script>var x = "Fake: 9.9.9";</script></head><body>
<h2>Current software versions</h2>
<div>Drive units</div>
<p>Performance Line CX from MY20: 1.1.5.0<br>Performance Line CX smart system: 21.12.0<br>
Performance Line SX smart system: 21.12.0</p>
<p>Kiox 400C: 21.11.0<br>System Controller: 21.11.0<br>Mini Remote: 1.3.24</p>
<p>Batteries smart system: 21.12.0</p><p>ABS: 1.2.0.</p>
<p>Some text: not a version</p></body></html>"""


def test_parse_versions_ignores_scripts_and_prose():
    versions = firmware.parse_versions(PAGE)
    assert versions["Performance Line CX smart system"] == "21.12.0"
    assert versions["Performance Line CX from MY20"] == "1.1.5.0"
    assert versions["System Controller"] == "21.11.0"
    assert versions["ABS"] == "1.2.0"                     # trailing dot tolerated
    assert "Fake" not in versions and "Some text" not in versions


def test_match_rules():
    v = firmware.parse_versions(PAGE)
    assert firmware.match("Performance Line CX", "DriveUnit", v) == (
        "Performance Line CX smart system", "21.12.0")
    assert firmware.match("System Controller", "RemoteControl", v) == (
        "System Controller", "21.11.0")
    assert firmware.match("PowerTube 800", "Battery", v) == ("Batteries smart system", "21.12.0")
    assert firmware.match("kiox 400c", "HeadUnit", v) == ("Kiox 400C", "21.11.0")
    assert firmware.match("Unknown Thing", "DriveUnit", v) is None
    assert firmware.match(None, "DriveUnit", v) is None


def test_version_comparison():
    assert firmware.is_newer("21.12.0", "21.11.0") is True
    assert firmware.is_newer("21.12.0", "21.12.0") is False
    assert firmware.is_newer("21.12", "21.12.0") is False          # padded
    assert firmware.is_newer("1.10.0", "1.9.9") is True             # numeric, not text
    assert firmware.is_newer(None, "21.12.0") is None


def test_update_payload_never_claims_up_to_date_without_data():
    url = firmware.DEFAULT_PAGE
    found = firmware.update_payload("Drive unit", "21.11.0",
                                    ("Performance Line CX smart system", "21.12.0"), url)
    assert found["latest_version"] == "21.12.0" and found["installed_version"] == "21.11.0"
    assert "not confirmed" in found["release_summary"]
    assert found["release_url"].endswith("/connect/release-notes")
    missing = firmware.update_payload("Display", "21.11.0", None, url)
    assert "latest_version" not in missing          # HA rejects null; entity unavailable
    assert all(isinstance(v, str) for v in missing.values())


def test_nearby_keeps_the_last_signal_when_an_update_has_none():
    nearby = Nearby(clock=lambda: 1000.0)
    nearby.heard("AA:AA:AA:AA:AA:01", "/p", -50, False, False)
    nearby.heard("AA:AA:AA:AA:AA:01", "/p", None, False, False)
    assert nearby.get("AA:AA:AA:AA:AA:01")["rssi"] == -50


def test_released_status_names_the_day_when_not_today():
    now = datetime(2026, 11, 16, 18, 0).timestamp()
    today, _ = status.bike_status(False, None, now + 3600, "x", now)
    later, _ = status.bike_status(False, None, now + 30 * 3600, "x", now)
    assert today.endswith("after 19:00")
    assert later.endswith("after Wed 00:00")
