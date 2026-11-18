"""
End-to-end tests of the explorer: the real bridge against the fake BlueZ's
fake Bosch bike speaking MCSP, plus a real MQTT broker.

Runs in the test container (scripts/test.sh); skipped elsewhere.
"""
import json
import time

import pytest

from protobuf_helpers import field
from test_bridge_integration import (BIKE, SNAPSHOT, Harness, dbus,  # noqa: F401
                                     wait_for)

import datapoints  # noqa: E402  (app module, on sys.path via conftest)

TABLE = datapoints.load_table()

SLUG = "a4_0d_bc_01_23_45"
MCSP_TOPIC = f"bosch_ebike/{SLUG}/mcsp"
STATUS_TOPIC = f"bosch_ebike/{SLUG}/mcsp_status"
SECOND_BIKE = "D0:00:00:00:00:02"

# Bodies the fake bike answers with (protobuf, field 1).
VALUES = {
    "0x1806": "0a07" + b"21.12.0".hex(),          # drive unit software
    "0x185d": "08fa01",                           # motor power 250 W
    "0x0096": "08d209",                           # cycles raw 1234 -> 123.4
    "0x00e2": "08ac11",                           # capacity raw 2220 -> 22.2 Ah
    "0x0088": "0857",                             # SoC 87 %
    "0x180d": "0a034f4646" + "0a0345434f" + "0a05544f55522b",   # OFF, ECO, TOUR+
    "0x1809": "0802",                             # assist mode index 2
    "0x1819": "082a",                             # 42 power-on hours
    "0x182e": "0a13" + b"Performance Line CX".hex(),     # drive unit product name
    "0x1801": "0a06" + b"SN1234".hex(),           # drive unit serial number
}


@pytest.fixture
def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    h.close()


def enable_mcsp(harness, address=BIKE, **config):
    harness.control.EnableMcsp(address, json.dumps({"values": VALUES, **config}))


def status(harness):
    return harness.mqtt.json(STATUS_TOPIC) or {}


def wait_sweep_done(harness):
    return wait_for(lambda: "sweep complete" in status(harness).get("state", ""),
                    "read sweep to finish", timeout=90)


def connect_mcsp_bike(harness, **config):
    harness.start_bridge()
    enable_mcsp(harness, **config)
    harness.control.Connect(BIKE, True, True, dbus.ByteArray(SNAPSHOT))


def test_mcsp_values_reach_home_assistant(harness):
    connect_mcsp_bike(harness)
    expected = ("drive_unit_software", "motor_power", "charge_cycles", "battery_capacity",
                "power_on_hours")
    # Wait for every value checked below (they arrive one by one; slow under
    # ARM emulation).
    state = wait_for(lambda: (s := harness.mqtt.json(MCSP_TOPIC))
                     and all(k in s for k in expected) and s.get("assist_mode") == "TOUR+"
                     and s, "curated MCSP values", timeout=30)
    assert state["drive_unit_software"] == "21.12.0"
    assert state["motor_power"] == 250
    assert state["charge_cycles"] == 123.4
    assert state["battery_capacity"] == 22.2
    assert state["power_on_hours"] == 42

    # Every decoded datapoint also gets its own (hidden) diagnostic entity.
    assert harness.mqtt.get(f"bosch_ebike/{SLUG}/dp/1806") == "21.12.0"
    config = json.loads(harness.mqtt.get(
        f"homeassistant/sensor/bosch_ebike_{SLUG}_dp_1806/config"))
    assert config["enabled_by_default"] is False
    assert config["entity_category"] == "diagnostic"


def test_only_read_only_requests_ever_reach_the_bike(harness):
    connect_mcsp_bike(harness)
    wait_sweep_done(harness)
    log = harness.mcsp_log()

    # Transport: only VERSION (1), DISABLE_FLOW_CONTROL (3), MAX_SEGMENTATION (4).
    assert {bytes.fromhex(c)[0] for c in log["commands"]} <= {1, 3, 4}
    requests = [m for m in log["messages"] if m["op"] % 2 == 0]
    replies = [m for m in log["messages"] if m["op"] % 2 == 1]
    assert {m["op"] for m in requests} <= {0, 6, 8}          # READ, SUBSCRIBE, UNSUB
    assert all(m["body"] == "" for m in requests)
    assert all(m["body"] == "04" for m in replies)            # only UNSUPPORTED replies
    # Addresses as the Flow app uses them.
    assert {m["src"] for m in requests if m["op"] == 0} == {0x4080}
    assert {m["src"] for m in requests if m["op"] == 6} == {0x4180}
    # Never a request to a command (RPC) or otherwise unreadable datapoint.
    requested = {m["dst"] & 0x7FFF for m in requests}
    unreadable = {i for i, dp in TABLE.items() if not dp.readable}
    assert requested & unreadable == set()
    # The sweep covered the whole readable table.
    readable = {i for i, dp in TABLE.items() if dp.readable}
    assert {m["dst"] & 0x7FFF for m in requests if m["op"] == 0} == readable


def test_the_bikes_questions_are_declined_and_logged(harness):
    connect_mcsp_bike(harness)
    wait_for(lambda: any(m["op"] == 1 and m["src"] == 0x41AA
                         for m in harness.mcsp_log()["messages"]), "unsupported reply")
    reply = next(m for m in harness.mcsp_log()["messages"] if m["src"] == 0x41AA)
    assert (reply["dst"], reply["seq"], reply["body"]) == (0x2100, 3, "04")
    s = wait_for(lambda: (s := status(harness)).get("bike_requests", 0) >= 3 and s,
                 "all bike requests counted")
    # The startup-stage write is decoded into a timeline.
    assert [x["stage"] for x in s["startup_stages"]] == [5]
    # Requests to addresses that aren't the phone's (0x4180) get no reply.
    assert not any(m["op"] % 2 == 1 and m["src"] == 0x4180
                   for m in harness.mcsp_log()["messages"])


def test_handshake_answers_the_bike_and_adopts_its_packet_limit(harness):
    connect_mcsp_bike(harness, starts_handshake=True)
    s = wait_for(lambda: (s := status(harness)).get("peer_max_packet") and s, "handshake")
    assert s["handshake"] == "answered the bike's"
    assert (s["mtu"], s["peer_version"], s["peer_max_packet"]) == (247, 3, 180)
    assert s["packet_size"] == 180


