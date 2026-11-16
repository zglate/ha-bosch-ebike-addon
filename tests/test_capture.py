"""Tests for the debug capture and report."""
import json
import os
import time

from capture import Capture


def run_dirs(base):
    return sorted(p for p in base.glob("run-*") if p.is_dir())


def read_events(run_dir):
    lines = []
    for path in sorted(run_dir.glob("capture-*.jsonl")):
        lines += [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    return lines


def test_each_start_gets_its_own_run_folder(tmp_path):
    first, second = Capture(str(tmp_path)), Capture(str(tmp_path))
    first.close(), second.close()
    assert len(run_dirs(tmp_path)) == 2
    assert first.run_dir != second.run_dir


def test_events_are_json_lines_with_bytes_as_hex(tmp_path):
    cap = Capture(str(tmp_path))
    cap.event("rx", bike="AA", data=b"\x30\x02\x01\x03")
    cap.close()
    (event,) = read_events(run_dirs(tmp_path)[0])
    assert event["kind"] == "rx" and event["data"] == "30020103" and "t" in event


def test_report_records_datapoint_outcomes_and_summary(tmp_path):
    cap = Capture(str(tmp_path))
    cap.datapoint("AA", 0x1806, "DriveUnit.softwareVersion", value="21.12.0", status="SUCCESS")
    cap.datapoint("AA", 0x1806, "DriveUnit.softwareVersion", value="21.12.0", status="SUCCESS")
    cap.datapoint("AA", 0x2112, "RemoteControl.x", status="DENIED")
    assert cap.write_report() is True
    report = json.loads((run_dirs(tmp_path)[0] / "report.json").read_text(encoding="utf-8"))
    bike = report["bikes"]["AA"]
    assert bike["datapoints"]["0x1806"]["value"] == "21.12.0"
    assert bike["datapoints"]["0x1806"]["count"] == 2
    assert bike["summary"] == {"datapoints": 2, "by_status": {"SUCCESS": 1, "DENIED": 1}}


def test_files_of_the_current_run_are_never_deleted(tmp_path):
    cap = Capture(str(tmp_path), file_bytes=200)
    for i in range(50):
        cap.event("rx", data=b"\x00" * 40, i=i)
    cap.close()
    events = read_events(run_dirs(tmp_path)[0])
    assert [e["i"] for e in events] == list(range(50))       # nothing lost
    assert len(list(run_dirs(tmp_path)[0].glob("capture-*.jsonl"))) > 1


def test_old_runs_are_pruned_as_whole_folders(tmp_path):
    for i in range(4):
        Capture(str(tmp_path), keep_runs=3).close()
        old = time.time() - 100 + i                           # distinct mtimes
        os.utime(run_dirs(tmp_path)[-1], (old, old))
    Capture(str(tmp_path), keep_runs=3).close()
    assert len(run_dirs(tmp_path)) == 3


def test_raw_events_stop_at_the_limit_but_results_continue(tmp_path):
    cap = Capture(str(tmp_path), raw_limit=500)
    for i in range(20):
        cap.event("rx", data=b"\x00" * 20, i=i)
    cap.event("datapoint", id="0x1806", value="21.12.0")
    cap.close()
    kinds = [e["kind"] for e in read_events(run_dirs(tmp_path)[0])]
    assert "raw_capture_stopped" in kinds
    assert kinds.count("rx") < 20 and kinds[-1] == "datapoint"


def test_disabled_capture_writes_nothing(tmp_path):
    cap = Capture(None)
    cap.event("rx", data=b"\x00")
    cap.datapoint("AA", 1, "x", value=1)
    cap.write_report()
    assert not cap.enabled and list(tmp_path.iterdir()) == []


def test_unwritable_directory_disables_capture(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    cap = Capture(str(blocker / "sub"))        # parent is a file
    assert not cap.enabled
