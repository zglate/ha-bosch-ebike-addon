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
:root{
  --bg:#f3f4f6;--card:#ffffff;--text:#111827;--muted:#6b7280;--faint:#9ca3af;--line:#e5e7eb;
  --soft:#f3f4f6;--accent:#2563eb;--accent-soft:#dbeafe;--good:#16a34a;--good-soft:#dcfce7;
  --warn:#b45309;--warn-soft:#fef3c7;--bad:#dc2626;--shadow:0 1px 2px rgba(17,24,39,.06),0 1px 1px rgba(17,24,39,.03)}
@media (prefers-color-scheme:dark){:root{
  --bg:#0f1115;--card:#181b21;--text:#f3f4f6;--muted:#9aa1ad;--faint:#6b7280;--line:#272b33;
  --soft:#20242c;--accent:#3b82f6;--accent-soft:#1e293b;--good:#4ade80;--good-soft:#14281c;
  --warn:#fbbf24;--warn-soft:#2b2310;--bad:#f87171;--shadow:none}}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text)}
body{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased}
main{max-width:1040px;margin:0 auto;padding:28px 20px 56px}
.top{display:flex;align-items:center;justify-content:space-between;margin-bottom:22px}
.top h1{font-size:20px;font-weight:650;margin:0;letter-spacing:-.01em}
.card{background:var(--card);border-radius:16px;box-shadow:var(--shadow);border:1px solid var(--line)}
@media (prefers-color-scheme:light){.card{border-color:transparent}}
.pad{padding:22px}
.muted{color:var(--muted)}.small{font-size:13px}
button,.btn{font:inherit;font-weight:550;font-size:14px;border-radius:10px;padding:9px 16px;cursor:pointer;
  border:1px solid var(--line);background:var(--card);color:var(--text);display:inline-flex;align-items:center;gap:8px}
button:hover{border-color:var(--faint)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
@media (prefers-color-scheme:dark){button.ghost,.runs a{color:#93c5fd}}
button.primary:hover{filter:brightness(1.05)}
button.ghost{border-color:transparent;background:transparent;color:var(--accent);padding:6px 8px}
button.danger{color:var(--bad)}
button:disabled{opacity:.55;cursor:default;filter:none}
svg.i{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;flex:none}

/* bike hero */
.hero{display:grid;grid-template-columns:auto 1fr;gap:28px;align-items:center}
.ring{position:relative;width:132px;height:132px}
.ring svg{width:132px;height:132px;transform:rotate(-90deg)}
.ring .track{stroke:var(--soft)}.ring .fill{stroke:var(--good);transition:stroke-dashoffset .6s}
.ring .val{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.ring .pct{font-size:32px;font-weight:650;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.ring .sub{font-size:12px;color:var(--muted);margin-top:-2px}
.name{font-size:24px;font-weight:650;margin:0;letter-spacing:-.015em}
.model{color:var(--muted);font-size:14px;margin-top:2px}
.pill{display:inline-flex;align-items:center;gap:7px;font-size:13px;font-weight:550;border-radius:999px;
  padding:4px 11px;background:var(--soft);color:var(--muted);margin-top:12px}
.pill .d{width:7px;height:7px;border-radius:50%;background:currentColor}
.pill.live{background:var(--good-soft);color:var(--good)}
.pill.busy{background:var(--accent-soft);color:var(--accent)}
.pill.busy .d{animation:pulse 1.2s infinite}
@keyframes pulse{50%{opacity:.25}}
.fresh{font-size:13px;color:var(--muted);margin-top:10px}
.cta{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:16px}
.explain{font-size:13px;color:var(--muted);margin:10px 0 0;max-width:620px}
.alert{font-size:13px;color:var(--warn);background:var(--warn-soft);border-radius:10px;padding:8px 12px;margin-top:12px;
  display:flex;gap:8px;align-items:flex-start}

/* stats */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:16px}
.stat{padding:16px 18px}
.stat .k{font-size:13px;color:var(--muted);display:flex;align-items:center;gap:6px}
.stat .v{font-size:26px;font-weight:650;margin-top:6px;letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.stat .v small{font-size:15px;font-weight:550;color:var(--muted);margin-left:2px}
.stat .h{font-size:12px;color:var(--faint);margin-top:2px}
.bar{height:6px;border-radius:3px;background:var(--soft);margin-top:10px;overflow:hidden}
.bar i{display:block;height:100%;border-radius:3px;background:var(--good)}

/* details */
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}
.sec h3{display:flex;align-items:center;gap:8px;font-size:15px;font-weight:650;margin:0 0 12px}
.rows{display:grid;grid-template-columns:max-content 1fr;row-gap:10px;column-gap:24px}
.wide{grid-column:1/-1}
.wide .rows{grid-template-columns:max-content 1fr max-content 1fr}
@media (max-width:720px){.wide .rows{grid-template-columns:max-content 1fr}}
.rows .l{color:var(--muted)}.rows .r{text-align:right;font-variant-numeric:tabular-nums;font-weight:500}
.chip{font-size:12px;font-weight:600;border-radius:999px;padding:2px 9px;margin-left:8px;white-space:nowrap}
.chip.ok{background:var(--soft);color:var(--muted)}
.chip.new{background:var(--warn-soft);color:var(--warn)}
.empty{color:var(--muted);font-size:14px}
.msg{padding:12px 0;border-top:1px solid var(--line)}.msg:first-of-type{border-top:none;padding-top:2px}

/* overview */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr));gap:16px}
.bike{cursor:pointer;display:flex;gap:18px;align-items:center;transition:transform .12s}
.bike:hover{transform:translateY(-1px)}
.bike .ring,.bike .ring svg{width:84px;height:84px}
.bike .ring .pct{font-size:20px}
.mini{display:flex;gap:16px;margin-top:8px;font-size:13px;color:var(--muted)}
.mini b{color:var(--text);font-weight:600}

/* add */
.steps{list-style:none;padding:0;margin:14px 0 0;display:grid;gap:12px}
.steps li{display:flex;gap:12px;align-items:flex-start}
.steps .n{width:24px;height:24px;border-radius:50%;background:var(--accent-soft);color:var(--accent);
  font-size:13px;font-weight:650;display:flex;align-items:center;justify-content:center;flex:none}
.found{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 0;border-top:1px solid var(--line)}
.found:first-of-type{border-top:none}
.sig{display:inline-flex;gap:2px;align-items:flex-end;height:14px;margin-right:8px;vertical-align:-1px}
.sig i{width:3px;border-radius:1px;background:var(--line)}.sig i.on{background:var(--good)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}

/* settings / debug */
details.more{margin-top:16px}
details.more>summary{list-style:none;cursor:pointer;padding:16px 22px;font-weight:600;display:flex;align-items:center;gap:8px}
details.more>summary::-webkit-details-marker{display:none}
details.more>summary .chev{margin-left:auto;transition:transform .15s;color:var(--faint)}
details.more[open]>summary .chev{transform:rotate(90deg)}
details.more .body{padding:0 22px 20px}
.form{display:grid;grid-template-columns:1fr 160px auto;gap:12px;align-items:end}
label{display:block;font-size:13px;color:var(--muted);margin-bottom:6px}
input{font:inherit;width:100%;padding:9px 12px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--text)}
input:focus{outline:2px solid var(--accent-soft);border-color:var(--accent)}
.hint{font-size:12px;color:var(--faint);margin-top:6px}
.sep{height:1px;background:var(--line);margin:18px 0}
.runs a{color:var(--accent);text-decoration:none}
.run{display:flex;justify-content:space-between;padding:8px 0;font-size:13px;border-top:1px solid var(--line)}
.run:first-child{border-top:none}

@media (max-width:720px){
  .hero{grid-template-columns:1fr;justify-items:start;gap:18px}
  .stats{grid-template-columns:1fr 1fr}
  .cols{grid-template-columns:1fr}
  .form{grid-template-columns:1fr}
  main{padding:20px 14px 40px}
}
</style></head><body><main>
<div class="top"><h1>Bosch eBike</h1><div id="topact"></div></div>
<div id="app"><p class="muted">Loading…</p></div>
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
const NS = "http://www.w3.org/2000/svg";
const ICONS = {   // simple 24px line icons
  battery: "M3 8h15a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1zM22 11v2",
  heart: "M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z",
  cycle: "M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6",
  road: "M6 21a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM18 7a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM6 17V11a4 4 0 0 1 4-4h6M18 7v6a4 4 0 0 1-4 4H8",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
  motor: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  plug: "M9 2v6M15 2v6M7 8h10v4a5 5 0 0 1-10 0V8zM12 17v5",
  chip: "M9 3v2M15 3v2M9 19v2M15 19v2M3 9h2M3 15h2M19 9h2M19 15h2M7 5h10a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z",
  refresh: "M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5",
  plus: "M12 5v14M5 12h14",
  back: "M15 18l-6-6 6-6",
  chev: "M9 18l6-6-6-6",
  gear: "M4 7h9M17 7h3M4 17h3M11 17h9M15 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM9 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4z",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v5M12 8h.01",
  live: "M4 12h3l2-6 4 12 2-6h5",
};
function icon(name) {
  const s = document.createElementNS(NS, "svg");
  s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("class", "i");
  const p = document.createElementNS(NS, "path"); p.setAttribute("d", ICONS[name]); s.append(p);
  return s;
}
function ring(pct, sub, size) {
  const r = 44, c = 2 * Math.PI * r, v = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  const s = document.createElementNS(NS, "svg"); s.setAttribute("viewBox", "0 0 100 100");
  for (const [cls, off] of [["track", 0], ["fill", c * (1 - v / 100)]]) {
    const ci = document.createElementNS(NS, "circle");
    ci.setAttribute("cx", 50); ci.setAttribute("cy", 50); ci.setAttribute("r", r);
    ci.setAttribute("fill", "none"); ci.setAttribute("stroke-width", 8); ci.setAttribute("class", cls);
    ci.setAttribute("stroke-linecap", "round");
    if (cls === "fill") { ci.setAttribute("stroke-dasharray", c); ci.setAttribute("stroke-dashoffset", off);
      if (pct == null) ci.setAttribute("stroke-opacity", 0); }
    s.append(ci);
  }
  return $("div", {class: "ring"}, s, $("div", {class: "val"},
    $("div", {class: "pct"}, pct == null ? "—" : Math.round(pct) + "%"),
    sub ? $("div", {class: "sub"}, sub) : null));
}

const fmt = (v, d = 0) => (v === null || v === undefined || v === "") ? null :
  Number(v).toLocaleString(undefined, {minimumFractionDigits: d, maximumFractionDigits: d});
function ago(iso) {
  if (!iso) return null;
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 90) return "just now";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  if (s < 129600) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " days ago";
}
const charge = b => b.ldi.battery_soc ?? b.mcsp.state_of_charge ?? null;
function dur(min) {
  if (min == null) return null;
  const h = Math.floor(min / 60), m = Math.round(min % 60);
  return h ? h + " h " + (m ? m + " min" : "") : m + " min";
}
function day(t) {
  return new Date(t * 1000).toLocaleDateString(undefined, {month: "short", day: "numeric"});
}
const km = b => b.ldi.odometer_km ?? (b.mcsp.odometer_m != null ? b.mcsp.odometer_m / 1000 : null);
const charging = b => b.ldi.charger_connected ?? b.mcsp.charging_active ?? null;
// The charge ring shows what the bike says now, or a recent value with its
// age ("96% · 29 min ago"). Older than RECENT_SEC it is unknown: the last
// value could be days old.
const RECENT_SEC = 6 * 3600;
const current = b => b.live || b.reading;
const recent = b => !current(b) && b.last_seen
  && (Date.now() - Date.parse(b.last_seen)) / 1000 < RECENT_SEC;
const ringPct = b => current(b) || recent(b) ? charge(b) : null;
const ringSub = b => current(b) ? (charging(b) ? "charging" : null)
  : recent(b) && charge(b) != null ? ago(b.last_seen) : "unknown";
const hasDetails = b => b.mcsp.battery_health != null || b.mcsp.charge_cycles != null;

function status(b) {
  if (b.reading) return ["busy", "Reading full details…"];
  if (b.live) return ["live", charging(b) ? "Connected · charging" : "Connected"];
  const seen = ago(b.last_seen);
  return ["", seen ? "Not connected · seen " + seen : "Not seen yet"];
}
const REASON = {peek: "when plugged in", charging: "while charging", button: "on request", explored: "on request"};
function freshness(b) {
  if (b.last_good) return "Full details from " + ago(b.last_good.at) + " (" + (REASON[b.last_good.reason] || "") + ")";
  return "No full details yet";
}
// Outcomes worth telling the user about, in plain words. Routine ones stay quiet.
function problem(b) {
  const r = b.last_read; if (!r || b.reading) return null;
  if (b.last_good && Date.parse(b.last_good.at) >= Date.parse(r.at)) return null;
  const o = r.outcome || "";
  if (o.startsWith("not charging") || o.startsWith("no charger answer") || o.startsWith("stopped")
      || o === "replies" || o === "app stopped") return null;
  let why = "it didn't finish";
  if (o.startsWith("no advertisement")) why = "nothing heard from the bike. Switch it on near " +
    "Home Assistant and try again";
  else if (o.startsWith("connect failed")) why = "couldn't connect. Close the Flow app and try again";
  else if (o.includes("sent nothing")) why = "the bike didn't answer";
  else if (o.includes("reconnected for LDI")) why = "the bike reconnected for live data first; try again";
  return "Reading full details " + ago(r.at) + " didn't work: " + why + ".";
}

async function act(action, extra) {
  busy = true; render();
  try {
    await fetch("api/action", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(Object.assign({action}, extra || {}))});
  } finally { setTimeout(() => { busy = false; load(); }, 700); }
}
const go = v => { view = v; location.hash = v; editing = false; render(); window.scrollTo(0, 0); };

function stat(ic, label, value, unit, hint, barPct) {
  return $("div", {class: "card stat"},
    $("div", {class: "k"}, icon(ic), label),
    $("div", {class: "v"}, value ?? "—", value != null && unit ? $("small", {}, unit) : null),
    hint ? $("div", {class: "h"}, hint) : null,
    barPct != null ? $("div", {class: "bar"}, $("i", {style: "width:" + barPct + "%"})) : null);
}
function rows(list) {
  const items = list.filter(r => r && r[1] !== null && r[1] !== undefined);
  return $("div", {class: "rows"}, items.map(([l, r]) => [$("div", {class: "l"}, l), $("div", {class: "r"}, r)]));
}
function section(ic, title, content) {
  return $("div", {class: "card pad sec"}, $("h3", {}, icon(ic), title), content);
}

function healthHint(b) {
  const f = b.health_first, m = b.mcsp;
  if (!f || m.battery_health == null) return null;
  const when = new Date(f.at).toLocaleDateString(undefined, {month: "short", day: "numeric"});
  return f.value === m.battery_health ? "same as on " + when : "was " + f.value + "% on " + when;
}
function statsCard(b) {
  const s = b.stats || {};
  const lc = s.last_charge, ch = s.charging, lr = s.last_ride;
  const rows = [];
  if (ch && !ch.full && b.live) rows.push(["Charging now", (ch.soc != null ? Math.round(ch.soc) + "%" : "—") +
    (ch.minutes_to_full != null ? " · full in about " + dur(ch.minutes_to_full) : "")]);
  if (lc) rows.push(["Last charge", Math.round(lc.start_soc) + "% → " + Math.round(lc.end_soc) +
    "% in " + dur(lc.minutes) + " (" + day(lc.start_t) + ")"]);
  if (s.full_charge_minutes) rows.push(["A full charge takes about", dur(s.full_charge_minutes)]);
  if (lr) rows.push(["Last ride", fmt(lr.km, 1) + " km" + (lr.used_pct != null ? " · " +
    Math.round(lr.used_pct) + "% of the battery" : "") + (lr.wh ? " · " + fmt(lr.wh) + " Wh" : "")]);
  if (s.wh_per_km) rows.push(["Energy per km", fmt(s.wh_per_km, 1) + " Wh/km"]);
  if (s.range_full_km) rows.push(["A full battery is good for", "about " + s.range_full_km +
    " km the way you ride"]);
  if (s.charges) rows.push(["Recorded", s.charges + (s.charges === 1 ? " charge" : " charges") +
    (s.km_recorded ? " · " + fmt(s.km_recorded, 1) + " km of riding" : "")]);
  return section("cycle", "Charging & riding", rows.length ? rowsEl(rows)
    : $("div", {class: "empty"}, "Builds up as you charge and ride."));
}
function rideCard(b) {
  const r = (b.mcsp || {}).ride || {}, m = b.mcsp || {};
  if (r.start_time == null && r.moving_seconds == null) return null;
  const odo = m.odometer_m ?? (b.ldi.odometer_km != null ? b.ldi.odometer_km * 1000 : null);
  const km = odo != null && r.start_odometer_m != null ? (odo - r.start_odometer_m) / 1000 : null;
  const when = r.start_time ? new Date(r.start_time * 1000).toLocaleString(undefined,
    {weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}) : null;
  const t = r.tricks || {}, br = r.brakes || {};
  const n = (o, k) => o[k] ?? o[String(k)] ?? 0;
  const tricks = [["jump", n(t, 1)], ["wheelie", n(t, 2)], ["manual", n(t, 3)], ["stoppie", n(t, 13)]]
    .filter(([, c]) => c).map(([w, c]) => c + " " + w + (c === 1 ? "" : "s")).join(" · ");
  const names = (m.range_by_mode ? ["OFF", ...Object.keys(m.range_by_mode)] : null);
  const modes = Array.isArray(r.assist_m) && names ? r.assist_m.map((v, i) => [names[i], v])
    .filter(([nm, v]) => nm && v > 0).map(([nm, v]) => nm + " " + fmt(v / 1000, 1) + " km").join(" · ") : null;
  const rows = [
    ["Started", when],
    ["Distance", km != null && km >= 0 ? fmt(km, 1) + " km" : null],
    ["Moving time", r.moving_seconds != null ? dur(r.moving_seconds / 60) : null],
    ["Average · top speed", r.average_speed != null ? fmt(r.average_speed, 1) + " · " +
      fmt(r.maximum_speed, 1) + " km/h" : null],
    ["Your power (avg · max)", r.average_power ? fmt(r.average_power) + " · " + fmt(r.maximum_power) + " W" : null],
    ["Cadence (avg · max)", r.average_cadence ? fmt(r.average_cadence) + " · " + fmt(r.maximum_cadence) + " rpm" : null],
    ["Heart rate (avg · max)", r.average_heart_rate ? fmt(r.average_heart_rate) + " · " +
      fmt(r.maximum_heart_rate) + " bpm" : null],
    ["Energy used", r.energy_wh != null ? fmt(r.energy_wh) + " Wh" : null],
    ["Calories", r.calories ? fmt(r.calories) + " kcal" : null],
    ["Assist modes", modes || null],
    ["Tricks", tricks || (r.tricks ? "none" : null)],
    ["Brake events", r.brakes ? n(br, 1) + (n(br, 2) ? " · " + n(br, 2) + " with ABS" : "") : null],
  ].filter(([, v]) => v != null && v !== "");
  const strava = km != null && r.moving_seconds ? "For Strava's manual entry: " + fmt(km, 1) +
    " km, moving time " + dur(r.moving_seconds / 60) + (when ? ", started " + when : "") + "." : null;
  const card = section("road", "Last ride, from the bike", [rowsEl(rows),
    $("div", {class: "hint", style: "margin-top:12px"}, "The bike's own ride computer, read at the " +
      "last full reading (no map: the route needs the phone's GPS)." + (strava ? " " + strava : ""))]);
  card.classList.add("wide");
  return card;
}
function rowsEl(list) {
  return $("div", {class: "rows"}, list.map(([l, r]) => [$("div", {class: "l"}, l), $("div", {class: "r"}, r)]));
}

function bikePage(b) {
  const m = b.mcsp, [cls, text] = status(b), p = charge(b), ch = charging(b);
  const seenDay = b.last_seen ? new Date(b.last_seen).toLocaleDateString(undefined,
    {month: "short", day: "numeric"}) : null;
  const details = hasDetails(b);
  const fw = b.firmware || [];
  const updates = fw.filter(f => f.newer);
  const issue = problem(b);

  const hero = $("div", {class: "card pad hero"},
    ring(ringPct(b), ringSub(b)),
    $("div", {},
      $("h2", {class: "name"}, b.name),
      $("div", {class: "model"}, [b.model, b.battery].filter(Boolean).join(" · ") || "Bosch smart system"),
      $("div", {class: "pill " + cls}, $("span", {class: "d"}), text),
      $("div", {class: "fresh"}, freshness(b)),
      issue ? $("div", {class: "alert"}, icon("info"), issue) : null,
      $("div", {class: "cta"},
        b.reading
          ? $("button", {onclick: () => act("stop"), disabled: busy}, "Stop")
          : $("button", {class: "primary", onclick: () => act("read", {address: b.address}), disabled: busy},
              icon("refresh"), "Read full details")),
      $("p", {class: "explain"}, b.reading
        ? "This takes about a minute. The Flow app can't connect to the bike until it's done."
        : "Battery health, charge cycles, motor hours, temperatures and software versions come " +
          "straight from the bike. They're read automatically when you plug it in. To read them now, " +
          "keep the bike switched on and parked; the Flow app can't connect for about a minute.")));

  const stats = $("div", {class: "stats"},
    stat("heart", "Battery health", fmt(m.battery_health), "%", !details ? "after a full reading"
         : healthHint(b), m.battery_health ?? null),
    stat("cycle", "Charge cycles", fmt(m.charge_cycles, 1), null, details ? "full cycles so far" : "after a full reading"),
    stat("road", "Distance", fmt(km(b)), "km", "odometer"),
    stat("clock", "Motor hours", fmt(m.motor_support_hours), "h",
         m.power_on_hours != null ? fmt(m.power_on_hours) + " h switched on" : "after a full reading"));

  const swRows = fw.length ? $("div", {class: "rows"}, fw.map(f => [
      $("div", {class: "l"}, f.title),
      $("div", {class: "r"}, f.installed,
        f.newer ? $("span", {class: "chip new"}, f.latest + " available")
          : f.latest ? $("span", {class: "chip ok"}, "latest") : null)]))
    : $("div", {class: "empty"}, "Shown after a full reading.");
  const msgs = m.messages || [];
  const messages = msgs.length ? $("div", {class: "card pad sec", style: "margin-top:16px"},
    $("h3", {}, icon("info"), "Messages from the bike"),
    msgs.map(f => $("div", {class: "msg"},
      $("div", {}, $("span", {class: "chip " + (f.severity === "Info" ? "ok" : "new"),
        style: "margin-left:0;margin-right:8px"}, f.severity || "Message"),
        $("span", {class: "muted small"}, f.component)),
      $("div", {style: "margin-top:6px;font-weight:550"}, f.title),
      f.description ? $("div", {class: "small muted", style: "margin-top:2px"}, f.description) : null,
      f.measures ? $("div", {class: "small", style: "margin-top:4px"}, f.measures) : null,
      $("div", {class: "hint"}, "Code " + f.code)))) : null;
  const cols = $("div", {class: "cols"},
    rideCard(b),
    statsCard(b),
    section("battery", "Battery", details || p != null ? rows([
      ["Charge", p == null ? null : current(b) ? Math.round(p) + "%"
        : recent(b) ? Math.round(p) + "% · " + ago(b.last_seen)
        : "Unknown" + (seenDay ? " · was " + Math.round(p) + "% on " + seenDay : "")],
      ["Energy left", fmt(m.remaining_energy) && fmt(m.remaining_energy) + " Wh"],
      ["Usable energy when full", fmt(m.system_usable_energy) && fmt(m.system_usable_energy) + " Wh"],
      ["Capacity", fmt(m.battery_capacity, 1) && fmt(m.battery_capacity, 1) + " Ah"],
      ["Temperature", fmt(m.battery_temperature, 1) && fmt(m.battery_temperature, 1) + " °C"],
      ["Energy delivered so far", fmt(m.lifetime_delivered_wh) && fmt(m.lifetime_delivered_wh) + " Wh"],
    ]) : $("div", {class: "empty"}, "Shown after a full reading.")),
    section("plug", "Charging", rows([
      ["Charger", !current(b) ? (recent(b) && ch != null
          ? (ch ? "Plugged in" : "Not plugged in") + " · " + ago(b.last_seen) : "Unknown")
        : ch == null ? "—" : !ch ? "Not plugged in"
        : (p != null && p >= 100) ? "Plugged in · full" : "Plugged in · charging"],
      ["Charging mode", m.charging_mode ?? "—"],
      ["Charge limit", m.charge_limit != null ? m.charge_limit + "%" : (m.charging_mode ? "None set" : "—")],
    ])),
    section("road", "Range", m.range_by_mode && Object.keys(m.range_by_mode).length
      ? [rows(Object.entries(m.range_by_mode).map(([mode, k]) => [mode, fmt(k) + " km"])),
         $("div", {class: "hint", style: "margin-top:12px"}, "The bike's own estimate per assist mode, " +
           "for the charge at the last full reading.")]
      : $("div", {class: "empty"}, "Shown after a full reading.")),
    section("motor", "Motor", details ? rows([
      ["Hours with motor help", fmt(m.motor_support_hours) && fmt(m.motor_support_hours) + " h"],
      ["Hours switched on", fmt(m.power_on_hours) && fmt(m.power_on_hours) + " h"],
      ["Temperature", fmt(m.drive_pcb_temperature, 1) && fmt(m.drive_pcb_temperature, 1) + " °C"],
    ]) : $("div", {class: "empty"}, "Shown after a full reading.")),
    section("chip", "Software", [swRows,
      updates.length ? $("div", {class: "hint", style: "margin-top:12px"},
        "Install updates with the Bosch Flow app.") : null]));

  const live = b.live_setup ? null : $("details", {class: "card more"},
    $("summary", {}, icon("live"), "Live riding data", $("span", {class: "muted small", style: "font-weight:400"},
      "· speed, cadence and your pedalling power"), $("span", {class: "chev"}, icon("chev"))),
    $("div", {class: "body"},
      $("p", {class: "small", style: "margin-top:0"},
        "Charge and distance already update whenever the bike is connected. For live speed, cadence and " +
        "your pedalling power while riding, add this app as an accessory in the Flow app:"),
      $("ol", {class: "small", style: "margin:0 0 14px;padding-left:20px"},
        $("li", {}, "Press Start pairing below (it stays open for 5 minutes)."),
        $("li", {}, "In the Flow app: your bike → Settings → Components → Add new device."),
        $("li", {}, "Pick “" + (S.bridge_name || "Home Assistant Reader") + "”.")),
      $("button", {disabled: busy || S.pairing_open, onclick: () => act("start_pairing")},
        S.pairing_open ? "Pairing open" : "Start pairing")));

  const settings = $("details", {class: "card more"},
    $("summary", {}, icon("gear"), "Bike settings", $("span", {class: "chev"}, icon("chev"))),
    $("div", {class: "body"},
      $("div", {class: "form"},
        $("div", {}, $("label", {for: "nm"}, "Name"),
          $("input", {id: "nm", value: b.name, maxlength: 40,
            onfocus: () => editing = true, onblur: () => editing = false})),
        $("div", {}, $("label", {for: "wh"}, "Battery size (Wh)"),
          $("input", {id: "wh", type: "number", min: 100, max: 5000, step: 1, value: b.battery_wh,
            onfocus: () => editing = true, onblur: () => editing = false})),
        $("button", {onclick: () => { editing = false; act("settings", {address: b.address,
          name: document.getElementById("nm").value, battery_wh: document.getElementById("wh").value}); }},
          "Save")),
      $("div", {class: "hint"}, "Battery size is only used to estimate the remaining energy."),
      $("div", {class: "sep"}),
      b.configured
        ? $("div", {class: "hint"}, "This bike is listed in the app's configuration; remove it there.")
        : $("button", {class: "danger", disabled: busy, onclick: () => {
            if (confirm("Remove " + b.name + "? You can add it again later.")) { act("remove", {address: b.address}); go(""); }
          }}, "Remove this bike"),
      $("div", {class: "hint", style: "margin-top:10px"}, "Bluetooth address " + b.address)));

  return [S.bikes.length > 1 ? $("button", {class: "ghost", style: "margin:-6px 0 10px -8px",
      onclick: () => go("")}, icon("back"), "All bikes") : null,
    hero, stats, messages, cols, live, settings, debugBox()];
}

function overview() {
  return [$("div", {class: "grid"}, S.bikes.map(b => {
    const [cls, text] = status(b), m = b.mcsp;
    return $("div", {class: "card pad bike", onclick: () => go(b.address)},
      ring(ringPct(b), ringSub(b)),
      $("div", {},
        $("div", {style: "font-weight:650;font-size:17px"}, b.name),
        $("div", {class: "pill " + cls, style: "margin-top:6px"}, $("span", {class: "d"}), text),
        $("div", {class: "mini"},
          $("span", {}, "Health ", $("b", {}, m.battery_health != null ? m.battery_health + "%" : "—")),
          $("span", {}, $("b", {}, km(b) != null ? fmt(km(b)) + " km" : "—")))));
  })), debugBox()];
}

function bars(rssi) {
  const n = rssi == null ? 0 : rssi > -65 ? 4 : rssi > -75 ? 3 : rssi > -85 ? 2 : 1;
  return $("span", {class: "sig", title: rssi == null ? "" : rssi + " dBm"},
    [4, 7, 10, 13].map((h, i) => $("i", {class: i < n ? "on" : "", style: "height:" + h + "px"})));
}
function addPage() {
  const has = S.bikes.length > 0;
  const list = S.nearby.length ? S.nearby.map(n => {
    const state = S.waiting_for === n.address ? "Waiting for the bike…" :
      S.adding.includes(n.address) ? "Adding…" : null;
    return $("div", {class: "found"},
      $("div", {}, $("div", {}, bars(n.rssi), $("span", {class: "mono"}, n.address)),
        $("div", {class: "small muted", style: "margin-top:2px"},
          n.pairing === "ready to pair" ? "Ready to pair" : "Bosch eBike")),
      state ? $("span", {class: "pill busy", style: "margin:0"}, $("span", {class: "d"}), state)
        : $("button", {class: "primary", disabled: busy, onclick: () => act("add", {address: n.address})},
            "This is my bike"));
  }) : [$("div", {class: "empty", style: "padding:6px 0"}, "Listening… your bike appears here within a minute.")];
  return [
    has ? $("button", {class: "ghost", style: "margin:-6px 0 10px -8px", onclick: () => go("")},
      icon("back"), "Back") : null,
    $("div", {class: "card pad"},
      $("h2", {class: "name"}, has ? "Add another bike" : "Add your bike"),
      $("ol", {class: "steps"},
        $("li", {}, $("span", {class: "n"}, "1"), $("div", {}, "Plug the bike into its charger next to Home Assistant, or switch it on.")),
        $("li", {}, $("span", {class: "n"}, "2"), $("div", {}, "When it appears below, press ", $("b", {}, "This is my bike"),
          ". The one with the strongest signal is usually yours.")),
        $("li", {}, $("span", {class: "n"}, "3"), $("div", {}, "If that doesn't work, switch the bike on and hold the " +
          "System Controller's on/off button for 3 seconds until the top bar flashes blue, then try again.")))),
    $("div", {class: "card pad", style: "margin-top:16px"},
      $("h3", {style: "font-size:15px;margin:0 0 6px"}, "Bosch bikes nearby"), list),
    debugBox()];
}

function debugBox() {
  return $("details", {class: "card more", id: "dbg"},
    $("summary", {}, $("span", {class: "muted", style: "font-weight:500"}, "Debug captures"),
      $("span", {class: "chev"}, icon("chev"))),
    $("div", {class: "body runs"},
      $("p", {class: "hint", style: "margin-top:0"},
        "If something doesn't work, download the newest capture and send it with the app's log."),
      S.runs && S.runs.length ? S.runs.map((r, i) => $("div", {class: "run"},
        $("span", {class: "mono"}, r.name, i === 0 ? $("span", {class: "muted"}, "  (current)") : null),
        $("a", {href: "download/" + r.name + ".zip"}, r.mb + " MB · download")))
        : $("div", {class: "hint"}, "No captures (debug capture is off).")));
}

function render() {
  if (!S || editing) return;
  const openDbg = document.getElementById("dbg")?.open;
  const openMore = [...document.querySelectorAll("details.more")].map(d => d.open);
  const one = S.bikes.length === 1 && view !== "add";
  const bike = S.bikes.find(b => b.address === view) || (one ? S.bikes[0] : null);
  const content = (view === "add" || !S.bikes.length) ? addPage() : bike ? bikePage(bike) : overview();
  document.getElementById("app").replaceChildren(...[content].flat().filter(Boolean));
  [...document.querySelectorAll("details.more")].forEach((d, i) => { if (openMore[i]) d.open = true; });
  if (openDbg && document.getElementById("dbg")) document.getElementById("dbg").open = true;
  document.getElementById("topact").replaceChildren(S.bikes.length && view !== "add"
    ? $("button", {onclick: () => go("add")}, icon("plus"), "Add bike") : "");
}
async function load() {
  try { S = await (await fetch("api/state", {cache: "no-store"})).json(); render(); }
  catch (e) { document.getElementById("app").replaceChildren(
    $("div", {class: "card pad muted"}, "The app isn't answering. Is it running?")); }
}
window.onhashchange = () => { view = location.hash.slice(1); editing = false; render(); };
load(); setInterval(() => { if (!busy) load(); }, 2000);
</script></body></html>
"""