def test_handshake_is_sent_proactively_when_the_bike_is_silent(harness):
    connect_mcsp_bike(harness, starts_handshake=False)
    wait_for(lambda: "0103" in harness.mcsp_log()["commands"], "our VERSION command")
    assert status(harness)["handshake"] == "proactive (bike sent none)"


def test_overloaded_bike_gets_a_retry_after_backing_off(harness):
    connect_mcsp_bike(harness, overload_once=["0x1819"])
    wait_for(lambda: (harness.mqtt.json(MCSP_TOPIC) or {}).get("power_on_hours") == 42,
             "value after the retry", timeout=20)
    wait_for(lambda: status(harness).get("overloaded", 0) >= 1, "overload counted")


def test_reads_fall_back_to_the_other_source_address(harness):
    connect_mcsp_bike(harness, only_src="0x4180")
    wait_for(lambda: (harness.mqtt.json(MCSP_TOPIC) or {}).get("drive_unit_software")
             == "21.12.0", "value read from 0x4180", timeout=30)
    wait_for(lambda: any(k.startswith("READ from 0x4180")
                         for k in status(harness).get("ok_by_source", {})),
             "fallback recorded in status")


def test_with_automatic_reads_off_it_never_connects_by_itself(harness):
    """mcsp_connect off: no peeks. Even a listed, bonded, advertising bike is
    left alone (only the Read now button would connect)."""
    harness.start_bridge(open_pairing_on_start=False, bikes=[{"address": SECOND_BIKE}],
                         mcsp_connect=False)
    enable_mcsp(harness, address=SECOND_BIKE)
    harness.control.Advertise(SECOND_BIKE, dbus.ByteArray(bytes([0, 0, 2, 0x21, 1, 1])), True)
    harness.control.Advertise(SECOND_BIKE, dbus.ByteArray(bytes([0, 0, 2, 0x21, 1, 1])), True)
    wait_for(lambda: any(e["kind"] == "advert" for e in harness.capture_events()), "heard")
    time.sleep(2)
    assert harness.state()["connect_calls"] == []

def test_other_notifying_characteristics_are_recorded(harness):
    harness.start_bridge()
    harness.control.Connect(BIKE, True, True, dbus.ByteArray(SNAPSHOT))
    wait_for(lambda: any(p.endswith("char0032") for p in harness.state()["notify_started"]),
             "listening on the extra characteristic")
    harness.control.ExtraNotify(BIKE, dbus.ByteArray(b"\x01\x02"))
    wait_for(lambda: any(e["kind"] == "gatt_notify" and e["value"] == "0102"
                         for e in harness.capture_events()), "gatt_notify captured")
    wait_for(lambda: status(harness).get("state") == "MCSP not exposed on this connection",
             "MCSP status published")


def test_report_and_capture_contain_everything(harness):
    harness.start_bridge()
    harness.control.Advertise(BIKE, dbus.ByteArray(bytes([0, 0, 2, 0x21, 1, 0])), True)
    enable_mcsp(harness)
    harness.control.Connect(BIKE, True, True, dbus.ByteArray(SNAPSHOT))
    wait_sweep_done(harness)
    harness.control.Disconnect(BIKE)                 # writes the report
    report = wait_for(lambda: (r := harness.report()).get("bikes", {}).get(BIKE)
                      and r["bikes"][BIKE].get("datapoints") and r, "report")

    bike = report["bikes"][BIKE]
    assert bike["device"]["Address"] == BIKE
    assert bike["gatt_values"]["00002a26-0000-1000-8000-00805f9b34fb"]["text"] == "21.12.0"
    assert bike["datapoints"]["0x1806"]["value"] == "21.12.0"
    statuses = {d["status"] for d in bike["datapoints"].values()}
    assert {"SUCCESS", "NO_ROUTE_FOUND"} <= statuses
    assert bike["bike_requests"] == {"0x40AA READ": 1, "0x40A9 WRITE": 1,
                                     "0x4180 READ": 1}
    assert bike["summary"]["by_status"]["SUCCESS"] >= 5
    assert report["run"]["readable_datapoints"] == len(
        [dp for dp in TABLE.values() if dp.readable])
    assert "password" not in json.dumps(report["run"]["options"])
    assert bike["ldi"]["battery_soc"] == 87
    assert report["adverts"][BIKE]["pairable"] is False
    assert report["adapter"]["Address"] == "00:11:22:33:44:55"

    kinds = {e["kind"] for e in harness.capture_events()}
    assert {"run_start", "adapter", "advert", "device", "device_changed", "bike_connected",
            "gatt_inventory", "gatt_read", "tx", "rx", "rx_command", "datapoint",
            "bike_request", "ldi", "milestone", "bike_offline"} <= kinds
    milestones = bike["sessions"][-1]["milestones"]
    assert {"notify_on", "first_rx", "peer_handshake", "handshake_sent", "first_success",
            "sweep_started", "sweep_done", "disconnected"} <= set(milestones)


def test_not_ready_bike_gets_the_start_up_set_again(harness):
    # The first ~400 requests (more than the whole start-up set) say NOT_READY.
    connect_mcsp_bike(harness, not_ready_requests=400)
    wait_for(lambda: "bike not ready" in status(harness).get("state", ""), "retry scheduled")
    wait_for(lambda: (harness.mqtt.json(MCSP_TOPIC) or {}).get("drive_unit_software")
             == "21.12.0", "values after the bike became ready", timeout=60)
    assert "first_not_ready" in status(harness)["milestones"]


def test_someone_elses_bike_is_never_touched_outside_the_pairing_window(harness):
    harness.start_bridge(open_pairing_on_start=False)
    stranger = "E0:00:00:00:00:09"
    # Even a bike that says "ready to pair" is left alone unless the user
    # opened the pairing window.
    harness.control.Advertise(stranger, dbus.ByteArray(bytes([0, 0, 2, 0x21, 1, 1])), False)
    wait_for(lambda: any(e["kind"] == "advert" and e["address"] == stranger
                         for e in harness.capture_events()), "advert recorded")
    time.sleep(1)
    assert harness.state()["connect_calls"] == [] and harness.state()["pair_calls"] == []


