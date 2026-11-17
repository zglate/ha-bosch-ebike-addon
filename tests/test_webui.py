"""Tests for the capture download page."""
import urllib.error
import urllib.request
import zipfile
import io

import webui


def test_lists_runs_newest_first_and_zips_one(tmp_path):
    for name in ("run-20261116-170513", "run-20261116-152945"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "report.json").write_text("{}")
    (tmp_path / "not-a-run").mkdir()
    assert [p.name for p in webui.runs(tmp_path)] == ["run-20261116-170513",
                                                      "run-20261116-152945"]
    server = webui.start(str(tmp_path), port=0)
    port = server.server_address[1]
    try:
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        assert 'href="download/run-20261116-170513.zip"' in page and "not-a-run" not in page
        data = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/download/run-20261116-170513.zip").read()
        assert zipfile.ZipFile(io.BytesIO(data)).namelist() == [
            "run-20261116-170513/report.json"]
        for bad in ("/download/..%2Fetc.zip", "/download/not-a-run.zip", "/report.json"):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}{bad}")
                raise AssertionError(bad)
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
    finally:
        server.shutdown()
