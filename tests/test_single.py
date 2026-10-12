"""Single-device test page: peak marker, 2-page report, journal `kind` column."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from testbench import pages, plotting  # noqa: E402
from testbench.config import Config  # noqa: E402
from testbench.db import JournalDB  # noqa: E402


def _cfg(tmp_path) -> Config:
    c = Config(mock=True, results_dir=tmp_path)
    c.ensure_dirs()
    return c


def _trace():
    freq = np.linspace(100, 200, 11)
    power = np.array([-80, -70, -60, np.nan, -30, -50, -90, -85, -80, -75, -70], float)
    return freq, power


def test_mark_peak_annotates_the_nan_safe_maximum():
    import matplotlib.pyplot as plt
    freq, power = _trace()
    fig, ax = plt.subplots()
    plotting._draw_spectrum(ax, freq, power, "x", "#000")
    plotting._mark_peak(ax, freq, power)
    assert ax.texts[0].get_text() == "140.0 MHz, -30.0 dBm"
    plt.close(fig)


def test_spectrum_png_and_single_pdf(tmp_path):
    cfg = _cfg(tmp_path)
    freq, power = _trace()
    assert (cfg.plots_dir / plotting.spectrum_png(cfg, "s", "S", freq, power, "t", mark_peak=True)).is_file()
    meta = {"ts_utc": "now", "start_a": 100.0, "stop_a": 200.0, "step_a": 10.0,
            "conditions_a": "c", "setup_a": "x", "peak_dbm_a": -30.0, "peak_freq_a": 140.0}
    assert (cfg.reports_dir / plotting.single_pdf(cfg, "s", freq, power, meta)).stat().st_size > 0


def test_kind_column_migrates_old_db_and_roundtrips(tmp_path):
    path = str(tmp_path / "j.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE comparisons (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL)")
    con.execute("INSERT INTO comparisons (ts_utc) VALUES ('old')")
    con.commit(); con.close()
    db = JournalDB(path)
    assert db.recent()[0]["kind"] == "comparison"
    rid = db.insert({"ts_utc": "t", "kind": "single"})
    assert db.get(rid)["kind"] == "single"


def test_single_page_renders(tmp_path):
    html = pages.single_page(_cfg(tmp_path))
    assert "S_run" in html and "/api/single/report" in html and "__" not in html.split("<script>")[1].replace("__proto__", "")