def test_sweep_runs_once_per_firmware(harness):
    connect_mcsp_bike(harness)
    wait_sweep_done(harness)
    harness.control.Disconnect(BIKE)
    wait_for(lambda: harness.mqtt.get(f"bosch_ebike/{SLUG}/availability") == "offline",
             "offline")
    reads_before = sum(m["op"] == 0 for m in harness.mcsp_log()["messages"])
    harness.control.Connect(BIKE, True, True, dbus.ByteArray(SNAPSHOT))
    wait_for(lambda: "already done for this firmware" in status(harness).get("state", ""),
             "second session skips the sweep")
    reads_after = sum(m["op"] == 0 for m in harness.mcsp_log()["messages"])
    assert reads_after - reads_before < 200          # start-up set only, no sweep


def test_sweep_pauses_while_the_bike_is_moving(harness):
    harness.start_bridge()
    enable_mcsp(harness)
    moving = SNAPSHOT + field(1, 1500)                # 15 km/h
    harness.control.Connect(BIKE, True, True, dbus.ByteArray(moving))
    wait_for(lambda: status(harness).get("state") == "sweep paused (bike moving)",
             "sweep paused", timeout=30)
    harness.control.Notify(BIKE, dbus.ByteArray(field(1, 0)))     # stopped
    wait_sweep_done(harness)


def test_mcsp_can_be_switched_off(harness):
    harness.start_bridge(mcsp=False)
    enable_mcsp(harness)
    harness.control.Connect(BIKE, True, True, dbus.ByteArray(SNAPSHOT))
    wait_for(lambda: status(harness).get("state") == "MCSP disabled in options",
             "disabled status")
    assert harness.mcsp_log()["messages"] == []


# ── Home Assistant finds the bike by itself ──────────────────────────────────

from test_supervisor import FakeSupervisor  # noqa: E402

NEW_BIKE = "D0:00:00:00:00:07"
NEW_SLUG = "d0_00_00_00_00_07"
READY = dbus.ByteArray(bytes([0, 0, 2, 0x21, 1, 1]))       # advert: pairable
NOT_READY = dbus.ByteArray(bytes([0, 0, 2, 0x21, 1, 0]))


def test_scans_by_itself_while_no_bike_is_connected(harness):
    harness.start_bridge(open_pairing_on_start=False)
    log = wait_for(lambda: (d := harness.state()["discovery"]) and ["stop"] in d and d,
                   "a short scan", timeout=10)
    # The RSSI filter makes BlueZ report every advert (not only 8 dBm changes).
    assert ["filter", {"Transport": "le", "RSSI": "-127"}] in log and ["start"] in log
    # The result is visible in the add-on log, not only in the capture.
    wait_for(lambda: "Scan done: no Bosch eBike heard" in harness.bridge_log.read_text(),
             "scan summary in the log")


def test_scan_summary_names_the_bike_it_heard(harness):
    harness.start_bridge(open_pairing_on_start=False,
                         env={"BOSCH_SCAN_FOR_SEC": "3"})
    wait_for(lambda: "Scanning for Bosch eBikes" in harness.bridge_log.read_text(), "scan")
    harness.control.Advertise(NEW_BIKE, NOT_READY, False)
    wait_for(lambda: f"heard {NEW_BIKE}" in harness.bridge_log.read_text()
             and "not in pairing mode" in harness.bridge_log.read_text(),
             "bike named in the scan summary", timeout=10)


def test_start_pairing_pairs_a_bike_that_is_ready_and_explores_it(harness):
    harness.start_bridge()                       # pairing window open
    enable_mcsp(harness, address=NEW_BIKE)
    harness.control.Advertise(NEW_BIKE, READY, False)
    wait_for(lambda: harness.state()["pair_calls"], "Pair() call")
    wait_for(lambda: (harness.mqtt.json(f"bosch_ebike/{NEW_SLUG}/mcsp") or {})
             .get("motor_power") == 250, "MCSP values from the newly paired bike")
    # Remembered for next time.
    state = json.loads((harness.tmp / "state" / "explorer_state.json").read_text())
    assert state[NEW_BIKE]["adopted"]


def test_a_bike_not_in_pairing_mode_is_only_announced(harness):
    sup = FakeSupervisor()
    try:
        harness.start_bridge(env={"SUPERVISOR_URL": sup.url, "SUPERVISOR_TOKEN": "tok"})
        harness.control.Advertise(NEW_BIKE, NOT_READY, False)
        # It shows up in the (single, evolving) setup notification.
        note = wait_for(lambda: [b for m, p, _, b in sup.requests
                                 if m == "POST" and b.get("notification_id") == "bosch_ebike_setup"
                                 and NEW_BIKE in b.get("message", "")], "setup notice")[-1]
        assert "not in pairing mode" in note["message"]
        time.sleep(1)
        assert harness.state()["pair_calls"] == [] and harness.state()["connect_calls"] == []
    finally:
        sup.server.shutdown()


# ── The whole journey: no bike → nearby → pick → paired → status → firmware ──

import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

FIRMWARE_PAGE = """<html><body><h2>Current software versions</h2>
<p>Performance Line CX smart system: 21.12.0<br>Batteries smart system: 21.12.0</p>
<p>System Controller: 21.11.0<br>Kiox 400C: 21.11.0</p></body></html>"""


class PageServer:
    """Serves a firmware version page like Bosch's (synthetic content)."""

    def __init__(self, body):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *args):
                pass
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/ca/service/software-update"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def notes(sup, notification_id):
    """Notifications shown (created) with this ID; dismissals don't count."""
    return [b for m, p, _, b in sup.requests
            if m == "POST" and p.endswith("/create")
            and b.get("notification_id") == notification_id]


