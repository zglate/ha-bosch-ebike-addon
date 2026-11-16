"""Tests for the Supervisor client (MQTT service lookup, notifications)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import supervisor


class FakeSupervisor:
    """A tiny HTTP stand-in for http://supervisor."""

    def __init__(self):
        self.requests = []
        self.mqtt = {"result": "error", "message": "Service not enabled"}
        self.notify_status = 200          # e.g. 401 when HA's API is not allowed
        self.device_id = "dev123"         # answer to the bridge-device template
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, status, body):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_GET(self):
                fake.requests.append(("GET", self.path, self.headers["Authorization"], None))
                if self.path == "/core/api/":
                    self._reply(fake.notify_status, {"message": "API running."})
                    return
                ok = fake.mqtt.get("result") == "ok"
                self._reply(200 if ok else 400, fake.mqtt)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(("POST", self.path, self.headers["Authorization"], body))
                if self.path == "/core/api/template":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(fake.device_id.encode())
                    return
                if fake.notify_status != 200:
                    self._reply(fake.notify_status, {"message": "Unauthorized"})
                    return
                self._reply(200, [])

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def fake(monkeypatch):
    f = FakeSupervisor()
    monkeypatch.setenv("SUPERVISOR_URL", f.url)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    yield f
    f.server.shutdown()


def test_mqtt_not_registered_gives_the_supervisors_reason(fake):
    assert supervisor.mqtt_service() == (None, "Service not enabled")


def test_mqtt_registered(fake):
    fake.mqtt = {"result": "ok", "data": {"host": "core-mosquitto", "port": 1883,
                                          "username": "addons", "password": "pw"}}
    data, reason = supervisor.mqtt_service()
    assert (data["host"], data["username"], reason) == ("core-mosquitto", "addons", "")
    assert fake.requests[-1][:3] == ("GET", "/services/mqtt", "Bearer tok")


def test_notification_create_and_dismiss(fake):
    assert supervisor.notify("mqtt", "Title", "Body") is True
    supervisor.dismiss("mqtt")
    (_, create, _, body), (_, dismiss, _, dbody) = fake.requests
    assert create == "/core/api/services/persistent_notification/create"
    assert body == {"notification_id": "bosch_ebike_mqtt", "title": "Title",
                    "message": "Body"}
    assert dismiss.endswith("/dismiss") and dbody == {"notification_id": "bosch_ebike_mqtt"}


def test_no_supervisor_is_harmless(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert supervisor.mqtt_service() == (None, "Supervisor not reachable")
    assert supervisor.notify("x", "t", "m") is False


def test_unreachable_supervisor_is_harmless(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    monkeypatch.setenv("SUPERVISOR_URL", "http://127.0.0.1:9")     # nothing listens
    assert supervisor.mqtt_service() == (None, "Supervisor not reachable")


def test_messages_render():
    assert "Service not enabled" in supervisor.MQTT_MISSING.format(reason="Service not enabled")
    text = supervisor.BIKE_FOUND.format(name="eBike 2345", address="A4:0D:BC:01:23:45")
    assert '- address: "A4:0D:BC:01:23:45"' in text


def test_a_refused_notification_is_reported_not_counted_as_shown(fake, caplog):
    fake.notify_status = 401
    assert supervisor.notify("setup", "Bosch eBike: add your bike", "Body") is False
    assert "HTTP 401" in caplog.text and "Unauthorized" in caplog.text
    assert "NOT reachable" in supervisor.check_core_api()


def test_core_api_check_and_template(fake):
    assert supervisor.check_core_api().startswith("Home Assistant API reachable")
    assert supervisor.render_template(supervisor.BRIDGE_DEVICE_TEMPLATE) == "dev123"
