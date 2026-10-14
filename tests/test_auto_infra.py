"""Auto Test plumbing: generator port resolution, mock tone, save_auto, DB columns."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from testbench import hardware  # noqa: E402
from testbench.config import Config  # noqa: E402
from testbench.db import JournalDB  # noqa: E402
from testbench.device import DeviceManager  # noqa: E402
from testbench.storage import load_sweep, save_auto  # noqa: E402


def test_mock_topology_has_generator():
    topo = hardware.resolve_topology(Config(mock=True))
    assert topo["generator"] == {"port": "mock", "source": "mock"}


def _patch_real(monkeypatch, gen):
    seen = {}
    monkeypatch.setattr(hardware, "find_arinst_tg", lambda: gen)

    def fake_sa6(exclude=()):
        seen["sa6"] = list(exclude)
        return "/dev/ttyACM1"

    def fake_sw(explicit, exclude=()):
        seen["sw"] = list(exclude)
        return "/dev/ttyACM2"

    monkeypatch.setattr(hardware, "find_sa6", fake_sa6)
    monkeypatch.setattr(hardware, "resolve_switcher_port", fake_sw)
    # force the probe paths, whatever symlinks exist on this machine
    monkeypatch.setattr(hardware, "SWITCHER_SYMLINK", "/nonexistent/switcher")
    monkeypatch.setattr(hardware, "FRONTERA1_SYMLINK", "/nonexistent/f1")
    monkeypatch.setattr(hardware, "FRONTERA2_SYMLINK", "/nonexistent/f2")
    return seen


def test_generator_probe_is_excluded_from_other_probes(monkeypatch):
    seen = _patch_real(monkeypatch, "/dev/ttyACM9")
    cfg = Config(device_mode="1_device_full_range")
    topo = hardware.resolve_topology(cfg)
    assert topo["generator"] == {"port": "/dev/ttyACM9", "source": "probe"}
    assert "/dev/ttyACM9" in seen["sw"]
    assert "/dev/ttyACM9" in seen["sa6"]


def test_generator_explicit_and_missing(monkeypatch):
    _patch_real(monkeypatch, None)
    topo = hardware.resolve_topology(Config(generator_port="/dev/ttyUSB5"))
    assert topo["generator"] == {"port": "/dev/ttyUSB5", "source": "explicit"}
    topo = hardware.resolve_topology(Config())
    assert topo["generator"] == {"port": None, "source": "none"}
    assert not any("enerator" in w for w in topo["warnings"])


def test_mock_tone_low_band():
    dm = DeviceManager(Config(mock=True))
    dm.mock_tone = (1000.0, -14.0)
    r = dm.sweep(950, 1050, 1)
    assert abs(np.nanmax(r.power_dbm) - (-14.0)) <= 3.0
    assert abs(r.freq_mhz[np.nanargmax(r.power_dbm)] - 1000.0) <= 2.0


def test_mock_tone_high_band_through_shifter():
    dm = DeviceManager(Config(mock=True, device_mode="1_device_full_range"))
    dm.mock_tone = (7000.0, -20.0)
    r = dm.sweep(6950, 7050, 1)
    assert abs(np.nanmax(r.power_dbm) - (-20.0)) <= 3.0
    assert abs(r.freq_mhz[np.nanargmax(r.power_dbm)] - 7000.0) <= 2.0


def test_status_has_generator():
    st = DeviceManager(Config(mock=True)).status()
    gen = st["ports"]["generator"]
    assert gen["open"] is True


def test_save_auto_roundtrip(tmp_path):
    cfg = Config(mock=True, results_dir=tmp_path)
    cfg.ensure_dirs()
    name = save_auto(cfg, "S", [100, 200, 300], [-10, -12, -40], [100, 201, 290], [True, True, False])
    assert name == "S_AUTO.npz"
    f, p = load_sweep(cfg, name)
    assert list(f) == [100, 200, 300] and list(p) == [-10, -12, -40]
    with np.load(cfg.data_dir / name) as d:
        assert d["peak_freq_mhz"].dtype == np.float32
        assert d["passed"].dtype == bool and list(d["passed"]) == [True, True, False]


def test_auto_columns_migrate_and_roundtrip(tmp_path):
    path = str(tmp_path / "j.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE comparisons (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL)")
    con.commit(); con.close()
    db = JournalDB(path)
    rid = db.insert({"ts_utc": "t", "kind": "auto", "gen_level_dbm": -10.0,
                     "window_mhz": 20.0, "tolerance_db": 3.0, "n_pass": 8, "n_fail": 2})
    row = db.get(rid)
    assert (row["kind"], row["gen_level_dbm"], row["window_mhz"], row["tolerance_db"],
            row["n_pass"], row["n_fail"]) == ("auto", -10.0, 20.0, 3.0, 8, 2)
