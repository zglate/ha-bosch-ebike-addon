"""
Talking to the Home Assistant Supervisor from inside the add-on.

- MQTT connection details: the broker app (Mosquitto) registers them with
  the Supervisor; we ask for them. While none are registered the Supervisor
  answers "Service not enabled" (Mosquitto briefly drops its registration
  every time it restarts), so callers retry.
- Notifications: shown in Home Assistant's sidebar, so setup problems are
  visible without reading the add-on log. Needs `homeassistant_api: true`.

Every call fails soft: outside Home Assistant (tests, a laptop) there is no
Supervisor, and that must never stop the add-on.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

TIMEOUT_SEC = 10


def _base() -> str:
    return os.environ.get("SUPERVISOR_URL", "http://supervisor")


def _request(method: str, path: str, body: dict | None = None) -> dict | None:
    """Call the Supervisor; return the parsed JSON body, or None on failure."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        _base() + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
    except (urllib.error.URLError, OSError) as exc:
        log.debug("Supervisor %s %s failed: %s", method, path, exc)
        return None
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return None


def mqtt_service() -> tuple[dict | None, str]:
    """The broker's registered connection details, and a reason if missing."""
    reply = _request("GET", "/services/mqtt")
    if reply is None:
        return None, "Supervisor not reachable"
    if reply.get("result") == "ok" and reply.get("data", {}).get("host"):
        return reply["data"], ""
    return None, str(reply.get("message", "no MQTT service registered"))


def notify(notification_id: str, title: str, message: str) -> bool:
    """Show (or replace) a notification in Home Assistant."""
    reply = _request("POST", "/core/api/services/persistent_notification/create",
                     {"notification_id": f"bosch_ebike_{notification_id}",
                      "title": title, "message": message})
    return reply is not None


def dismiss(notification_id: str) -> None:
    _request("POST", "/core/api/services/persistent_notification/dismiss",
             {"notification_id": f"bosch_ebike_{notification_id}"})


# ── Messages (kept here so they're easy to review and test) ──────────────────

MQTT_MISSING = """\
The **Bosch eBike** app can't connect to an MQTT broker yet, so its entities \
can't appear. It keeps checking every 30 seconds and carries on by itself.

Supervisor says: *{reason}*

1. Open the [Mosquitto broker app](\
https://my.home-assistant.io/redirect/supervisor_app/?app=core_mosquitto), \
check its **Log** for *"Successfully send service information to the \
Supervisor"*. If it says *failed*, or the line is missing, **restart \
Mosquitto**.
2. Not using Mosquitto? Enter your broker as `mqtt_host`, `mqtt_username` \
and `mqtt_password` in this app's **Configuration** tab.
"""

BLUETOOTH_MISSING = """\
The **Bosch eBike** app can't reach Bluetooth on this host: *{reason}*

Check **Settings → System → Hardware** for a Bluetooth adapter and that the \
`adapter` option matches it (usually `hci0`), then restart the app.
"""

PAIRING_FAILED = """\
Pairing with the Bosch eBike `{address}` didn't complete: *{reason}*

Make sure the bike is on and close to Home Assistant, then pick it in \
**Nearby Bosch eBikes** and press **Pair selected bike** again (or put the \
bike in pairing mode and press **Start pairing**).
"""

BIKE_FOUND = """\
**{name}** connected over Bluetooth (address `{address}`). It's remembered \
and reconnects by itself; nothing else is needed.

Optional, to give it a name or a different battery size, add this to the \
app's **Configuration → bikes**:

```yaml
- address: "{address}"
  name: My eBike
  battery_wh: 800
```
"""
