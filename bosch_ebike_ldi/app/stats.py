"""
Charging and riding history for the page: how long charges take, what a ride
used, Wh per km, what a full battery is good for. Pure Python.

Fed with samples (time, charge %, charger connected, odometer km) from the
live data and full readings. A real bike streams live data for the whole
charge (10 % -> 100 % in 4 h 25 min, 19-20 Nov), so charges are seen
start to finish; rides are what happened between unplugging and the next
plug-in (odometer and charge difference).

The history dict is stored in the add-on's state file:
  last      latest known values {t, soc, charger, km}
  cur       charge in progress {start_t, start_soc, start_km, last_t, last_soc, last_km, full_t}
  anchor    battery state when last unplugged {t, soc, km}
  sessions  finished charges [{start_t, start_soc, end_t, end_soc, full}]   (newest last)
  rides     between charges [{from_t, to_t, km, used_pct}]                  (newest last)
"""
from __future__ import annotations

KEEP = 30                     # sessions / rides kept
MIN_SESSION_PCT = 5           # a charge must add at least this much to count
MIN_RIDE_KM = 1.0             # rides shorter than this don't count for Wh/km


FRESH_SEC = 180              # a value is "current" for this long
PENDING_SEC = 300            # values of a plug-in may arrive this long after it


def update(h: dict, t: float, soc=None, charger=None, km=None) -> bool:
    """Feed one sample. Returns True when a charge or ride was opened/closed
    (worth saving right away).

    A full reading delivers charge, charger and odometer as separate values,
    "charger connected" sometimes first; so a plug-in takes the charge and
    odometer that arrive within PENDING_SEC, not the stale ones from before
    the ride.
    """
    last = h.setdefault("last", {})
    for key, value in (("soc", soc), ("charger", charger), ("km", km)):
        if value is not None:
            last[key], last[f"{key}_t"] = value, t
    last["t"] = t
    fresh = {k: last.get(k) if last.get(f"{k}_t", -1e18) >= t - FRESH_SEC else None
             for k in ("soc", "km")}
    soc_now, km_now, charger = last.get("soc"), last.get("km"), last.get("charger")
    cur = h.get("cur")
    changed = False

    if charger and cur is not None and soc is not None and cur.get("last_soc") is not None \
            and soc < cur["last_soc"] - 5:
        # Lower than when we last saw it on the charger: it was unplugged,
        # ridden and plugged in again while we weren't watching.
        h["anchor"] = {"t": cur["last_t"], "soc": cur.get("last_soc"), "km": cur.get("last_km")}
        _close(h, cur["last_t"], cur.get("last_km"), keep_anchor=True)
        cur, changed = None, True

    if charger:
        if cur is None:
            h["cur"] = cur = {"start_t": t, "start_soc": fresh["soc"], "start_km": fresh["km"],
                              "last_t": t, "last_soc": soc_now, "last_km": km_now,
                              "full_t": t if fresh["soc"] is not None and fresh["soc"] >= 100
                              else None, "ride_pending": h.get("anchor") is not None}
            changed = True
        else:
            cur["last_t"] = t
            if km is not None:
                cur["last_km"] = km
            if soc is not None:
                cur["last_soc"] = soc
                if soc >= 100 and not cur.get("full_t"):
                    cur["full_t"] = t
                    changed = True
        # Values of the plug-in itself, arriving a moment after it.
        if t - cur["start_t"] <= PENDING_SEC:
            if cur.get("start_soc") is None and soc is not None:
                cur["start_soc"] = soc
            if cur.get("start_km") is None and km is not None:
                cur["start_km"] = km
        if cur.get("ride_pending") and (
                (cur.get("start_km") is not None and cur.get("start_soc") is not None)
                or t - cur["start_t"] > PENDING_SEC):
            cur["ride_pending"] = False
            changed |= _add_ride(h, cur)
        return changed

    if charger is False:
        if cur is not None:
            _close(h, t, fresh["km"] if fresh["km"] is not None else cur.get("last_km"))
            return True
        anchor = h.get("anchor")
        if anchor is None:
            h["anchor"] = {"t": t, "soc": fresh["soc"], "km": fresh["km"]}
            return True
        if t - anchor["t"] <= PENDING_SEC:          # the unplug's own values, a moment later
            if anchor.get("km") is None and km is not None:
                anchor["km"] = km
            if anchor.get("soc") is None and soc is not None:
                anchor["soc"] = soc
    return changed


def _add_ride(h: dict, cur: dict) -> bool:
    anchor = h.get("anchor")
    if not anchor or anchor.get("km") is None or cur.get("start_km") is None:
        return False
    used = (anchor["soc"] - cur["start_soc"]) if anchor.get("soc") is not None \
        and cur.get("start_soc") is not None else None
    ride = {"from_t": anchor["t"], "to_t": cur["start_t"],
            "km": round(cur["start_km"] - anchor["km"], 2),
            "used_pct": round(used, 1) if used is not None else None}
    if ride["km"] > 0.05 or (used or 0) > 0:
        h.setdefault("rides", []).append(ride)
        h["rides"] = h["rides"][-KEEP:]
        return True
    return False