def test_the_whole_journey_from_no_bike_to_firmware(harness):
    sup, page = FakeSupervisor(), PageServer(FIRMWARE_PAGE)
    try:
        harness.start_bridge(
            open_pairing_on_start=False, firmware_page=page.url,
            env={"SUPERVISOR_URL": sup.url, "SUPERVISOR_TOKEN": "tok",
                 "BOSCH_FIRMWARE_FIRST_SEC": "1"})
        # 1. No bike yet: Setup Status and the setup notification say what to do.
        setup = wait_for(lambda: harness.mqtt.json("bosch_ebike/bridge/setup"), "setup status")
        assert setup["state"].startswith("No bike added yet")
        message = wait_for(lambda: notes(sup, "bosch_ebike_setup"), "setup notice")[-1]["message"]
        assert "This is my bike" in message
        assert "(/config/devices/device/dev123)" in message      # straight to the bridge

        # 2. The bike is switched on: it appears in the Nearby dropdown.
        harness.control.EnableMcsp(NEW_BIKE, json.dumps({"values": {
            **VALUES, "0x1806": "0a07" + b"21.11.0".hex(),
            # A controller Bosch's list doesn't name: must not look "up to date".
            "0x206f": "0a0b" + b"Mini Remote".hex(), "0x206b": "0a06" + b"1.3.24".hex()}}))
        harness.control.Advertise(NEW_BIKE, NOT_READY, False)
        config = wait_for(lambda: (c := harness.mqtt.json(
            "homeassistant/select/bosch_ebike_bridge_nearby/config"))
            and len(c["options"]) > 1 and c, "bike in the dropdown")
        option = next(o for o in config["options"] if o.startswith(NEW_BIKE))

        # 3. Pick it and press Pair selected bike (the pick is the consent).
        harness.mqtt.client.publish("bosch_ebike/bridge/nearby/set", option)
        wait_for(lambda: harness.mqtt.get("bosch_ebike/bridge/nearby/state") == option,
                 "selection reflected")
        harness.mqtt.client.publish("bosch_ebike/bridge/pair_selected/set", "PRESS")
        wait_for(lambda: harness.state()["pair_bondable"], "Pair() finished")
        # Outside the window the adapter is made bondable just for this Pair()
        # (else the kernel stores no bond), then put back.
        assert harness.state()["pair_bondable"] == [True]
        wait_for(lambda: [p for p in harness.state()["property_sets"]
                          if p[0] == "Pairable"][-1] == ["Pairable", False], "Pairable restored")

        # 4. Paired: setup notification dismissed, bike Status and device info.
        wait_for(lambda: [b for m, p, _, b in sup.requests if p.endswith("/dismiss")
                          and b["notification_id"] == "bosch_ebike_setup"], "setup dismissed")
        status_topic = f"bosch_ebike/{NEW_SLUG}/status"
        wait_for(lambda: (harness.mqtt.json(status_topic) or {}).get("state", "")
                 .startswith(("Exploring", "Connected")), "bike status")
        device = wait_for(lambda: (c := harness.mqtt.json(
            f"homeassistant/sensor/bosch_ebike_{NEW_SLUG}_status/config"))
            and {"model", "serial_number", "sw_version"} <= c["device"].keys()
            and c["device"], "device info")
        assert (device["model"], device["serial_number"], device["sw_version"]) == (
            "Performance Line CX", "SN1234", "21.11.0")

        # 5. Firmware: update entity shows installed vs published, one notification.
        fw = wait_for(lambda: (p := harness.mqtt.json(
            f"bosch_ebike/{NEW_SLUG}/firmware/drive_unit")) and p.get("latest_version") and p,
            "firmware update entity", timeout=20)
        assert (fw["installed_version"], fw["latest_version"]) == ("21.11.0", "21.12.0")
        config = harness.mqtt.json(
            f"homeassistant/update/bosch_ebike_{NEW_SLUG}_firmware_drive_unit/config")
        assert "command_topic" not in config                  # never installs
        assert harness.mqtt.get(f"bosch_ebike/{NEW_SLUG}/firmware/drive_unit/available") \
            == "online"
        # Unmatched component: no latest_version (HA rejects null) and unavailable.
        ctl = wait_for(lambda: harness.mqtt.json(f"bosch_ebike/{NEW_SLUG}/firmware/controller"),
                       "controller entity")
        assert ctl["installed_version"] == "1.3.24" and "latest_version" not in ctl
        assert harness.mqtt.get(f"bosch_ebike/{NEW_SLUG}/firmware/controller/available") \
            == "offline"
        fw_note = f"bosch_ebike_firmware_{NEW_SLUG.replace('_', '')}_drive_unit"
        wait_for(lambda: notes(sup, fw_note), "firmware notification")
        time.sleep(0.5)
        assert len(notes(sup, fw_note)) == 1

        # 6. After a restart the same release is evaluated again but not re-announced.
        fw_topic = f"bosch_ebike/{NEW_SLUG}/firmware/drive_unit"
        published = harness.mqtt.count(fw_topic)
        harness.stop_bridge()
        harness.start_bridge(open_pairing_on_start=False, firmware_page=page.url,
                             env={"SUPERVISOR_URL": sup.url, "SUPERVISOR_TOKEN": "tok",
                                  "BOSCH_FIRMWARE_FIRST_SEC": "1"})
        wait_for(lambda: sum(e["kind"] == "firmware_versions"
                             for e in harness.capture_events()) >= 2, "second check", timeout=20)
        wait_for(lambda: harness.mqtt.count(fw_topic) > published + 1, "re-evaluated", timeout=20)
        time.sleep(1)
        assert len(notes(sup, fw_note)) == 1
    finally:
        sup.server.shutdown()
        page.server.shutdown()


def test_pair_selected_without_a_selection_does_nothing(harness):
    harness.start_bridge(open_pairing_on_start=False)
    harness.mqtt.client.publish("bosch_ebike/bridge/pair_selected/set", "PRESS")
    wait_for(lambda: any(e["kind"] == "pair_selected_none" for e in harness.capture_events()),
             "ignored")
    assert harness.state()["pair_calls"] == []


def test_a_leftover_setup_notice_is_cleared_once_a_bike_is_known(harness):
    sup = FakeSupervisor()
    try:
        harness.start_bridge(open_pairing_on_start=False, bikes=[{"address": BIKE}],
                             env={"SUPERVISOR_URL": sup.url, "SUPERVISOR_TOKEN": "tok"})
        wait_for(lambda: [b for m, p, _, b in sup.requests if p.endswith("/dismiss")
                          and b["notification_id"] == "bosch_ebike_setup"], "setup dismissed")
        assert notes(sup, "bosch_ebike_setup") == []
    finally:
        sup.server.shutdown()


