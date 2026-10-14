"""Auto Test HTTP routes against a real server (mock device)."""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from testbench.autotest import AutoTestRunner  # noqa: E402
from testbench.config import Config  # noqa: E402
from testbench.db import JournalDB  # noqa: E402
from testbench.device import DeviceManager  # noqa: E402
from testbench.server import make_handler  # noqa: E402


@pytest.fixture
def srv(tmp_path):
    cfg = Config(mock=True, results_dir=tmp_path)
    cfg.ensure_dirs()
    device = DeviceManager(cfg)
    db = JournalDB(str(tmp_path / "journal.db"))
    runner = AutoTestRunner(cfg, device)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, device, db, runner))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, cfg, db
    try:
        runner.stop()
    except Exception:
        pass
    httpd.shutdown()
    httpd.server_close()


def call(base, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data,
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_done(base):
    deadline = time.time() + 20
    while time.time() < deadline:
        _, st = call(base, "/api/auto/status")
        if st["state"] != "running":
            return st
        time.sleep(0.1)
    raise AssertionError("auto test did not finish")


def test_page(srv):
    code, html = call(srv[0], "/auto")
    assert code == 200 and "auto_canvas" in html


def test_run_status_report_and_unblock(srv):
    base, cfg, db = srv
    code, st = call(base, "/api/auto/start",
                    {"start": 1000, "stop": 1010, "step": 5, "window": 20})
    assert code == 200 and st["state"] == "running"
    st = wait_done(base)
    assert st["state"] == "done"
    n = len(st["points"])
    assert n == 3
    code, part = call(base, "/api/auto/status?since=1")
    assert code == 200 and len(part["points"]) == part["next"] - 1
    code, part = call(base, "/api/auto/status?since=bogus")
    assert code == 200 and len(part["points"]) == n

    code, rep = call(base, "/api/auto/report", {})
    assert code == 200
    assert (cfg.reports_dir / rep["pdf_url"].rsplit("/", 1)[1]).is_file()
    row = db.get(rep["comparison_id"])
    assert row["kind"] == "auto"
    assert row["n_pass"] + row["n_fail"] == n
    assert row["gen_level_dbm"] == -14

    code, _ = call(base, "/api/sweep", {"block": "S", "start": 1000, "stop": 1100, "step": 1})
    assert code == 200


def test_blocked_and_conflict_while_running(srv):
    base, _, _ = srv
    code, st = call(base, "/api/auto/start", {"start": 1000, "stop": 1200, "step": 1})
    assert code == 200
    try:
        assert call(base, "/api/sweep", {"block": "S", "start": 1000, "stop": 1100, "step": 1})[0] == 409
        assert call(base, "/api/hardware/probe", {})[0] == 409
        assert call(base, "/api/auto/start", {"start": 1000, "stop": 1200})[0] == 409
        assert call(base, "/api/auto/report", {})[0] == 409
    finally:
        code, _ = call(base, "/api/auto/stop", {})
        assert code == 200
        wait_done(base)


def test_bad_params(srv):
    code, body = call(srv[0], "/api/auto/start",
                      {"start": 1000, "stop": 1010, "level_dbm": -15})
    assert code == 400 and "error" in body


def test_report_before_run(srv):
    code, body = call(srv[0], "/api/auto/report", {})
    assert code == 400 and "error" in body