def _close(h: dict, t: float, km, keep_anchor: bool = False) -> None:
    cur = h.pop("cur")
    end_t = cur.get("full_t") or cur["last_t"]
    end_soc = 100 if cur.get("full_t") else cur.get("last_soc")
    if cur.get("start_soc") is not None and end_soc is not None \
            and end_soc - cur["start_soc"] >= MIN_SESSION_PCT:
        h.setdefault("sessions", []).append({
            "start_t": cur["start_t"], "start_soc": cur["start_soc"], "end_t": end_t,
            "end_soc": end_soc, "full": bool(cur.get("full_t"))})
        h["sessions"] = h["sessions"][-KEEP:]
    if not keep_anchor:
        h["anchor"] = {"t": t, "soc": cur.get("last_soc"),
                       "km": km if km is not None else cur.get("last_km")}


def summary(h: dict, usable_wh=None) -> dict:
    """What the page shows. Missing values are None (not enough history)."""
    sessions = [s for s in h.get("sessions", []) if s["end_t"] > s["start_t"]]
    cur = h.get("cur")
    if cur and cur.get("full_t") and cur.get("start_soc") is not None \
            and 100 - cur["start_soc"] >= MIN_SESSION_PCT and cur["full_t"] > cur["start_t"]:
        sessions = sessions + [{"start_t": cur["start_t"], "start_soc": cur["start_soc"],
                                "end_t": cur["full_t"], "end_soc": 100, "full": True}]
    rate = None                                   # % per hour, over real charges
    pct = sum(s["end_soc"] - s["start_soc"] for s in sessions)
    hours = sum((s["end_t"] - s["start_t"]) / 3600 for s in sessions)
    if hours >= 0.3 and pct > 0:
        rate = pct / hours
    last_charge = sessions[-1] if sessions else None
    charging = None
    if cur is not None:
        soc = cur.get("last_soc")
        charging = {"since_t": cur["start_t"], "start_soc": cur.get("start_soc"), "soc": soc,
                    "full": bool(cur.get("full_t")),
                    "minutes_to_full": round((100 - soc) / rate * 60)
                    if rate and soc is not None and soc < 100 else None}
    rides = h.get("rides", [])
    last_ride = dict(rides[-1]) if rides else None
    if last_ride and usable_wh and last_ride.get("used_pct") is not None:
        last_ride["wh"] = round(last_ride["used_pct"] / 100 * usable_wh)
        if last_ride["km"] >= MIN_RIDE_KM:
            last_ride["wh_per_km"] = round(last_ride["wh"] / last_ride["km"], 1)
    counted = [r for r in rides if r["km"] >= MIN_RIDE_KM and r.get("used_pct")]
    wh_per_km = None
    if usable_wh and counted:
        wh_per_km = sum(r["used_pct"] / 100 * usable_wh for r in counted) / sum(
            r["km"] for r in counted)
    return {
        "last_charge": last_charge and {**last_charge, "minutes": round(
            (last_charge["end_t"] - last_charge["start_t"]) / 60)},
        "full_charge_minutes": round(100 / rate * 60) if rate else None,
        "charging": charging,
        "last_ride": last_ride,
        "wh_per_km": round(wh_per_km, 1) if wh_per_km else None,
        "range_full_km": round(usable_wh / wh_per_km) if wh_per_km and usable_wh else None,
        "charges": len(sessions),
        "rides": len(rides),
        "km_recorded": round(sum(r["km"] for r in rides), 1),
    }


def samples_from_captures(paths, address: str) -> list:
    """(t, soc, charger, km) from debug capture files, oldest first: live-data
    events and the charge / charger / odometer datapoints of full readings."""
    import json
    from datetime import datetime

    out = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if address not in line or ('"ldi"' not in line and '"datapoint"' not in line):
                        continue
                    try:
                        e = json.loads(line)
                        t = datetime.fromisoformat(e["t"]).timestamp()
                    except (ValueError, KeyError):
                        continue
                    if e.get("bike") != address:
                        continue
                    if e.get("kind") == "ldi":
                        d = e.get("decoded") or {}
                        if any(d.get(k) is not None for k in
                               ("battery_soc", "charger_connected", "odometer_km")):
                            out.append((t, d.get("battery_soc"), d.get("charger_connected"),
                                        d.get("odometer_km")))
                    elif e.get("kind") == "datapoint" and e.get("status") == "SUCCESS" \
                            and e.get("value") is not None:
                        dp, v = e.get("id"), e.get("value")
                        if dp == "0x0088":
                            out.append((t, v, None, None))
                        elif dp == "0x0089":
                            out.append((t, None, bool(v), None))
                        elif dp == "0x1818" and isinstance(v, (int, float)):
                            out.append((t, None, None, v / 1000))
        except OSError:
            continue
    return sorted(out, key=lambda x: x[0])