def test_firmware_check_off_removes_update_entities(harness):
    harness.start_bridge(open_pairing_on_start=False, firmware_check=False)
    harness.control.EnableMcsp(NEW_BIKE, json.dumps(
        {"values": {**VALUES, "0x1806": "0a07" + b"21.11.0".hex()}}))
    harness.control.Advertise(NEW_BIKE, NOT_READY, False)
    option = wait_for(lambda: next((o for o in (harness.mqtt.json(
        "homeassistant/select/bosch_ebike_bridge_nearby/config") or {}).get("options", [])
        if o.startswith(NEW_BIKE)), None), "bike in the dropdown")
    harness.mqtt.client.publish("bosch_ebike/bridge/nearby/set", option)
    wait_for(lambda: harness.mqtt.get("bosch_ebike/bridge/nearby/state") == option, "picked")
    harness.mqtt.client.publish("bosch_ebike/bridge/pair_selected/set", "PRESS")
    topic = f"homeassistant/update/bosch_ebike_{NEW_SLUG}_firmware_drive_unit/config"
    wait_for(lambda: harness.mqtt.count(topic) >= 1, "update entity removed")
    assert harness.mqtt.get(topic) == ""
    assert harness.mqtt.get(f"bosch_ebike/{NEW_SLUG}/firmware/drive_unit") is None


def test_a_failed_pairing_never_makes_the_bike_ours(harness):
    sup = FakeSupervisor()
    try:
        harness.start_bridge(open_pairing_on_start=False,
                             env={"SUPERVISOR_URL": sup.url, "SUPERVISOR_TOKEN": "tok"})
        harness.control.EnableMcsp(NEW_BIKE, json.dumps({"values": VALUES}))
        harness.control.FailPairing(NEW_BIKE)
        harness.control.Advertise(NEW_BIKE, NOT_READY, False)
        option = wait_for(lambda: next((o for o in (harness.mqtt.json(
            "homeassistant/select/bosch_ebike_bridge_nearby/config") or {}).get("options", [])
            if o.startswith(NEW_BIKE)), None), "bike in the dropdown")
        harness.mqtt.client.publish("bosch_ebike/bridge/nearby/set", option)
        wait_for(lambda: harness.mqtt.get("bosch_ebike/bridge/nearby/state") == option, "picked")
        harness.mqtt.client.publish("bosch_ebike/bridge/pair_selected/set", "PRESS")
        wait_for(lambda: notes(sup, f"bosch_ebike_pair_{NEW_SLUG.replace('_', '')}"),
                 "pairing failed notice")
        time.sleep(1)
        events = harness.capture_events()
        assert not [e for e in events if e["kind"] == "bike_connected"]
        assert not [e for e in events if e["kind"] == "paired"]
        assert NEW_BIKE not in harness.mqtt.json("bosch_ebike/bridge/setup")["attributes"]["bikes"]
        assert harness.mqtt.get(f"bosch_ebike/{NEW_SLUG}/status") is None
        # The link Pair() opened is closed again.
        assert any(p.endswith("dev_D0_00_00_00_00_07") for p in harness.state()["disconnect_calls"])
    finally:
        sup.server.shutdown()


def test_a_bike_repeating_the_same_advert_is_heard(harness):
    """Real BlueZ: the advert data arrives once (or from its cache at start);
    after that the same advert only changes RSSI. The bike must still be heard,
    and the scan summary must list every device heard."""
    harness.control.Advertise(NEW_BIKE, NOT_READY, False)      # cached before start
    harness.start_bridge(open_pairing_on_start=False, env={"BOSCH_SCAN_FOR_SEC": "3"})
    wait_for(lambda: harness.state()["discovery"], "scan started")
    harness.control.Advertise(NEW_BIKE, NOT_READY, False)      # RSSI-only update
    wait_for(lambda: any(o.startswith(NEW_BIKE) for o in (harness.mqtt.json(
        "homeassistant/select/bosch_ebike_bridge_nearby/config") or {}).get("options", [])),
        "bike in the dropdown")
    summary = wait_for(lambda: [e for e in harness.capture_events()
                                if e["kind"] == "scan_devices" and e["devices"]],
                       "scan summary", timeout=15)[0]
    assert summary["devices"][0]["address"] == NEW_BIKE
    assert summary["devices"][0]["manufacturer_ids"] == ["0x02A6"]


def test_a_bike_advertising_only_its_discovery_service_can_be_picked_and_paired(harness):
    """What the real Range VLT CX sends: name, flags and service 0xFE02, no
    manufacturer data. It must appear (pairing mode unknown) and pair when picked."""
    harness.start_bridge(open_pairing_on_start=False, env={"BOSCH_SCAN_FOR_SEC": "3"})
    harness.control.EnableMcsp(NEW_BIKE, json.dumps({"values": VALUES}))
    harness.control.AdvertiseDiscoveryOnly(NEW_BIKE)
    option = wait_for(lambda: next((o for o in (harness.mqtt.json(
        "homeassistant/select/bosch_ebike_bridge_nearby/config") or {}).get("options", [])
        if o.startswith(NEW_BIKE)), None), "bike in the dropdown")
    assert "pairing mode unknown" in option
    assert harness.state()["pair_calls"] == []           # never without consent
    harness.mqtt.client.publish("bosch_ebike/bridge/nearby/set", option)
    wait_for(lambda: harness.mqtt.get("bosch_ebike/bridge/nearby/state") == option, "picked")
    harness.mqtt.client.publish("bosch_ebike/bridge/pair_selected/set", "PRESS")
    wait_for(lambda: harness.state()["pair_bondable"] == [True], "paired")
    wait_for(lambda: (harness.mqtt.json(f"bosch_ebike/{NEW_SLUG}/status") or {})
             .get("state", "").startswith(("Exploring", "Connected")), "bike status")


