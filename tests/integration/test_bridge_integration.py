"""
End-to-end tests: the real bridge.py against a fake BlueZ and a real MQTT broker.

Each test starts a private D-Bus daemon, mosquitto, fake_bluez.py and the
bridge, then drives the fake bike and checks what reaches MQTT.

These need Linux with dbus-python, dbus-daemon and mosquitto, so they run in
the test container (see scripts/test.sh) and are skipped elsewhere.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

dbus = pytest.importorskip("dbus")
if not (shutil.which("dbus-daemon") and shutil.which("mosquitto")):
    pytest.skip("needs dbus-daemon and mosquitto", allow_module_level=True)

import paho.mqtt.client as mqtt  # noqa: E402

from protobuf_helpers import field  # noqa: E402

HERE = Path(__file__).resolve().parent
BRIDGE = HERE.parents[1] / "bosch_ebike_ldi" / "app" / "bridge.py"
LDI_SERVICE = "0000eb20-eaa2-11e9-81b4-2a2ae2dbcce4"
MQTT_PORT = 18883

BIKE = "A4:0D:BC:01:23:45"
BIKE_PATH = "/org/bluez/hci0/dev_A4_0D_BC_01_23_45"
STATE_TOPIC = "bosch_ebike/a4_0d_bc_01_23_45/state"
AVAIL_TOPIC = "bosch_ebike/a4_0d_bc_01_23_45/availability"
STRANGER = "11:22:33:44:55:66"   # e.g. one of Home Assistant's own sensors

BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""

SNAPSHOT = (field(1, 0) + field(2, 0) + field(5, 0) + field(10, 87)
            + field(12, 1234567) + field(22, 1))


def wait_for(check, what, timeout=10.0):
    """Poll `check` until it returns something truthy, or fail."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class MqttRecorder:
    """Keeps the latest payload of every topic on the broker."""

    def __init__(self, port):
        self.messages = {}
        self.counts = {}
        self._lock = threading.Lock()
        self.client = mqtt.Client(client_id="test-recorder")
        self.client.on_connect = lambda c, *_: c.subscribe("#")
        self.client.on_message = self._on_message
        self.client.connect("127.0.0.1", port)
        self.client.loop_start()

    def _on_message(self, client, userdata, msg):
        with self._lock:
            self.messages[msg.topic] = msg.payload.decode()
            self.counts[msg.topic] = self.counts.get(msg.topic, 0) + 1

    def count(self, topic):
        with self._lock:
            return self.counts.get(topic, 0)

    def get(self, topic):
        with self._lock:
            return self.messages.get(topic)

    def json(self, topic):
        payload = self.get(topic)
        return json.loads(payload) if payload else None

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()


