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


def _call(method: str, path: str, body: dict | None = None) -> tuple[int | None, bytes]:
    """Call the Supervisor: (HTTP status, raw body). Status None = no answer."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None, b"no SUPERVISOR_TOKEN"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        _base() + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        return None, str(exc).encode()


def _request(method: str, path: str, body: dict | None = None) -> dict | None:
    """Call the Supervisor; the parsed JSON body (also for errors), or None."""
    status, raw = _call(method, path, body)
    if status is None:
        log.debug("Supervisor %s %s failed: %s", method, path, raw.decode(errors="replace"))
        return None
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return None


def _describe(status: int | None, raw: bytes) -> str:
    text = raw.decode(errors="replace").strip()[:200]
    return f"HTTP {status}: {text}" if status is not None else text


def mqtt_service() -> tuple[dict | None, str]:
    """The broker's registered connection details, and a reason if missing."""
    reply = _request("GET", "/services/mqtt")
    if reply is None:
        return None, "Supervisor not reachable"
    if reply.get("result") == "ok" and reply.get("data", {}).get("host"):
        return reply["data"], ""
    return None, str(reply.get("message", "no MQTT service registered"))


def check_core_api() -> str:
    """One line for the log: can this app reach Home Assistant's API?
    (Notifications and the device link need it.)"""
    status, raw = _call("GET", "/core/api/")
    if status == 200:
        return "Home Assistant API reachable (notifications on)"
    return f"Home Assistant API NOT reachable, so no notifications: {_describe(status, raw)}"


def notify(notification_id: str, title: str, message: str) -> bool:
    """Show (or replace) a notification in Home Assistant. True only if shown."""
    full_id = f"bosch_ebike_{notification_id}"
    status, raw = _call("POST", "/core/api/services/persistent_notification/create",
                        {"notification_id": full_id, "title": title, "message": message})
    if status is not None and 200 <= status < 300:
        log.info("Notification shown in Home Assistant: %s", title)
        return True
    if os.environ.get("SUPERVISOR_TOKEN"):
        log.warning("Could not show notification '%s': %s", title, _describe(status, raw))
    return False


def dismiss(notification_id: str) -> None:
    status, raw = _call("POST", "/core/api/services/persistent_notification/dismiss",
                        {"notification_id": f"bosch_ebike_{notification_id}"})
    if status is not None and not 200 <= status < 300:
        log.warning("Could not dismiss notification %s: %s", notification_id,
                    _describe(status, raw))


def render_template(template: str) -> str | None:
    """Render a Home Assistant template (plain-text answer), or None."""
    status, raw = _call("POST", "/core/api/template", {"template": template})
    if status == 200:
        return raw.decode(errors="replace").strip()
    log.debug("Template failed: %s", _describe(status, raw))
    return None


def addon_page() -> str | None:
    """This add-on's own page in Home Assistant (its ingress panel)."""
    reply = _request("GET", "/addons/self/info")
    data = reply.get("data") if isinstance(reply, dict) else None
    slug = data.get("slug") if isinstance(data, dict) else None
    return f"/hassio/ingress/{slug}" if slug else None


# The bridge device's page in Home Assistant, found from its dropdown entity.
BRIDGE_DEVICE_TEMPLATE = (
    "{{ states.select | selectattr('attributes.friendly_name', 'search', "
    "'Nearby Bosch eBikes') | map(attribute='entity_id') | map('device_id') "
    "| select('string') | first | default('') }}")


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

Put the bike in pairing mode: switch it on (don't ride), press and hold the \
System Controller's **on/off button for more than 3 seconds** and let go when \
the **top bar of its charge indicator flashes blue**. Then press **This is my \
bike** again on {bridge}.
"""

MCSP_CHECK = """\
**Read full details** finished for `{address}`: *{outcome}*.

LDI (the live-data feed) reconnects by itself. Automatic full readings happen \
when the bike advertises while charging; **Read now** reads any time.

- *replies*: the bike talks MCSP when the add-on connects the way the app \
does; its extra data (motor power, battery health ...) appears on the bike's \
device.
- anything else: send the add-on log and the latest capture (the app's \
**Open web UI** page has a download link).
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