def test_a_discovery_only_bike_is_not_auto_paired_in_the_window(harness):
    harness.start_bridge(pairing_window=60, env={"BOSCH_SCAN_FOR_SEC": "3"})
    harness.control.AdvertiseDiscoveryOnly(NEW_BIKE)
    harness.control.AdvertiseDiscoveryOnly(NEW_BIKE)
    wait_for(lambda: any(e["kind"] == "advert" for e in harness.capture_events()), "heard")
    time.sleep(1)
    assert harness.state()["pair_calls"] == []           # pairing mode unknown: wait for a pick


def _pick(harness, address):
    option = wait_for(lambda: next((o for o in (harness.mqtt.json(
        "homeassistant/select/bosch_ebike_bridge_nearby/config") or {}).get("options", [])
        if o.startswith(address)), None), "bike in the dropdown")
    harness.mqtt.client.publish("bosch_ebike/bridge/nearby/set", option)
    wait_for(lambda: harness.mqtt.get("bosch_ebike/bridge/nearby/state") == option, "picked")
    harness.mqtt.client.publish("bosch_ebike/bridge/pair_selected/set", "PRESS")


def test_a_quiet_bike_is_paired_on_its_next_advert(harness):
    """The real bike advertised once, then went quiet. Pair selected bike waits
    for its next advertisement instead of pairing with a bike not listening."""
    harness.start_bridge(open_pairing_on_start=False,
                         env={"BOSCH_PAIR_RECENT_SEC": "0", "BOSCH_SCAN_FOR_SEC": "3"})
    harness.control.EnableMcsp(NEW_BIKE, json.dumps({"values": VALUES}))
    harness.control.AdvertiseDiscoveryOnly(NEW_BIKE)
    _pick(harness, NEW_BIKE)
    wait_for(lambda: any(e["kind"] == "pair_armed" for e in harness.capture_events()), "waiting")
    assert harness.mqtt.json("bosch_ebike/bridge/setup")["state"].startswith("Waiting for")
    time.sleep(1)
    assert harness.state()["pair_calls"] == []           # not while it's quiet
    harness.control.AdvertiseDiscoveryOnly(NEW_BIKE)     # it speaks again
    wait_for(lambda: harness.state()["pair_bondable"] == [True], "paired on the advert")


def test_waiting_for_a_quiet_bike_gives_up_and_says_so(harness):
    sup = FakeSupervisor()
    try:
        harness.start_bridge(open_pairing_on_start=False, env={
            "BOSCH_PAIR_RECENT_SEC": "0", "BOSCH_PAIR_WAIT_SEC": "2",
            "SUPERVISOR_URL": sup.url, "SUPERVISOR_TOKEN": "tok"})
        harness.control.AdvertiseDiscoveryOnly(NEW_BIKE)
        _pick(harness, NEW_BIKE)
        wait_for(lambda: any(e["kind"] == "pair_wait_expired"
                             for e in harness.capture_events()), "gave up", timeout=10)
        note = wait_for(lambda: notes(sup, f"bosch_ebike_pair_{NEW_SLUG.replace('_', '')}"),
                        "notification")[-1]
        assert "wasn't heard" in note["message"]
        assert harness.state()["pair_calls"] == []
    finally:
        sup.server.shutdown()


# ── "Connect like the Flow app" ─────────────────────────────────────────────

from datetime import datetime  # noqa: E402

ADVERT = dbus.ByteArray(bytes([0, 0, 2, 0x21, 1, 0]))
FAST = {"BOSCH_MCSP_TIMEOUT_MS": "200", "BOSCH_WINDOW_SEC": "40",
        "BOSCH_MCSP_REFRESH_MS": "1000"}       # the 10-min refresh, due quickly


def events(harness, kind):
    return [e for e in harness.capture_events() if e["kind"] == kind]


def test_mcsp_silent_on_the_ldi_link_stops_after_twenty_unanswered_requests(harness):
    """The first real bike: on the link it opened for LDI, MCSP sent nothing."""
    harness.control.EnableMcsp(BIKE, json.dumps({"values": VALUES,
                                                 "answers_only_app_links": True}))
    harness.start_bridge(open_pairing_on_start=False, env=FAST)
    harness.control.Connect(BIKE, True, True, dbus.ByteArray(SNAPSHOT))
    wait_for(lambda: status(harness).get("state")
             == "bike not answering MCSP on this connection", "gave up", timeout=20)
    assert events(harness, "bike_connected")[0]["opened_by"] == "bike"
    sent = status(harness)["requests_sent"]
    assert sent <= 24                                   # 20 + at most 4 in flight
    time.sleep(3)                                       # past the (1 s) refresh timer
    assert status(harness)["requests_sent"] == sent     # and nothing more
    assert len(events(harness, "milestone")) == len(
        [e for e in events(harness, "milestone")])      # (capture readable)
    assert sum(e["name"] == "silent" for e in events(harness, "milestone")) == 1
    assert "sweep_done" not in status(harness)["milestones"]
    bike_status = wait_for(lambda: (harness.mqtt.json(f"bosch_ebike/{SLUG}/status") or {})
                           .get("state", "").endswith("MCSP not answering on this link"),
                           "status says so")
    assert bike_status


