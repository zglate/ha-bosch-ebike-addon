"""
The add-on's own page in Home Assistant (ingress: "Open web UI", or the
sidebar): an overview of your bikes, each bike's details and actions, adding
a bike, and the debug captures at the bottom.

The server runs in a background thread. It only reads a plain snapshot the
bridge refreshes every 2 s on its main loop, and hands button presses back to
the main loop; it never touches Bluetooth itself. Only the Supervisor's
ingress proxy may connect (Home Assistant has already logged the user in).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

PORT = int(os.environ.get("BOSCH_WEBUI_PORT", 8099))           # = ingress_port
# The Supervisor's ingress proxy; tests connect from localhost.
ALLOWED_CLIENTS = {"172.30.32.2", "127.0.0.1"}
RUN_NAME = re.compile(r"^run-\d{8}-\d{6}(-\d+)?$")
ACTIONS = {"read", "stop", "add", "start_pairing", "remove", "settings"}
_ZIP_LOCK = threading.Lock()     # one zip at a time: a run can be large


def runs(base: Path | None) -> list[Path]:
    """Run folders, newest first."""
    if base is None or not base.is_dir():
        return []
    return sorted((p for p in base.iterdir() if p.is_dir() and RUN_NAME.match(p.name)),
                  key=lambda p: p.name, reverse=True)


def zip_run(run_dir: Path, out) -> None:
    """Write the run folder as a zip into the file object `out`."""
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(run_dir.iterdir()):
            if path.is_file():
                zf.write(path, f"{run_dir.name}/{path.name}")


def run_list(base: Path | None) -> list[dict]:
    out = []
    for run in runs(base)[:10]:
        size = sum(f.stat().st_size for f in run.iterdir() if f.is_file())
        out.append({"name": run.name, "mb": round(size / 1e6, 1)})
    return out


def make_handler(base: Path | None, state: Callable[[], dict],
                 action: Callable[[str, dict], None]):
    class Handler(BaseHTTPRequestHandler):
        timeout = 10                         # a stalled client can't hold a thread

        def _allowed(self) -> bool:
            if self.client_address[0] in ALLOWED_CLIENTS:
                return True
            self.send_error(403)
            return False

        def do_GET(self):
            if not self._allowed():
                return
            path = self.path.split("?", 1)[0].rstrip("/")
            name = path.rsplit("/", 1)[-1]
            if path in ("", "/") or path.endswith("/index.html"):
                self._send(200, "text/html; charset=utf-8", PAGE.encode())
            elif path == "/api/state":
                body = {**(state() or {}), "runs": run_list(base)}
                self._send(200, "application/json", json.dumps(body, default=str).encode())
            elif base is not None and path.startswith("/download/") and name.endswith(".zip") \
                    and RUN_NAME.match(name[:-4]) and (base / name[:-4]).is_dir():
                with _ZIP_LOCK, tempfile.TemporaryFile() as tmp:
                    zip_run(base / name[:-4], tmp)
                    size = tmp.tell()
                    tmp.seek(0)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Length", str(size))
                    self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                    self.end_headers()
                    shutil.copyfileobj(tmp, self.wfile, 1 << 16)
            else:
                self.send_error(404)

        def do_POST(self):
            if not self._allowed():
                return
            if self.path.split("?", 1)[0].rstrip("/") != "/api/action":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 <= length <= 4096:
                    raise ValueError("length")
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("not an object")
                name = str(body.get("action", ""))
                if name not in ACTIONS:
                    raise ValueError(name)
            except (ValueError, TypeError, OSError):
                self._send(400, "application/json", b'{"ok": false}')
                return
            action(name, {k: v for k, v in body.items() if k != "action"})
            self._send(200, "application/json", b'{"ok": true}')

        def _send(self, code: int, content_type: str, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    return Handler


def start(base_dir: str | None, port: int = PORT, state: Callable[[], dict] = dict,
          action: Callable[[str, dict], None] = lambda a, p: None
          ) -> ThreadingHTTPServer | None:
    """Serve the page in a background thread; never fatal."""
    try:
        server = ThreadingHTTPServer(
            ("0.0.0.0", port),
            make_handler(Path(base_dir) if base_dir else None, state, action))
    except OSError as exc:
        log.warning("Web page not started: %s", exc)
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Web page on port %d (the app's Open web UI button, or the sidebar)", port)
    return server


# ── The page ─────────────────────────────────────────────────────────────────
# One file, no build step, no external assets (ingress pages can't load them
# reliably). Plain DOM rendering from /api/state every 2 s. All text from the
# bike goes in through textContent, never as HTML.

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bosch eBike</title>
<style>
:root{--bg:#f6f6f7;--card:#fff;--text:#1c1c1e;--muted:#6b6b72;--line:#e4e4e7;
--accent:#0b74c4;--good:#2e7d32;--warn:#b26a00;--bad:#c62828;--chip:#f0f1f3}
@media (prefers-color-scheme:dark){:root{--bg:#111214;--card:#1c1d20;--text:#ececef;
--muted:#9a9aa3;--line:#2c2d31;--accent:#4aa3e8;--good:#66bb6a;--warn:#e0a03a;--bad:#ef5350;
--chip:#26272b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px 48px}
header{display:flex;align-items:baseline;gap:10px;margin-bottom:18px}
h1{font-size:22px;font-weight:600;margin:0}
h2{font-size:17px;font-weight:600;margin:0 0 4px}
h3{font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;
letter-spacing:.04em;margin:0 0 10px}
.muted{color:var(--muted)}.small{font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(280px,100%),1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.bike{cursor:pointer;transition:border-color .15s}.bike:hover{border-color:var(--accent)}
.status{display:flex;align-items:center;gap:8px;font-size:14px;color:var(--muted);margin:6px 0 14px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--muted);flex:none}
.dot.live{background:var(--good)}.dot.reading{background:var(--accent)}
.big{font-size:34px;font-weight:600;line-height:1}
.facts{display:flex;gap:18px;margin-top:12px;flex-wrap:wrap}
.fact .v{font-weight:600}.fact .k{font-size:12px;color:var(--muted)}
.bar{height:6px;background:var(--chip);border-radius:3px;margin-top:10px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--good)}
button,.btn{font:inherit;font-size:14px;border-radius:8px;padding:8px 14px;cursor:pointer;
border:1px solid var(--line);background:var(--card);color:var(--text);text-decoration:none;
display:inline-block}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.danger{color:var(--bad)}
button:disabled{opacity:.5;cursor:default}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 4px}
.back{background:none;border:none;color:var(--accent);padding:0;margin-bottom:12px}
.sections{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(290px,100%),1fr));
gap:14px;margin-top:14px}
@media (max-width:560px){.form{grid-template-columns:1fr}}
dl{display:grid;grid-template-columns:1fr auto;gap:7px 12px;margin:0}
dt{color:var(--muted)}dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
.tag{font-size:12px;padding:2px 7px;border-radius:10px;background:var(--chip);color:var(--muted)}
.tag.new{background:var(--warn);color:#fff}
ol{padding-left:20px;margin:8px 0}ol li{margin:6px 0}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px;
padding:10px 0;border-top:1px solid var(--line)}
.row:first-child{border-top:none}
label{display:block;font-size:13px;color:var(--muted);margin-bottom:4px}
input{font:inherit;width:100%;padding:8px 10px;border-radius:8px;border:1px solid var(--line);
background:var(--bg);color:var(--text)}
.form{display:grid;grid-template-columns:1fr 140px auto;gap:10px;align-items:end}
details{margin-top:28px}summary{cursor:pointer;color:var(--muted);font-size:14px}
.note{font-size:13px;color:var(--muted);margin-top:10px}
a{color:var(--accent)}
</style></head><body><main>
<header><h1>Bosch eBike</h1><span id="ver" class="muted small"></span></header>
<div id="app"><p class="muted">Loading…</p></div>
<details id="debug"><summary>Debug captures</summary>
<p class="note">Each start of the app records one run. Send the newest one along with the
app's log when something doesn't work.</p><div id="runs"></div></details>
</main>
<script>
"use strict";
let S = null, view = location.hash.slice(1) || "", busy = false, editing = false;
const $ = (t, attrs, ...kids) => {
  const e = document.createElement(t);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v; else if (k.startsWith("on")) e[k] = v;
    else if (v === true) e.setAttribute(k, "");
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v);
  }
  for (const c of kids.flat(Infinity)) if (c !== null && c !== undefined && c !== false)
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
};
const num = (v, d = 0) => (v === null || v === undefined || v === "") ? null : Number(v).toFixed(d);
const unit = (v, u, d = 0) => { const n = num(v, d); return n === null ? "–" : n + u; };
function ago(iso) {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 90) return "just now";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  if (s < 129600) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " days ago";
}
const READS = {peek: "plugged in", charging: "while charging", button: "you asked",
  explored: "you asked"};
function lastRead(b) {
  const r = b.last_good, t = b.last_read;
  if (r) return "Full reading " + ago(r.at) + " (" + (READS[r.reason] || r.reason) + ")";
  if (t) return "No full reading yet. Last try " + ago(t.at) + ": " + t.outcome;
  return "No full reading yet";
}
async function act(action, extra) {
  busy = true; render();
  try {
    await fetch("api/action", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(Object.assign({action}, extra || {}))});
  } finally { setTimeout(() => { busy = false; load(); }, 600); }
}
const go = v => { view = v; location.hash = v; editing = false; render(); };

function soc(b) { return b.ldi.battery_soc ?? b.mcsp.state_of_charge ?? null; }
function km(b) {
  if (b.ldi.odometer_km != null) return b.ldi.odometer_km;
  return b.mcsp.odometer_m != null ? b.mcsp.odometer_m / 1000 : null;
}
function statusText(b) {
  if (b.reading) return "Reading the bike…";
  if (b.live) return "Connected · live data";
  return b.last_seen ? "Asleep · last seen " + ago(b.last_seen) : "Not seen yet";
}
function statusLine(b) {
  return $("div", {class: "status", title: b.status},
    $("span", {class: "dot" + (b.reading ? " reading" : b.live ? " live" : "")}), statusText(b));
}

function overview() {
  const cards = S.bikes.map(b => {
    const p = soc(b);
    return $("div", {class: "card bike", onclick: () => go(b.address)},
      $("h2", {}, b.name),
      $("div", {class: "muted small"}, [b.model, b.battery].filter(Boolean).join(" · ") || b.address),
      statusLine(b),
      $("div", {class: "big"}, p === null ? "–" : p + "%"),
      $("div", {class: "bar"}, $("i", {style: "width:" + (p || 0) + "%"})),
      $("div", {class: "facts"},
        fact("Health", unit(b.mcsp.battery_health, "%")),
        fact("Cycles", unit(b.mcsp.charge_cycles, "", 1)),
        fact("Distance", unit(km(b), " km"))),
      $("div", {class: "note"}, lastRead(b)));
  });
  return [
    $("div", {class: "grid"}, cards),
    $("div", {class: "actions"}, $("button", {onclick: () => go("add")}, "Add a bike"))];
}
const fact = (k, v) => $("div", {class: "fact"}, $("div", {class: "v"}, v), $("div", {class: "k"}, k));

function section(title, rows) {
  const items = rows.filter(r => r && r[1] !== null && r[1] !== undefined && r[1] !== "–");
  if (!items.length) return null;
  return $("div", {class: "card"}, $("h3", {}, title),
    $("dl", {}, items.map(([k, v]) => [$("dt", {}, k), $("dd", {}, v)])));
}

function bikePage(b) {
  const m = b.mcsp, l = b.ldi;
  const charger = l.charger_connected ?? m.charging_active;
  const sections = [
    section("Battery", [
      ["Charge", unit(soc(b), "%")], ["Health", unit(m.battery_health, "%")],
      ["Charge cycles", unit(m.charge_cycles, "", 1)],
      ["Temperature", unit(m.battery_temperature, " °C", 1)],
      ["Usable energy", unit(m.system_usable_energy, " Wh")],
      ["Capacity", unit(m.battery_capacity, " Ah", 1)],
      ["Delivered so far", unit(m.lifetime_delivered_wh, " Wh")],
      ["Size used for estimates", b.battery_wh + " Wh"]]),
    section("Charging", [
      ["Charger", charger == null ? "–" : charger ? "Connected" : "Not connected"],
      ["Charging mode", m.charging_mode ?? "–"],
      ["Charge limit", m.charge_limit != null ? m.charge_limit + "%" : (m.charging_mode ? "None" : "–")]]),
    section("Riding", [
      ["Distance", unit(km(b), " km", 1)],
      ["Hours switched on", unit(m.power_on_hours, " h")],
      ["Hours with motor help", unit(m.motor_support_hours, " h")],
      ["Motor temperature", unit(m.drive_pcb_temperature, " °C", 1)],
      ["Assist mode", m.assist_mode ?? "–"]]),
    b.firmware.length ? $("div", {class: "card"}, $("h3", {}, "Software"),
      $("dl", {}, b.firmware.map(f => [
        $("dt", {title: f.product || ""}, f.title),
        $("dd", {}, f.installed, " ", f.newer ? $("span", {class: "tag new"}, f.latest + " available")
          : f.latest ? $("span", {class: "tag"}, "up to date") : null)]))) : null,
  ];
  const settings = $("div", {class: "card"}, $("h3", {}, "Settings"),
    $("div", {class: "form"},
      $("div", {}, $("label", {for: "nm"}, "Name"),
        $("input", {id: "nm", value: b.name, maxlength: 40, onfocus: () => editing = true,
          onblur: () => editing = false})),
      $("div", {}, $("label", {for: "wh"}, "Battery size (Wh)"),
        $("input", {id: "wh", type: "number", min: 100, max: 5000, step: 1, value: b.battery_wh,
          onfocus: () => editing = true, onblur: () => editing = false})),
      $("button", {onclick: () => { editing = false; act("settings", {address: b.address,
        name: document.getElementById("nm").value,
        battery_wh: document.getElementById("wh").value}); }}, "Save")),
    b.configured ? $("p", {class: "note"}, "This bike is listed in the app's configuration; remove it there.") : null);
  return [
    $("button", {class: "back", onclick: () => go("")}, "‹ All bikes"),
    $("div", {class: "card"},
      $("h2", {}, b.name),
      $("div", {class: "muted small"}, [b.model, b.battery, b.address].filter(Boolean).join(" · ")),
      statusLine(b),
      $("div", {class: "small"}, lastRead(b)),
      $("div", {class: "actions"},
        b.reading ? $("button", {onclick: () => act("stop"), disabled: busy}, "Stop reading")
          : $("button", {class: "primary", onclick: () => act("read", {address: b.address}),
              disabled: busy}, "Read now"),
        !b.configured ? $("button", {class: "danger", disabled: busy, onclick: () => {
          if (confirm("Remove " + b.name + "? You can add it again later.")) {
            act("remove", {address: b.address}); go(""); } }}, "Remove") : null),
      $("p", {class: "note"}, "Read now pauses live data and connects the way the Flow app does, " +
        "for up to 5 minutes. Keep the bike on and standing still; close the Flow app.")),
    $("div", {class: "sections"}, sections),
    $("div", {style: "margin-top:14px"}, settings)];
}

function addPage() {
  const has = S.bikes.length > 0;
  const list = S.nearby.length ? S.nearby.map(n => $("div", {class: "row"},
    $("div", {}, $("div", {}, n.address),
      $("div", {class: "muted small"},
        signal(n.rssi) + (n.pairing === "ready to pair" ? " · ready to pair" : ""))),
    S.adding.includes(n.address) || S.waiting_for === n.address
      ? $("span", {class: "tag"}, S.waiting_for === n.address ? "Waiting for the bike…" : "Adding…")
      : $("button", {class: "primary", disabled: busy, onclick: () => act("add", {address: n.address})},
          "This is my bike")))
    : [$("p", {class: "muted"}, "No Bosch bike heard yet. It shows up here within a minute.")];
  return [
    has ? $("button", {class: "back", onclick: () => go("")}, "‹ All bikes") : null,
    $("div", {class: "card"},
      $("h2", {}, has ? "Add another bike" : "Add your bike"),
      $("ol", {},
        $("li", {}, "Plug the bike into its charger, or switch it on, next to Home Assistant."),
        $("li", {}, "Pick it below. The strongest signal is usually the bike closest to Home Assistant."),
        $("li", {}, "If adding fails: switch the bike on, hold the System Controller's on/off button " +
          "for more than 3 seconds until the top bar flashes blue, and pick it again."))),
    $("div", {class: "card", style: "margin-top:14px"}, $("h3", {}, "Bosch bikes nearby"), list),
    $("div", {class: "card", style: "margin-top:14px"}, $("h3", {}, "Live riding data (optional)"),
      $("p", {class: "small"}, "For live speed, cadence and your pedalling power, also add the app " +
        "as an accessory in the Flow app: press Start pairing, then in the Flow app open your bike → " +
        "Settings → Components → Add new device, and pick HA eBike Bridge."),
      $("button", {disabled: busy || S.pairing_open, onclick: () => act("start_pairing")},
        S.pairing_open ? "Pairing open for 5 minutes" : "Start pairing"))];
}
function signal(rssi) {
  if (rssi == null) return "signal unknown";
  return rssi > -65 ? "strong signal" : rssi > -80 ? "medium signal" : "weak signal";
}

function render() {
  if (!S || editing) return;
  document.getElementById("ver").textContent = S.version ? "app " + S.version : "";
  const app = document.getElementById("app");
  const bike = S.bikes.find(b => b.address === view);
  const content = view === "add" || !S.bikes.length ? addPage() : bike ? bikePage(bike) : overview();
  app.replaceChildren(...[content].flat().filter(Boolean));
  const runs = document.getElementById("runs");
  runs.replaceChildren(...(S.runs.length ? S.runs.map((r, i) => $("div", {class: "row small"},
    $("span", {}, r.name, i === 0 ? $("span", {class: "muted"}, " (current)") : null),
    $("a", {href: "download/" + r.name + ".zip"}, r.mb + " MB · download"))) :
    [$("p", {class: "muted small"}, "No captures (debug capture is off).")]));
}
async function load() {
  try { S = await (await fetch("api/state", {cache: "no-store"})).json(); render(); }
  catch (e) { document.getElementById("app").replaceChildren(
    $("p", {class: "muted"}, "The app isn't answering. Is it running?")); }
}
window.onhashchange = () => { view = location.hash.slice(1); editing = false; render(); };
load(); setInterval(() => { if (!busy) load(); }, 2000);
</script></body></html>
"""
