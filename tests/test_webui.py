"""Tests for the add-on's page: overview data, actions, capture downloads."""
import io
import json
import urllib.error
import urllib.request
import zipfile

import pytest

import webui


@pytest.fixture
def server(tmp_path):
    for name in ("run-20261116-170513", "run-20261116-152945"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "report.json").write_text("{}")
    (tmp_path / "not-a-run").mkdir()
    calls = []
    state = {"version": "0.7.0", "bikes": [{"address": "00:04:63:AA:BB:01", "name": "Range"}]}
    srv = webui.start(str(tmp_path), port=0, state=lambda: state,
                      action=lambda a, p: calls.append((a, p)))
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, calls
    srv.shutdown()


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req)


def test_page_and_state(server):
    base, _ = server
    page = urllib.request.urlopen(base + "/").read().decode()
    assert "<title>Bosch eBike</title>" in page and "api/state" in page
    state = json.loads(urllib.request.urlopen(base + "/api/state").read())
    assert state["bikes"][0]["name"] == "Range"
    assert [r["name"] for r in state["runs"]] == ["run-20261116-170513", "run-20261116-152945"]


def test_actions_reach_the_bridge_and_unknown_ones_are_refused(server):
    base, calls = server
    assert json.loads(post(base + "/api/action",
                           {"action": "read", "address": "00:04:63:AA:BB:01"}).read())["ok"]
    assert calls == [("read", {"address": "00:04:63:AA:BB:01"})]
    with pytest.raises(urllib.error.HTTPError) as err:
        post(base + "/api/action", {"action": "write_setting"})
    assert err.value.code == 400 and len(calls) == 1


def test_downloads_a_run_and_nothing_else(server):
    base, _ = server
    data = urllib.request.urlopen(base + "/download/run-20261116-170513.zip").read()
    assert zipfile.ZipFile(io.BytesIO(data)).namelist() == ["run-20261116-170513/report.json"]
    for bad in ("/download/..%2Fetc.zip", "/download/not-a-run.zip", "/report.json"):
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(base + bad)
        assert err.value.code == 404


def test_lists_runs_newest_first(tmp_path):
    for name in ("run-20261116-152945", "run-20261117-082447", "other"):
        (tmp_path / name).mkdir()
    assert [p.name for p in webui.runs(tmp_path)] == ["run-20261117-082447",
                                                      "run-20261116-152945"]
    assert webui.runs(None) == []