class Harness:
    """Owns all processes for one test."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.procs = []
        self.bridge = None
        self.bridge_log = tmp_path / "bridge.log"

        sock = tmp_path / "bus"
        conf = tmp_path / "bus.conf"
        conf.write_text(BUS_CONFIG.format(socket=sock))
        self._spawn(["dbus-daemon", "--config-file", str(conf), "--nofork"])
        wait_for(sock.exists, "dbus-daemon socket")
        self.env = {**os.environ, "DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={sock}"}

        self._spawn(["mosquitto", "-p", str(MQTT_PORT)])
        wait_for(lambda: _port_open(MQTT_PORT), "mosquitto")
        self.mqtt = MqttRecorder(MQTT_PORT)

        fake = self._spawn([sys.executable, str(HERE / "fake_bluez.py")],
                           stdout=subprocess.PIPE, text=True)
        assert "ready" in fake.stdout.readline()
        bus = dbus.bus.BusConnection(self.env["DBUS_SYSTEM_BUS_ADDRESS"])
        self.control = dbus.Interface(bus.get_object("org.bluez", "/test"),
                                      "test.FakeBluez")

    def _spawn(self, cmd, **kwargs):
        env = kwargs.pop("env", getattr(self, "env", None))
        proc = subprocess.Popen(cmd, env=env, **kwargs)
        self.procs.append(proc)
        return proc

    def start_bridge(self, pairing_window=60, release_ms=600_000, env=None, wait=True,
                     **options):
        # Most tests exercise pairing, so open the window at start like the
        # add-on used to; the default (closed) has its own test.
        opts = {"mqtt_host": "127.0.0.1", "mqtt_port": MQTT_PORT,
                "log_level": "debug", "open_pairing_on_start": True,
                "raw_entities": True, **options}          # tests cover the debug entities too
        opts = {k: v for k, v in opts.items() if v is not None}
        opts_path = self.tmp / "options.json"
        opts_path.write_text(json.dumps(opts))
        self.capture_dir = self.tmp / "capture"
        env = {**self.env, "LDI_PAIRING_WINDOW_SEC": str(pairing_window),
               "BOSCH_CAPTURE_DIR": str(self.capture_dir),
               "BOSCH_STATE_DIR": str(self.tmp / "state"),
               "BOSCH_MCSP_SEND_GAP_MS": "1",
               "BOSCH_SCAN_FOR_SEC": "1",
               "BOSCH_PAIR_RECENT_SEC": "60",     # tests pick right after an advert
               "BOSCH_MCSP_RELEASE_MS": str(release_ms), **(env or {})}
        log = open(self.bridge_log, "w")
        self.bridge = self._spawn([sys.executable, "-u", str(BRIDGE), str(opts_path)],
                                  env=env, stdout=log, stderr=subprocess.STDOUT)
        if not wait:
            return
        wait_for(lambda: self.state()["advertisements"], "first advertisement")
        wait_for(lambda: self.mqtt.get("bosch_ebike/bridge/availability") == "online",
                 "bridge online on MQTT")

    def stop_bridge(self):
        """Stop the add-on like a restart would (its state dir is kept)."""
        self.bridge.terminate()
        self.bridge.wait(timeout=30)              # ARM emulation is slow

    def state(self):
        return json.loads(self.control.State())

    def mcsp_log(self):
        return json.loads(self.control.McspLog())

    def report(self):
        paths = sorted(self.capture_dir.glob("run-*/report.json"))
        return json.loads(paths[-1].read_text()) if paths else {}

    def capture_events(self):
        events = []
        for path in sorted(self.capture_dir.glob("run-*/capture-*.jsonl")):
            events += [json.loads(l) for l in path.read_text().splitlines() if l]
        return events

    def close(self):
        self.mqtt.stop()
        for proc in reversed(self.procs):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if self.bridge_log.exists():
            print("---- bridge log ----\n" + self.bridge_log.read_text())


def _port_open(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture
def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    h.close()


def test_startup_registers_agent_and_pairing_advertisement(harness):
    harness.start_bridge()
    state = harness.state()

    adv = state["advertisements"][-1]
    assert adv["Type"] == "peripheral"
    assert adv["SolicitUUIDs"] == [LDI_SERVICE]
    assert adv["Discoverable"] is True
    assert state["default_agent"] == "/org/bosch_ebike_bridge/agent"
    assert state["agent_capability"] == "NoInputNoOutput"
    assert ["Powered", True] in state["property_sets"]
    assert ["Pairable", True] in state["property_sets"]
    # The adapter itself is never made discoverable (classic Bluetooth).
    assert not [p for p in state["property_sets"] if p[0] == "Discoverable"]


def test_bike_data_reaches_home_assistant(harness):
    harness.start_bridge()
    harness.control.Connect(BIKE, True, True, dbus.ByteArray(SNAPSHOT))

    state = wait_for(lambda: harness.mqtt.json(STATE_TOPIC), "first state")
    assert state["battery_soc"] == 87
    assert state["odometer_km"] == 1234.567
    assert state["charger_connected"] is True
    assert state["energy_remaining_wh"] == 696        # 87 % of 800 Wh
    assert state["last_seen"].endswith("+00:00")
    assert harness.mqtt.get(AVAIL_TOPIC) == "online"
    assert harness.mqtt.get(
        "homeassistant/sensor/bosch_ebike_a4_0d_bc_01_23_45_battery_soc/config")

    # A sparse notification is merged into the running state.
    harness.control.Notify(BIKE, dbus.ByteArray(field(10, 86) + field(5, 150)))
    state = wait_for(
        lambda: (s := harness.mqtt.json(STATE_TOPIC)) and s["battery_soc"] == 86 and s,
        "updated state")
    assert state["rider_power"] == 150
    assert state["odometer_km"] == 1234.567

    harness.control.Disconnect(BIKE)
    wait_for(lambda: harness.mqtt.get(AVAIL_TOPIC) == "offline", "bike offline")


def test_home_assistants_own_devices_are_left_alone(harness):
    harness.start_bridge()
    for _ in range(4):  # more than the stale-bond threshold
        harness.control.Connect(STRANGER, False, False, dbus.ByteArray(b""))
        harness.control.Disconnect(STRANGER)
    harness.control.Connect(STRANGER, False, True, dbus.ByteArray(b""))
    time.sleep(1.0)

    assert harness.state()["removed_devices"] == []
    assert not any("11_22_33_44_55_66" in t for t in harness.mqtt.messages)
    assert "LDI characteristic" not in harness.bridge_log.read_text()


def private_advertising(harness):
    return "SolicitUUIDs" not in harness.state()["advertisements"][-1]


def test_suspected_stale_bond_is_reported_never_removed(harness):
    harness.control.AddBondedBike(BIKE)
    harness.start_bridge(pairing_window=2)
    wait_for(lambda: private_advertising(harness), "window to close", timeout=8)

    for _ in range(3):  # connects that drop before services resolve
        harness.control.Connect(BIKE, True, False, dbus.ByteArray(b""))
        harness.control.Disconnect(BIKE)

    wait_for(lambda: any(e["kind"] == "stale_bond_suspected"
                         for e in harness.capture_events()), "stale bond reported")
    time.sleep(0.5)
    assert harness.state()["removed_devices"] == []     # the bond is left alone
    assert private_advertising(harness)                  # no window opened by itself


def test_pairing_window_stays_closed_at_start_by_default(harness):
    harness.start_bridge(open_pairing_on_start=False)
    assert private_advertising(harness)
    assert harness.mqtt.get("bosch_ebike/bridge/pairing/state") == "off"
    assert not harness.control.AskPairing(STRANGER)
    assert ["Pairable", True] not in harness.state()["property_sets"]


def test_pairing_is_only_accepted_in_the_window(harness):
    harness.control.AddBondedBike(BIKE)
    harness.start_bridge(pairing_window=2)
    assert harness.control.AskPairing(STRANGER)          # window open at start

    wait_for(lambda: private_advertising(harness),
             "private advertising after the window", timeout=8)
    assert not harness.control.AskPairing(STRANGER)
    assert "org.bluez.Error.Rejected" in harness.state()["agent_errors"]
    assert harness.control.AskPairing(BIKE)              # known bike may re-pair
    # Pairable is put back the way it was found (False in the fake).
    assert harness.state()["property_sets"][-1] == ["Pairable", False]

    # The "Start pairing" button in HA reopens the window.
    harness.mqtt.client.publish("bosch_ebike/bridge/pairing/set", "PRESS")
    wait_for(lambda: not private_advertising(harness),
             "pairing advertisement after button press")
    assert harness.control.AskPairing(STRANGER)
    assert harness.state()["property_sets"][-1] == ["Pairable", True]


def test_pairing_gate_holds_without_private_advertising(harness):
    harness.start_bridge(pairing_window=2, private_advertising=False)
    wait_for(lambda: harness.mqtt.get("bosch_ebike/bridge/pairing/state") == "off",
             "window to close", timeout=8)
    assert not harness.control.AskPairing(STRANGER)
    # ...but the bridge stays visible to Flow apps, as configured.
    assert not private_advertising(harness)


def test_bike_is_keyed_by_identity_address_not_object_path(harness):
    rpa = "7A:11:22:33:44:55"   # random address the bike first connects with
    harness.start_bridge()
    harness.control.Connect(rpa, True, False, dbus.ByteArray(SNAPSHOT))
    harness.control.SetAddress(rpa)             # BlueZ resolved the identity
    harness.control.Connect(rpa, True, True, dbus.ByteArray(SNAPSHOT))

    wait_for(lambda: harness.mqtt.json("bosch_ebike/c0_ff_ee_00_00_01/state"),
             "state under the identity address")
    assert not any("7a_11_22" in topic for topic in harness.mqtt.messages)


def test_each_notification_is_published_once_after_reconnects(harness):
    harness.start_bridge()
    for _ in range(3):
        harness.control.Connect(BIKE, True, True, dbus.ByteArray(SNAPSHOT))
        wait_for(lambda: harness.mqtt.get(AVAIL_TOPIC) == "online", "online")
        harness.control.Disconnect(BIKE)
        wait_for(lambda: harness.mqtt.get(AVAIL_TOPIC) == "offline", "offline")
    harness.control.Connect(BIKE, True, True, dbus.ByteArray(SNAPSHOT))
    wait_for(lambda: harness.mqtt.get(AVAIL_TOPIC) == "online", "online")
    time.sleep(0.5)

    before = harness.mqtt.count(STATE_TOPIC)
    harness.control.Notify(BIKE, dbus.ByteArray(field(10, 50)))
    wait_for(lambda: harness.mqtt.count(STATE_TOPIC) > before, "state publish")
    time.sleep(0.5)
    assert harness.mqtt.count(STATE_TOPIC) == before + 1


def test_recovers_after_bluetoothd_restart(harness):
    harness.start_bridge()
    harness.control.Connect(BIKE, True, True, dbus.ByteArray(SNAPSHOT))
    wait_for(lambda: harness.mqtt.get(AVAIL_TOPIC) == "online", "bike online")
    adverts = len(harness.state()["advertisements"])

    harness.control.RestartDaemon()
    wait_for(lambda: harness.state()["agent_registrations"] >= 2, "agent re-registered")
    wait_for(lambda: len(harness.state()["advertisements"]) > adverts,
             "advertisement re-registered")
    assert harness.mqtt.get(AVAIL_TOPIC) == "offline"


def test_forget_button_removes_only_bike_bonds_and_reopens_pairing(harness):
    harness.control.AddBondedBike(BIKE)
    harness.start_bridge(pairing_window=2)
    harness.control.Connect(BIKE, True, True, dbus.ByteArray(SNAPSHOT))
    wait_for(lambda: harness.mqtt.get(AVAIL_TOPIC) == "online", "bike online")
    harness.control.Connect(STRANGER, False, True, dbus.ByteArray(b""))
    wait_for(lambda: private_advertising(harness), "window to close", timeout=8)

    harness.mqtt.client.publish("bosch_ebike/bridge/forget/set", "PRESS")
    removed = wait_for(lambda: harness.state()["removed_devices"], "bond removal")
    assert removed == [BIKE_PATH]                    # never HA's other device
    wait_for(lambda: harness.mqtt.get(AVAIL_TOPIC) == "offline", "bike offline")
    wait_for(lambda: not private_advertising(harness), "pairing window reopened")


def test_readvertises_after_adapter_power_cycle(harness):
    harness.start_bridge()
    before = len(harness.state()["advertisements"])
    harness.control.PowerCycle()
    wait_for(lambda: len(harness.state()["advertisements"]) > before,
             "re-advertisement after power cycle")


def test_stop_is_clean_and_marks_bridge_offline(harness):
    harness.start_bridge()
    harness.bridge.send_signal(signal.SIGTERM)
    assert harness.bridge.wait(timeout=5) == 0
    wait_for(lambda: harness.mqtt.get("bosch_ebike/bridge/availability") == "offline",
             "bridge offline on MQTT")


# ── Setup problems are shown in Home Assistant ────────────────────────────────

from test_supervisor import FakeSupervisor  # noqa: E402


@pytest.fixture
def fake_supervisor():
    sup = FakeSupervisor()
    yield sup
    sup.server.shutdown()


def supervisor_env(sup):
    return {"SUPERVISOR_URL": sup.url, "SUPERVISOR_TOKEN": "tok",
            "BOSCH_MQTT_RETRY_SEC": "0.3"}


def posted(sup, path_end):
    return [body for method, path, _, body in sup.requests
            if method == "POST" and path.endswith(path_end)]


def test_waits_for_the_mqtt_broker_with_a_notification(harness, fake_supervisor):
    # No broker in the options, and Mosquitto hasn't registered yet.
    harness.start_bridge(mqtt_host=None, mqtt_port=None, wait=False,
                         env=supervisor_env(fake_supervisor))
    notes = wait_for(lambda: posted(fake_supervisor, "/create"), "notification")
    assert notes[0]["notification_id"] == "bosch_ebike_mqtt"
    assert "Service not enabled" in notes[0]["message"]
    time.sleep(1)
    assert harness.bridge.poll() is None                     # still running, waiting

    # Mosquitto registers: the add-on carries on by itself and clears the note.
    fake_supervisor.mqtt = {"result": "ok", "data": {
        "host": "127.0.0.1", "port": MQTT_PORT, "username": "", "password": ""}}
    wait_for(lambda: harness.mqtt.get("bosch_ebike/bridge/availability") == "online",
             "bridge online after the broker appeared")
    assert posted(fake_supervisor, "/dismiss") == [{"notification_id": "bosch_ebike_mqtt"}]


def test_a_new_bike_is_announced_with_its_address(harness, fake_supervisor):
    harness.start_bridge(env=supervisor_env(fake_supervisor))
    harness.control.Connect(BIKE, True, True, dbus.ByteArray(SNAPSHOT))
    note = wait_for(lambda: [n for n in posted(fake_supervisor, "/create")
                             if n["notification_id"] == "bosch_ebike_bike_a40dbc012345"],
                    "bike notification")[0]
    assert f'- address: "{BIKE}"' in note["message"]