def test_connect_like_the_flow_app_pauses_ldi_and_reaches_mcsp(harness):
    harness.control.EnableMcsp(BIKE, json.dumps({"values": VALUES,
                                                 "answers_only_app_links": True}))
    harness.start_bridge(open_pairing_on_start=False, env=FAST)
    harness.control.Connect(BIKE, True, True, dbus.ByteArray(SNAPSHOT))   # the bike: LDI
    wait_for(lambda: status(harness).get("state")
             == "bike not answering MCSP on this connection", "silent on LDI", timeout=20)
    adverts = len(harness.state()["advertisements"])

    harness.mqtt.client.publish("bosch_ebike/bridge/mcsp_window/set", "PRESS")
    wait_for(lambda: events(harness, "mcsp_window_start"), "window started")
    wait_for(lambda: harness.state()["disconnect_calls"], "LDI link dropped")
    harness.control.Advertise(BIKE, ADVERT, True)                         # bike advertises
    wait_for(lambda: harness.state()["connect_calls"], "connects as central")
    wait_for(lambda: events(harness, "window_replies"), "the bike answers", timeout=20)
    end = wait_for(lambda: events(harness, "mcsp_window_end"), "window done", timeout=40)[0]
    assert end["outcome"] == "replies"
    # Not advertising during the window (else the bike could reconnect for LDI).
    start_t = datetime.fromisoformat(events(harness, "mcsp_window_start")[0]["t"]).timestamp()
    end_t = datetime.fromisoformat(end["t"]).timestamp()
    assert not [t for t in harness.state()["advert_times"] if start_t < t < end_t]
    assert [link["by_us"] for link in harness.state()["links"]] == [False, True]
    assert events(harness, "bike_connected")[-1]["opened_by"] == "add-on"
    wait_for(lambda: len(harness.state()["advertisements"]) > adverts, "advertising again")
    assert status(harness)["values"] > 0
    # Afterwards the bike reconnects for LDI: that link is the bike's, not ours.
    wait_for(lambda: not harness.state()["links"] or True, "")
    harness.control.Disconnect(BIKE)
    time.sleep(0.5)
    harness.control.Connect(BIKE, True, True, dbus.ByteArray(SNAPSHOT))
    wait_for(lambda: len(events(harness, "bike_connected")) >= 3, "LDI back")
    assert events(harness, "bike_connected")[-1]["opened_by"] == "bike"


def test_connect_like_the_flow_app_gives_up_and_restores_advertising(harness):
    harness.control.EnableMcsp(BIKE, json.dumps({"values": VALUES,
                                                 "answers_only_app_links": True}))
    harness.start_bridge(open_pairing_on_start=False, mcsp_connect=False,
                         env={**FAST, "BOSCH_WINDOW_SEC": "3"})
    harness.control.Connect(BIKE, True, True, dbus.ByteArray(SNAPSHOT))
    wait_for(lambda: events(harness, "bike_connected"), "LDI link")
    adverts = len(harness.state()["advertisements"])
    harness.mqtt.client.publish("bosch_ebike/bridge/mcsp_window/set", "PRESS")
    end = wait_for(lambda: events(harness, "mcsp_window_end"), "window ends", timeout=15)[0]
    assert end["outcome"] == "no advertisement heard from the bike"
    wait_for(lambda: len(harness.state()["advertisements"]) > adverts, "advertising again")
    # With automatic reads off, the bike is never connected to by itself.
    harness.control.Advertise(BIKE, ADVERT, True)
    harness.control.Advertise(BIKE, ADVERT, True)
    time.sleep(2)
    assert harness.state()["connect_calls"] == []


# ── 0.7: peek on the bike's advertisement; the add-on's page ────────────────

import urllib.request  # noqa: E402

PEEK = {"BOSCH_MCSP_TIMEOUT_MS": "200", "BOSCH_PEEK_DECIDE_SEC": "8"}


def bike_values(charging):
    return json.dumps({"values": {**VALUES, "0x0089": "0801" if charging else "0800"},
                       "answers_only_app_links": True})


def window_end(harness, timeout=30):
    return wait_for(lambda: [e for e in harness.capture_events()
                             if e["kind"] == "mcsp_window_end"], "read ended", timeout=timeout)[0]


def test_a_peek_at_a_bike_that_is_not_charging_lets_go_at_once(harness):
    """About to ride: connect, see 'no charger', leave the bike to the Flow app."""
    harness.control.EnableMcsp(BIKE, bike_values(charging=False))
    harness.start_bridge(open_pairing_on_start=False, bikes=[{"address": BIKE}], env=PEEK)
    harness.control.Advertise(BIKE, ADVERT, True)
    wait_for(lambda: harness.state()["connect_calls"], "peek connects")
    end = window_end(harness)
    assert end["outcome"].startswith("not charging") and end["seconds"] < 10
    assert "sweep_started" not in status(harness).get("milestones", {})
    wait_for(lambda: harness.state()["disconnect_calls"], "let go")
    harness.control.Advertise(BIKE, ADVERT, True)          # again, right away
    time.sleep(2)
    assert len(harness.state()["connect_calls"]) == 1       # one peek per gap


def test_a_peek_at_a_charging_bike_reads_everything(harness):
    harness.control.EnableMcsp(BIKE, bike_values(charging=True))
    harness.start_bridge(open_pairing_on_start=False, bikes=[{"address": BIKE}], env=PEEK)
    harness.control.Advertise(BIKE, ADVERT, True)
    end = window_end(harness, timeout=60)
    assert end["outcome"] == "replies" and end["reason"] == "charging"
    assert (harness.mqtt.json(MCSP_TOPIC) or {}).get("battery_health") is not None or \
        status(harness)["values"] > 5


def test_the_page_shows_the_bike_and_its_buttons_work(harness):
    harness.control.EnableMcsp(BIKE, bike_values(charging=False))
    harness.start_bridge(open_pairing_on_start=False, env=PEEK)
    harness.control.Connect(BIKE, True, True, dbus.ByteArray(SNAPSHOT))   # LDI link

    def page_state():
        try:
            return json.loads(urllib.request.urlopen("http://127.0.0.1:8099/api/state",
                                                     timeout=2).read())
        except OSError:
            return {}
    bike = wait_for(lambda: next(iter(page_state().get("bikes", [])), None), "bike on the page")
    assert bike["address"] == BIKE and bike["live"] and bike["ldi"]["battery_soc"] == 87
    req = urllib.request.Request("http://127.0.0.1:8099/api/action", method="POST",
                                 data=json.dumps({"action": "settings", "address": BIKE,
                                                  "name": "Range VLT", "battery_wh": 625}).encode())
    urllib.request.urlopen(req, timeout=2)
    renamed = wait_for(lambda: next((b for b in page_state().get("bikes", [])
                                     if b["name"] == "Range VLT"), None), "renamed")
    assert renamed["battery_wh"] == 625
    req = urllib.request.Request("http://127.0.0.1:8099/api/action", method="POST",
                                 data=json.dumps({"action": "read", "address": BIKE}).encode())
    urllib.request.urlopen(req, timeout=2)
    wait_for(lambda: [e for e in harness.capture_events() if e["kind"] == "mcsp_window_start"
                      and e["reason"] == "button"], "Read now started a read")


