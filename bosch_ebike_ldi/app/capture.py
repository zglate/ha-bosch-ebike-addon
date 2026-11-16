"""
Debug capture: everything the add-on sees and sends, written to /share.

Each add-on start is one *run*, in its own folder (reachable through the
Samba share):

  /share/bosch_ebike/run-YYYYmmdd-HHMMSS/
      capture-NNN.jsonl   one JSON object per event: adverts, device and GATT
                          inventory, every raw notification and write, every
                          decoded MCSP message, milestones, pairing decisions.
                          A new numbered file starts every 20 MB; files of the
                          current run are never deleted.
      report.json         a summary, rewritten as things change: run details,
                          what each bike exposed, every datapoint's latest
                          value or error, session milestones.

Old runs are pruned as whole folders (the newest KEEP_RUNS are kept). If a
run's raw capture passes RAW_LIMIT, raw byte events stop (decoded results and
milestones keep being written) so a long ride can't fill the SD card.

No credentials are ever written; nothing here is sent to MQTT.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

RAW_KINDS = {"rx", "tx", "ldi", "gatt_notify", "device_changed"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Capture:
    def __init__(self, base_dir: str | None, file_bytes: int = 20_000_000,
                 keep_runs: int = 20, raw_limit: int = 1_000_000_000) -> None:
        self._dir: Path | None = None
        self._file = None
        self._file_index = 0
        self._file_bytes, self._raw_limit = file_bytes, raw_limit
        self._written = 0
        self._raw_stopped = False
        self._dirty = False
        self.report: dict = {"started": now_iso(), "run": {}, "bikes": {}, "adverts": {}}
        if not base_dir:
            return
        try:
            base = Path(base_dir)
            base.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            run_dir, n = base / f"run-{stamp}", 1
            while run_dir.exists():
                run_dir, n = base / f"run-{stamp}-{n}", n + 1
            run_dir.mkdir()
            self._dir = run_dir
            self._prune(base, keep_runs)
            self._next_file()
        except OSError as exc:
            log.error("Capture disabled, cannot write %s: %s", base_dir, exc)
            self._dir = None

    @property
    def enabled(self) -> bool:
        return self._dir is not None

    @property
    def run_dir(self) -> str | None:
        return str(self._dir) if self._dir else None

    # ── Raw event log ────────────────────────────────────────────────────────

    def event(self, kind: str, **fields) -> None:
        if not self._file:
            return
        if kind in RAW_KINDS and self._raw_stopped:
            return
        line = json.dumps({"t": now_iso(), "kind": kind, **fields},
                          default=_json_default) + "\n"
        self._file.write(line)
        self._file.flush()
        self._written += len(line)
        if self._file.tell() > self._file_bytes:
            self._next_file()
        if not self._raw_stopped and self._written > self._raw_limit:
            self._raw_stopped = True
            self.event("raw_capture_stopped", bytes_written=self._written,
                       note="raw rx/tx/ldi events stop; decoded results continue")

    def _next_file(self) -> None:
        if self._file:
            self._file.close()
        self._file_index += 1
        path = self._dir / f"capture-{self._file_index:03d}.jsonl"
        self._file = open(path, "a", encoding="utf-8")
        log.info("Debug capture: %s", path)

    @staticmethod
    def _prune(base: Path, keep_runs: int) -> None:
        """Delete whole old runs, oldest first, keeping the newest keep_runs."""
        runs = sorted((p for p in base.glob("run-*") if p.is_dir()),
                      key=lambda p: p.stat().st_mtime)
        for old in runs[:max(0, len(runs) - keep_runs)]:
            shutil.rmtree(old, ignore_errors=True)

    # ── Summary report ───────────────────────────────────────────────────────

    def bike(self, address: str) -> dict:
        self._dirty = True
        return self.report["bikes"].setdefault(address, {
            "gatt": {}, "datapoints": {}, "mcsp": {}, "bike_requests": {}, "sessions": [],
        })

    def advert(self, address: str, info: dict) -> None:
        self.report["adverts"][address] = {**info, "seen": now_iso()}
        self._dirty = True

    def datapoint(self, address: str, dp_id: int, label: str, **result) -> None:
        """Record the latest outcome for one datapoint of one bike."""
        key = f"0x{dp_id:04X}"
        entry = self.bike(address)["datapoints"].setdefault(
            key, {"label": label, "first_seen": now_iso(), "count": 0})
        entry.update(result, last_seen=now_iso(), count=entry["count"] + 1)

    def write_report(self) -> bool:
        """Write report.json if anything changed. Returns True (for GLib timers)."""
        if self._dir and self._dirty:
            for bike in self.report["bikes"].values():
                statuses: dict = {}
                for dp in bike["datapoints"].values():
                    statuses[dp.get("status", "?")] = statuses.get(dp.get("status", "?"), 0) + 1
                bike["summary"] = {"datapoints": len(bike["datapoints"]), "by_status": statuses}
            tmp = self._dir / "report.json.tmp"
            tmp.write_text(json.dumps(self.report, indent=1, sort_keys=True,
                                      default=_json_default), encoding="utf-8")
            os.replace(tmp, self._dir / "report.json")
            self._dirty = False
        return True

    def close(self) -> None:
        self.write_report()
        if self._file:
            self._file.close()
            self._file = None


def _json_default(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return str(value)
