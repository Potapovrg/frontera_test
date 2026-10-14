"""Auto Test page: static rendering and JS syntax checks."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from testbench import pages  # noqa: E402
from testbench.config import Config  # noqa: E402


def _cfg(tmp_path) -> Config:
    c = Config(mock=True, results_dir=tmp_path)
    c.ensure_dirs()
    return c


def _all_pages(tmp_path):
    cfg = _cfg(tmp_path)
    return {
        "auto": pages.auto_page(cfg),
        "single": pages.single_page(cfg),
        "comparison": pages.comparison_page(cfg),
        "journal": pages.journal_page(),
    }


def _scripts(html):
    return re.findall(r"<script>(.*?)</script>", html, re.S)


def test_auto_page_renders(tmp_path):
    html = pages.auto_page(_cfg(tmp_path))
    for needle in ("auto_run", "auto_stop", "auto_canvas", "report_btn",
                   "/api/auto/start", "/api/auto/status", "/api/auto/stop",
                   "/api/auto/report", "Auto Test"):
        assert needle in html, needle
    assert re.search(r'<option value="-14" selected>', html)


def test_nav_has_auto(tmp_path):
    for name, html in _all_pages(tmp_path).items():
        assert '<a href="/auto">Auto Test</a>' in html, name


def test_no_leftover_placeholders(tmp_path):
    for name, html in _all_pages(tmp_path).items():
        for body in _scripts(html):
            assert not re.search(r"__[A-Z_]+__", body), name


def test_onhardware_defined_in_same_script(tmp_path):
    for name in ("auto", "single", "comparison"):
        scripts = _scripts(_all_pages(tmp_path)[name])
        assert len(scripts) == 1
        assert "function onHardware(" in scripts[0] and "onHardware(HW)" in scripts[0]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_scripts_pass_node_check(tmp_path):
    for name, html in _all_pages(tmp_path).items():
        for k, body in enumerate(_scripts(html)):
            f = tmp_path / f"{name}_{k}.js"
            f.write_text(body, encoding="utf-8")
            r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, f"{name}: {r.stderr}"