def test_a_peek_understands_not_charging_sent_as_an_empty_value(harness):
    """Protobuf sends false as nothing: an empty successful answer is 'no charger'."""
    harness.control.EnableMcsp(BIKE, json.dumps({"values": {**VALUES, "0x0089": ""},
                                                 "answers_only_app_links": True}))
    harness.start_bridge(open_pairing_on_start=False, bikes=[{"address": BIKE}], env=PEEK)
    harness.control.Advertise(BIKE, ADVERT, True)
    end = window_end(harness)
    assert end["outcome"].startswith("not charging") and end["seconds"] < 10


def test_the_page_keeps_the_last_full_reading_after_a_restart(harness):
    harness.control.EnableMcsp(BIKE, bike_values(charging=True))
    harness.start_bridge(open_pairing_on_start=False, bikes=[{"address": BIKE}], env=PEEK)
    harness.control.Advertise(BIKE, ADVERT, True)
    assert window_end(harness, timeout=60)["outcome"] == "replies"
    harness.stop_bridge()
    harness.start_bridge(open_pairing_on_start=False, bikes=[{"address": BIKE}], env=PEEK,
                         wait=False)

    def bike():
        try:
            state = json.loads(urllib.request.urlopen("http://127.0.0.1:8099/api/state",
                                                      timeout=2).read())
            return next(iter(state.get("bikes", [])), None)
        except OSError:
            return None
    after = wait_for(lambda: (b := bike()) and b.get("last_good") and b, "page after restart")
    assert after["mcsp"].get("charge_cycles") is not None and after["model"]


def test_the_live_data_link_is_not_asked_again_once_it_stayed_silent(harness):
    harness.control.EnableMcsp(BIKE, json.dumps({"values": VALUES,
                                                 "answers_only_app_links": True}))
    harness.start_bridge(open_pairing_on_start=False, mcsp_connect=False,
                         env={"BOSCH_MCSP_TIMEOUT_MS": "200"})
    harness.control.Connect(BIKE, True, True, dbus.ByteArray(SNAPSHOT))      # LDI link
    wait_for(lambda: status(harness).get("state")
             == "bike not answering MCSP on this connection", "silent once", timeout=20)
    harness.control.Disconnect(BIKE)
    time.sleep(1)
    harness.control.Connect(BIKE, True, True, dbus.ByteArray(SNAPSHOT))      # again
    wait_for(lambda: status(harness).get("state", "").startswith(
        "MCSP disabled on the bike's live-data link"), "not asked again")
    assert status(harness).get("requests_sent") in (None, 0)


def test_range_per_mode_reaches_the_page_and_home_assistant(harness):
    values = {**VALUES, "0x0089": "0801", "0x1857": "0a04" + bytes([80, 46, 31, 23]).hex()}
    harness.control.EnableMcsp(BIKE, json.dumps({"values": values,
                                                 "answers_only_app_links": True}))
    harness.start_bridge(open_pairing_on_start=False, bikes=[{"address": BIKE}], env=PEEK)
    harness.control.Advertise(BIKE, ADVERT, True)
    wait_for(lambda: (harness.mqtt.json(MCSP_TOPIC) or {}).get("range_by_mode")
             == {"ECO": 80, "TOUR+": 46}, "range per mode", timeout=60)
    config = harness.mqtt.json(f"homeassistant/sensor/bosch_ebike_{SLUG}_range_tour/config")
    assert config["name"] == "Range TOUR+" and config["unit_of_measurement"] == "km"


def test_messages_from_the_bike_are_decoded(harness):
    """The real bike's issue list: {1: 5660676} = Bosch code 566004."""
    values = {**VALUES, "0x0089": "0801", "0x109a": "0a050884c0d902"}
    harness.control.EnableMcsp(BIKE, json.dumps({"values": values,
                                                 "answers_only_app_links": True}))
    harness.start_bridge(open_pairing_on_start=False, bikes=[{"address": BIKE}], env=PEEK)
    harness.control.Advertise(BIKE, ADVERT, True)
    state = wait_for(lambda: (s := harness.mqtt.json(MCSP_TOPIC) or {}).get("messages") and s,
                     "messages", timeout=60)
    assert state["messages"][0]["code"] == "566004"
    assert "distracted" in state["bike_messages"]


def test_one_fully_charged_notification_per_charge(harness):
    sup = FakeSupervisor()
    try:
        harness.start_bridge(open_pairing_on_start=False,
                             env={"SUPERVISOR_URL": sup.url, "SUPERVISOR_TOKEN": "tok"})
        charging = field(1, 0) + field(2, 0) + field(10, 97) + field(12, 1234567) + field(22, 1)
        harness.control.Connect(BIKE, True, True, dbus.ByteArray(charging))
        wait_for(lambda: harness.mqtt.json(f"bosch_ebike/{SLUG}/state"), "LDI state")
        assert notes(sup, f"bosch_ebike_full_{SLUG.replace('_', '')}") == []
        harness.control.Notify(BIKE, dbus.ByteArray(field(10, 100)))       # full
        wait_for(lambda: notes(sup, f"bosch_ebike_full_{SLUG.replace('_', '')}"), "notified")
        harness.control.Notify(BIKE, dbus.ByteArray(field(10, 99)))
        harness.control.Notify(BIKE, dbus.ByteArray(field(10, 100)))       # still the same charge
        time.sleep(1)
        assert len(notes(sup, f"bosch_ebike_full_{SLUG.replace('_', '')}")) == 1
    finally:
        sup.server.shutdown()


def test_the_bike_can_read_what_this_accessory_is(harness):
    harness.start_bridge(open_pairing_on_start=False, accessory_model="BHA9000",
                         accessory_serial="")
    info = wait_for(lambda: harness.state()["gatt_app"], "device information served")
    assert info["2a29"] == "Home Assistant"            # manufacturer (default)
    assert info["2a24"] == "BHA9000"                   # model, from the option
    assert info["2a27"] == "HA-4200-PI"                # hardware (default)
    assert info["2a25"] == "HA-334455"                 # serial: end of the adapter address
    assert info["2a28"] == info["2a26"]                # software (shown) = firmware = version
    assert "2a50" not in info and "2a23" not in info   # experiment over: not served
