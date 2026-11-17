"""
The app's "Open web UI" page (Home Assistant ingress): download the debug
capture of a run as a zip, without Samba or SSH.

Read-only and tiny: lists the run folders under the capture directory and
zips one on request. Only the Supervisor's ingress proxy may connect, and
only names shaped like a run folder are accepted (no paths).
"""
from __future__ import annotations

import html
import logging
import os
import re
import shutil
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

PORT = int(os.environ.get("BOSCH_WEBUI_PORT", 8099))           # = ingress_port
# The Supervisor's ingress proxy; tests connect from localhost.
ALLOWED_CLIENTS = {"172.30.32.2", "127.0.0.1"}
RUN_NAME = re.compile(r"^run-\d{8}-\d{6}(-\d+)?$")


def runs(base: Path) -> list[Path]:
    """Run folders, newest first."""
    if not base.is_dir():
        return []
    return sorted((p for p in base.iterdir() if p.is_dir() and RUN_NAME.match(p.name)),
                  key=lambda p: p.name, reverse=True)


_ZIP_LOCK = threading.Lock()     # one zip at a time: a run can be large


def zip_run(run_dir: Path, out) -> None:
    """Write the run folder as a zip into the file object `out`."""
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(run_dir.iterdir()):
            if path.is_file():
                zf.write(path, f"{run_dir.name}/{path.name}")


def page(base: Path) -> str:
    items = []
    for i, run in enumerate(runs(base)):
        size = sum(f.stat().st_size for f in run.iterdir() if f.is_file())
        label = " (current)" if i == 0 else ""
        # Relative links: ingress serves this page under a token path.
        items.append(f'<li><a href="download/{html.escape(run.name)}.zip">'
                     f'{html.escape(run.name)}</a>{label} – {size / 1e6:.1f} MB</li>')
    listing = "\n".join(items) or "<li>No runs yet.</li>"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Bosch eBike – debug captures</title>
<style>body{{font-family:sans-serif;margin:2em;line-height:1.6}}</style></head><body>
<h2>Debug captures</h2>
<p>Each run is one start of the app: <code>report.json</code> (summary) and
<code>capture-*.jsonl</code> (every byte, with timestamps). Download the newest
and send it along with the app's log.</p>
<ul>{listing}</ul></body></html>"""


def make_handler(base: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.client_address[0] not in ALLOWED_CLIENTS:
                self.send_error(403)
                return
            path = self.path.split("?", 1)[0].rstrip("/")
            name = path.rsplit("/", 1)[-1]
            if path in ("", "/") or path.endswith("/index.html"):
                self._send(200, "text/html; charset=utf-8", page(base).encode())
            elif path.startswith("/download/") and name.endswith(".zip") \
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

        def _send(self, code: int, content_type: str, body: bytes, headers=None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    return Handler


def start(base_dir: str, port: int = PORT) -> ThreadingHTTPServer | None:
    """Serve the page in a background thread; never fatal."""
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(Path(base_dir)))
    except OSError as exc:
        log.warning("Web UI (capture downloads) not started: %s", exc)
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Web UI: capture downloads on port %d (the app's Open web UI button)", port)
    return server
