"""
Plain-language status for Home Assistant. Pure Python.

- Setup Status (bridge device): where setup stands and what to do next,
  plus the text of the "setup" notification while no bike is added.
- Bike Status (each bike): connected how, exploring, released until when.
"""
from __future__ import annotations

from datetime import datetime

from ble_policy import pairing_text

# From Bosch's System Controller (BRC3100) manual (pairing with a smartphone).
PAIRING_MODE_STEPS = ("switch the bike on (don't ride), then press and hold the System Controller's **on/off button for more than 3 seconds** and let go when the **top bar of its charge indicator flashes blue**")


BRIDGE = "the Bosch eBike page (Settings → Apps → Bosch eBike → Open web UI)"


def setup_status(known: list[str], connected: list[str], strangers: list[tuple[str, dict]],
                 pairing_open: bool, last_scan_heard: int | None, waiting: str | None = None,
                 bridge: str = BRIDGE) -> tuple[str, dict, str | None]:
    """Returns (state, attributes, setup notification text or None).

    `waiting`: a bike picked for pairing that hasn't advertised again yet.
    `bridge`: how to refer to the bridge device (a link when known).
    """
    nearby = [{"address": a, "signal_dbm": b["rssi"],
               "ready_to_pair": b["pairable"]} for a, b in strangers]
    attributes = {"bikes": known, "connected": connected, "nearby": nearby,
                  "pairing_window_open": pairing_open, "last_scan_heard": last_scan_heard,
                  "waiting_to_pair": waiting}
    if waiting:
        state = (f"Waiting for {waiting} to advertise, then pairing: bike on, hold the "
                 "System Controller on/off button >3 s until the top bar flashes blue")
    elif pairing_open:
        state = ("Pairing window open: bike on, hold the System Controller on/off button "
                 ">3 s until the top bar flashes blue, or pick it and press Pair selected bike")
    elif not known:
        state = (f"No bike added yet; {len(strangers)} Bosch bike(s) nearby" if strangers
                 else "No bike added yet; none heard (switch the bike on next to the host)")
    elif connected:
        state = f"{len(known)} bike(s), {len(connected)} connected"
    else:
        state = f"{len(known)} bike(s), none connected right now"
    note = None if known else setup_notification(strangers, bridge)
    return state[:255], attributes, note


def setup_notification(strangers: list[tuple[str, dict]], bridge: str = BRIDGE) -> str:
    heard = "\n".join(
        f"- `{a}`: signal {b['rssi']} dBm, "
        f"{'**ready to pair**' if b['pairable'] else pairing_text(b['pairable'])}"
        for a, b in strangers) or "- none yet. Switch the bike on next to the host."
    return f"""No bike has been added yet. To add yours, open {bridge}:

1. Plug your bike into its charger (or switch it on) next to Home Assistant.
2. On the page, press **This is my bike** next to it (the strongest signal is usually the one next to the host). The bike may only advertise for a short while, so if it has gone quiet the app waits up to 3 minutes and pairs the moment it hears it. To make it advertise, put it in pairing mode: {PAIRING_MODE_STEPS}. (Other controls or displays: see their manual for "pairing".)

Bosch bikes heard nearby:
{heard}

For the official live-data feed (LDI), also press **Start pairing** and add *HA eBike Bridge* in the Flow app (bike → ⚙ → Components → Add new device).
"""


def bike_status(ldi: bool, mcsp: dict | None, released_until: float | None,
                last_seen: str | None, now: float, window_phase: str | None = None,
                last_check: dict | None = None) -> tuple[str, dict]:
    """Returns (state, attributes) for one bike.

    `window_phase`: "Connect like the Flow app" is running (LDI paused).
    `last_check`: outcome and time of the last such check.
    """
    mcsp = mcsp or {}
    mcsp_state = mcsp.get("state", "")
    silent = mcsp_state.startswith("bike not answering")
    mcsp_live = bool(mcsp_state) and not silent and not any(
        mcsp_state.startswith(s) for s in ("stopped", "disconnected", "add-on stopped",
                                           "restarted", "MCSP not exposed",
                                           "MCSP disabled"))
    links = " + ".join(n for n, on in (("LDI", ldi), ("MCSP", mcsp_live)) if on)
    answered = mcsp.get("values") or 0
    if window_phase:
        state = f"Checking MCSP like the Flow app: {window_phase} (LDI paused)"
    elif mcsp_live and mcsp_state.startswith("sweeping"):
        total, done = mcsp.get("sweep_total") or 0, mcsp.get("sweep_done") or 0
        pct = round(100 * done / total) if total else 0
        state = f"Exploring ({links}): sweeping datapoints, {pct} %, {answered} answered"
    elif mcsp_live and mcsp_state.startswith(("reading", "listening", "handshake",
                                              "starting", "bike not ready")):
        state = f"Exploring ({links}): {mcsp_state}, {answered} answered"
    elif silent and links:
        state = f"Connected ({links}); MCSP not answering on this link"
    elif links:
        state = f"Connected ({links})"
    elif released_until and released_until > now:
        when = datetime.fromtimestamp(released_until)
        same_day = when.date() == datetime.fromtimestamp(now).date()
        until = when.strftime("%H:%M" if same_day else "%a %H:%M")
        state = f"Released for the Flow app; next check after {until}"
    elif last_seen:
        state = "Not connected"
    else:
        state = "Not seen yet"
    attributes = {"ldi_connected": ldi, "mcsp_state": mcsp_state or None,
                  "mcsp_values": mcsp.get("values"), "last_seen": last_seen,
                  "released_until": (datetime.fromtimestamp(released_until).isoformat()
                                     if released_until else None),
                  "mcsp_check": last_check}
    return state[:255], attributes
