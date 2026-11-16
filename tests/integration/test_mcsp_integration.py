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
    state = wait_for(lambda: (s := harness.mqtt.json(MCSP_TOPIC))
                     and s.get("battery_capacity") and s.get("assist_mode") == "TOUR+" and s,
                     "curated MCSP values")
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


def test_connects_as_central_to_an_advertising_bike(harness):
    harness.start_bridge(bikes=[{"address": SECOND_BIKE, "name": "Second"}])
    enable_mcsp(harness, address=SECOND_BIKE)
    harness.control.Advertise(SECOND_BIKE, dbus.ByteArray(bytes([0, 0, 2, 0x21, 1, 1])))
    wait_for(lambda: harness.state()["connect_calls"], "Connect() call")
    topic = "bosch_ebike/d0_00_00_00_00_02/mcsp"
    wait_for(lambda: (harness.mqtt.json(topic) or {}).get("motor_power") == 250,
             "MCSP values over the central connection")
    assert len(harness.state()["connect_calls"]) == 1          # not repeated


def test_other_notifying_characteristics_are_recorded(harness):
    harness.start_bridge()
    harness.control.Connect(BIKE, True, True, dbus.ByteArray(SNAPSHOT))
    wait_for(lambda: any(p.endswith("char0032") for p in harness.state()["notify_started"]),
             "listening on the extra characteristic")
    harness.control.ExtraNotify(BIKE, dbus.ByteArray(b"\x01\x02"))
    wait_for(lambda: any(e["kind"] == "gatt_notify" and e["value"] == "0102"
                         for e in harness.capture_events()), "gatt_notify captured")
    assert status(harness)["state"] == "MCSP not exposed on this connection"


def test_report_and_capture_contain_everything(harness):
    harness.start_bridge()
    harness.control.Advertise(BIKE, dbus.ByteArray(bytes([0, 0, 2, 0x21, 1, 0])))
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


def test_someone_elses_bike_is_never_connected(harness):
    harness.start_bridge()
    stranger = "E0:00:00:00:00:09"
    harness.control.Advertise(stranger, dbus.ByteArray(bytes([0, 0, 2, 0x21, 1, 1])))
    wait_for(lambda: any(e["kind"] == "connect_skipped" for e in harness.capture_events()),
             "skip recorded")
    time.sleep(1)
    assert harness.state()["connect_calls"] == []


def test_bike_is_released_after_exploring_and_not_reconnected(harness):
    # Ordinary retry gaps off, so only the post-release hold-off can stop a reconnect.
    harness.start_bridge(bikes=[{"address": SECOND_BIKE}], release_ms=500,
                         env={"BOSCH_CONNECT_RETRY_SEC": "0", "BOSCH_CONNECT_GAP_SEC": "0"})
    enable_mcsp(harness, address=SECOND_BIKE)
    payload = dbus.ByteArray(bytes([0, 0, 2, 0x21, 1, 1]))
    harness.control.Advertise(SECOND_BIKE, payload)
    wait_for(lambda: harness.state()["disconnect_calls"], "release", timeout=90)
    # The bike keeps advertising, but the add-on holds off for hours.
    harness.control.Advertise(SECOND_BIKE, dbus.ByteArray(bytes([0, 0, 2, 0x21, 1, 0])))
    time.sleep(2)
    assert len(harness.state()["connect_calls"]) == 1


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
