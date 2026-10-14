"""Auto Test plotting: level PNG and 3-page PDF."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from testbench import plotting  # noqa: E402
from testbench.config import Config  # noqa: E402


def _cfg(tmp_path) -> Config:
    c = Config(mock=True, results_dir=tmp_path)
    c.ensure_dirs()
    return c


def _meta(level, passed):
    return dict(ts_utc="2026-01-01 00:00:00", start_a=1000.0, stop_a=1020.0, step_a=1.0,
                gen_level_dbm=-14.0, window_mhz=2.0, analyzer_step_mhz=0.1,
                cable_loss_db=0.5, tolerance_db=3.0, expected_dbm=-14.5,
                n_pass=int(np.sum(passed)), n_fail=int(len(passed) - np.sum(passed)),
                setup_a="Arinst -> Frontera", conditions_a="room temp, " * 20)


def _run(tmp_path, gen, level, peak):
    cfg = _cfg(tmp_path)
    passed = np.isfinite(level) & (np.abs(level + 14.5) <= 3.0)
    png = plotting.auto_png(cfg, "S", gen, level, passed, -14.5, 3.0, "t")
    pdf = plotting.auto_pdf(cfg, "S", gen, level, peak, passed, _meta(level, passed))
    assert (cfg.plots_dir / png).stat().st_size > 0
    assert (cfg.reports_dir / pdf).stat().st_size > 0


def test_mixed(tmp_path):
    gen = np.arange(1000, 1021, 1.0)
    level = -14.5 + 0.3 * np.sin(gen)
    level[5] = -20.0
    level[9] = np.nan
    peak = gen + 0.05
    peak[9] = np.nan
    _run(tmp_path, gen, level, peak)


def test_all_nan(tmp_path):
    gen = np.arange(1000, 1005, 1.0)
    nan = np.full(5, np.nan)
    _run(tmp_path, gen, nan, nan)


def test_single_point(tmp_path):
    _run(tmp_path, np.array([1000.0]), np.array([-14.5]), np.array([1000.0]))
