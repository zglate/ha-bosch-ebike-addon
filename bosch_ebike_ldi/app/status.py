"""
Plain-language status for Home Assistant. Pure Python.

- Setup Status (bridge device): where setup stands and what to do next,
  plus the text of the "setup" notification while no bike is added.
- Bike Status (each bike): connected how, exploring, released until when.
"""
from __future__ import annotations

from datetime import datetime


def setup_status(known: list[str], connected: list[str], strangers: list[tuple[str, dict]],
                 pairing_open: bool, last_scan_heard: int | None) -> tuple[str, dict, str | None]:
    """Returns (state, attributes, setup notification text or None)."""
    nearby = [{"address": a, "signal_dbm": b["rssi"],
               "ready_to_pair": b["pairable"]} for a, b in strangers]
    attributes = {"bikes": known, "connected": connected, "nearby": nearby,
                  "pairing_window_open": pairing_open, "last_scan_heard": last_scan_heard}
    if pairing_open:
        state = ("Pairing window open: put the bike in pairing mode, or pick it "
                 "under Nearby Bosch eBikes and press Pair selected bike")
    elif not known:
        state = (f"No bike added yet; {len(strangers)} Bosch bike(s) nearby" if strangers
                 else "No bike added yet; none heard (switch the bike on next to the host)")
    elif connected:
        state = f"{len(known)} bike(s), {len(connected)} connected"
    else:
        state = f"{len(known)} bike(s), none connected right now"
    return state[:255], attributes, (None if known else setup_notification(strangers))


def setup_notification(strangers: list[tuple[str, dict]]) -> str:
    heard = "\n".join(
        f"- `{a}`: signal {b['rssi']} dBm, "
        f"{'**ready to pair**' if b['pairable'] else 'not in pairing mode'}"
        for a, b in strangers) or "- none yet. Switch the bike on next to the host."
    return f"""\
No bike has been added yet. Two ways to add yours:

1. **Pairing mode:** put the bike in pairing mode, then press **Start \
pairing** on the *Bosch eBike Bridge* device. The app pairs with the bike \
that says it's ready.
2. **Pick it:** choose it under **Nearby Bosch eBikes** on the same device \
and press **Pair selected bike**.

Bosch bikes heard nearby:
{heard}

For the official live-data feed (LDI), also add *HA eBike Bridge* in the \
Flow app (bike → ⚙ → Components → Add new device) while Start pairing is on.
"""


def bike_status(ldi: bool, mcsp: dict | None, released_until: float | None,
                last_seen: str | None, now: float) -> tuple[str, dict]:
    """Returns (state, attributes) for one bike."""
    mcsp = mcsp or {}
    mcsp_state = mcsp.get("state", "")
    mcsp_live = bool(mcsp_state) and not any(
        mcsp_state.startswith(s) for s in ("stopped", "disconnected", "add-on stopped",
                                           "restarted", "MCSP not exposed",
                                           "MCSP disabled"))
    links = " + ".join(n for n, on in (("LDI", ldi), ("MCSP", mcsp_live)) if on)
    if mcsp_live and mcsp_state.startswith("sweeping"):
        total, done = mcsp.get("sweep_total") or 0, mcsp.get("sweep_done") or 0
        pct = round(100 * done / total) if total else 0
        state = f"Exploring ({links}): sweeping datapoints, {pct} %"
    elif mcsp_live and mcsp_state.startswith(("reading", "listening", "handshake",
                                              "starting", "bike not ready")):
        state = f"Exploring ({links}): {mcsp_state}"
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
                                     if released_until else None)}
    return state[:255], attributes
