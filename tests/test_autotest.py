"""Auto Test runner: params/validation, window clamping, and the background run
(mock end-to-end, stop, device/generator failures) — with the generator always
left off and closed."""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from testbench import autotest  # noqa: E402
from testbench.arinst_tg import ArinstError  # noqa: E402
from testbench.autotest import (  # noqa: E402
    AutoTestParams,
    AutoTestRunner,
    _MockGenerator,
    window_for,
)
from testbench.config import Config  # noqa: E402
from testbench.device import DeviceError, DeviceManager  # noqa: E402


def _cfg(tmp_path, **kw) -> Config:
    c = Config(mock=True, results_dir=tmp_path, **kw)
    c.ensure_dirs()
    return c


def _wait(runner: AutoTestRunner, deadline_s: float = 20.0) -> dict:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        st = runner.status()
        if st["state"] != "running":
            return st
        time.sleep(0.02)
    raise AssertionError("auto test did not finish in time")


class _Gen(_MockGenerator):
    """Mock generator that records every off/close and can fail on demand."""

    def __init__(self, device, fail_on_call: int | None = None) -> None:
        super().__init__(device)
        self.calls = 0
        self.off_calls = 0
        self.fail_on_call = fail_on_call

    def set_generator(self, freq_hz, on, power_index=0):
        self.calls += 1
        if self.fail_on_call is not None and self.calls == self.fail_on_call:
            raise ArinstError("device reported error for command 0xC3")
        super().set_generator(freq_hz, on, power_index)

    def generator_off(self, freq_hz=1_000_000_000):
        self.off_calls += 1
        super().generator_off(freq_hz)


def _runner(cfg, device, **gen_kw):
    holder = {}

    def factory():
        holder["gen"] = _Gen(device, **gen_kw)
        return holder["gen"]

    return AutoTestRunner(cfg, device, generator_factory=factory), holder


class _FailingDevice(DeviceManager):
    """Mock device whose sweep fails on the listed call numbers (or always)."""

    def __init__(self, cfg, fail_calls=None) -> None:
        super().__init__(cfg)
        self.fail_calls = fail_calls  # None -> always fail
        self.sweeps = 0

    def sweep(self, start_mhz, stop_mhz, step_mhz):
        self.sweeps += 1
        if self.fail_calls is None or self.sweeps in self.fail_calls:
            raise DeviceError("frontera1: scan failed after reconnect and retry")
        return super().sweep(start_mhz, stop_mhz, step_mhz)


# -- pure parts ------------------------------------------------------------
def test_window_for_clamps_to_the_topology_range(tmp_path):
    low = _cfg(tmp_path)
    assert window_for(low, 120, 100) == (100, 170)
    assert window_for(low, 1000, 100) == (950, 1050)
    assert window_for(low, 6180, 100) == (6130, 6200)
    full = _cfg(tmp_path, device_mode="1_device_full_range")
    assert window_for(full, 12150, 100) == (12100, 12200)


def test_validate_rejects_bad_params(tmp_path):
    cfg = _cfg(tmp_path)
    base = {"start": 1000, "stop": 1010}
    AutoTestParams.from_dict(base).validate(cfg)  # defaults are fine

    bad = [
        {"level_dbm": -15},
        {"start": 50},
        {"stop": 7000},                          # above 6200 in low_range
        {"step": 0},
        {"window": 1.5, "analyzer_step": 1},     # < 2 × analyzer step
        {"start": 1020},                         # start > stop
        {"tolerance": -1},
        {"step": 1e-5},                          # too many points
        {"cable_loss": float("nan")},
    ]
    for extra in bad:
        with pytest.raises(ValueError):
            AutoTestParams.from_dict({**base, **extra}).validate(cfg)

    with pytest.raises(ValueError) as e:
        AutoTestParams.from_dict({**base, "level_dbm": -15}).validate(cfg)
    assert "-14" in str(e.value) and "-29" in str(e.value)

    for d in ({"start": "abc", "stop": 1010}, {"stop": 1010}, {"start": 1000},
              {"start": 1000, "stop": 1010, "step": [1]}):
        with pytest.raises(ValueError):
            AutoTestParams.from_dict(d)


def test_params_defaults_frequencies_and_dict(tmp_path):
    p = AutoTestParams.from_dict({"start": "1000", "stop": 1010, "step": 5, "window": ""})
    assert p.window == 100.0 and p.level_dbm == -14.0 and p.power_index == 2
    assert p.frequencies().tolist() == [1000, 1005, 1010]
    assert p.expected_dbm == -14.0
    assert list(p.as_dict()) == ["start", "stop", "step", "level_dbm", "window",
                                 "analyzer_step", "cable_loss", "tolerance", "conditions"]

    f = AutoTestParams(start=100, stop=100.3, step=0.1).frequencies()
    assert f.size == 4 and f[-1] == pytest.approx(100.3)
    assert AutoTestParams(start=500, stop=500).frequencies().tolist() == [500]

    full = _cfg(tmp_path, device_mode="1_device_full_range")
    AutoTestParams(start=11000, stop=12000).validate(full)
    with pytest.raises(ValueError):
        AutoTestParams(start=11000, stop=12100).validate(full)  # generator max


def test_mock_generator_validates_and_drives_the_tone(tmp_path):
    dev = DeviceManager(_cfg(tmp_path))
    g = _MockGenerator(dev)
    g.set_generator(1_000_000_000, True, 2)
    assert dev.mock_tone == (1000.0, -14) and g.on
    with pytest.raises(ValueError):
        g.set_generator(10_000_000, True, 0)
    with pytest.raises(ValueError):
        g.set_generator(1_000_000_000, True, 8)
    g.generator_off()
    assert dev.mock_tone is None and not g.on
    g.close()
    assert g.closed


# -- runs ------------------------------------------------------------------
def test_mock_end_to_end(tmp_path):
    cfg = _cfg(tmp_path)
    dev = DeviceManager(cfg)
    runner, holder = _runner(cfg, dev)

    st = runner.start({"start": 1000, "stop": 1010, "step": 5, "window": 20,
                       "conditions": "bench A"})
    assert st["state"] == "running" and st["n"] == 3
    st = _wait(runner)

    assert st["state"] == "done", st["error"]
    assert st["error"] is None
    assert st["i"] == st["n"] == st["next"] == 3
    assert [p["f"] for p in st["points"]] == [1000, 1005, 1010]
    assert all(abs(p["level"] + 14) < 4 for p in st["points"])
    assert all(abs(p["peak_f"] - p["f"]) < 1.5 for p in st["points"])
    assert st["summary"]["n_pass"] >= 2
    assert st["summary"]["n_pass"] + st["summary"]["n_fail"] == 3
    assert st["expected_dbm"] == -14.0 and st["tolerance_db"] == 3.0
    assert st["eta_s"] is None
    assert st["generator"] == {"port": "mock", "source": "mock"}
    assert st["params"]["conditions"] == "bench A"
    json.dumps(st, allow_nan=False)

    gen = holder["gen"]
    assert not gen.on and gen.closed and gen.off_calls >= 1
    assert dev.mock_tone is None

    res = runner.result()
    assert res is not None
    assert (cfg.data_dir / res["npz"]).is_file()
    assert (cfg.plots_dir / res["png"]).is_file()
    assert st["png_url"] == f"/plot/{res['png']}"
    for k in ("gen_freq", "level", "peak_freq", "passed"):
        assert len(res[k]) == 3
    assert res["n_pass"] + res["n_fail"] == 3
    assert res["device_mode"] == "1_device_low_range"
    assert res["setup"] == cfg.mode_label() and res["started_utc"]
    assert isinstance(res["params"], AutoTestParams)

    tail = runner.status(since=2)
    assert len(tail["points"]) == 1 and tail["next"] == 3


def test_idle_status_and_result(tmp_path):
    cfg = _cfg(tmp_path)
    runner = AutoTestRunner(cfg, DeviceManager(cfg))
    st = runner.status()
    assert st["state"] == "idle" and st["n"] == 0 and st["points"] == []
    assert st["expected_dbm"] is None and st["tolerance_db"] is None
    assert st["params"] is None and st["png_url"] is None
    assert st["summary"]["min"] is None
    json.dumps(st, allow_nan=False)
    assert runner.result() is None and not runner.running


def test_stop_ends_the_run_early(tmp_path):
    cfg = _cfg(tmp_path)
    dev = DeviceManager(cfg)
    runner, holder = _runner(cfg, dev)
    runner.start({"start": 1000, "stop": 2000, "step": 1, "window": 20})

    end = time.monotonic() + 20
    while runner.status()["next"] < 1:
        assert time.monotonic() < end, "no point recorded"
        time.sleep(0.02)
    assert runner.running
    assert runner.status()["eta_s"] is not None
    runner.stop()
    st = _wait(runner)

    assert st["state"] == "stopped"
    assert 0 < st["i"] < st["n"] == 1001
    assert not holder["gen"].on and holder["gen"].closed
    assert dev.mock_tone is None
    assert runner.result() is not None


def test_already_running_and_validation_errors(tmp_path):
    cfg = _cfg(tmp_path)
    dev = DeviceManager(cfg)
    runner, _ = _runner(cfg, dev)

    with pytest.raises(ValueError):
        runner.start({"start": 1000, "stop": 1010, "level_dbm": -15})
    assert not runner.running and runner.status()["state"] == "idle"

    runner.start({"start": 1000, "stop": 2000, "step": 1, "window": 20})
    try:
        with pytest.raises(RuntimeError, match="already running"):
            runner.start({"start": 1000, "stop": 1010})
    finally:
        runner.stop()
        _wait(runner)
    assert not runner.running


def test_persistent_device_errors_abort_after_five(tmp_path):
    cfg = _cfg(tmp_path)
    dev = _FailingDevice(cfg)
    runner, holder = _runner(cfg, dev)
    runner.start({"start": 1000, "stop": 1100, "step": 1, "window": 20})
    st = _wait(runner)

    assert st["state"] == "error"
    assert "5 consecutive measurement failures" in st["error"]
    assert st["i"] == 5
    assert all(p["level"] is None and p["peak_f"] is None and not p["pass"]
               for p in st["points"])
    assert st["summary"] == {"n_pass": 0, "n_fail": 5, "min": None, "max": None,
                             "mean": None}
    json.dumps(st, allow_nan=False)
    assert not holder["gen"].on and holder["gen"].closed
    res = runner.result()
    assert np.all(np.isnan(res["level"])) and not res["passed"].any()


def test_single_device_error_is_recorded_and_skipped(tmp_path):
    cfg = _cfg(tmp_path)
    dev = _FailingDevice(cfg, fail_calls={2})
    runner, holder = _runner(cfg, dev)
    runner.start({"start": 1000, "stop": 1010, "step": 5, "window": 20})
    st = _wait(runner)

    assert st["state"] == "done" and st["error"] is None
    assert st["i"] == 3
    bad = st["points"][1]
    assert bad["level"] is None and bad["pass"] is False
    assert all(abs(st["points"][k]["level"] + 14) < 4 for k in (0, 2))
    json.dumps(st, allow_nan=False)
    res = runner.result()
    assert math.isnan(res["level"][1]) and math.isnan(res["peak_freq"][1])
    assert not res["passed"][1]
    assert holder["gen"].closed and dev.mock_tone is None


def test_generator_error_aborts_and_still_turns_off(tmp_path):
    cfg = _cfg(tmp_path)
    dev = DeviceManager(cfg)
    runner, holder = _runner(cfg, dev, fail_on_call=2)
    runner.start({"start": 1000, "stop": 1010, "step": 5, "window": 20})
    st = _wait(runner)

    assert st["state"] == "error"
    assert "generator" in st["error"]
    assert st["i"] == 1
    gen = holder["gen"]
    assert gen.off_calls >= 1 and gen.closed and not gen.on
    assert dev.mock_tone is None


def test_close_happens_even_if_off_fails(tmp_path):
    cfg = _cfg(tmp_path)
    dev = DeviceManager(cfg)

    class _BadOff(_Gen):
        def generator_off(self, freq_hz=1_000_000_000):
            self.off_calls += 1
            raise ArinstError("timeout: got 0 of 4 bytes")

    gens = []

    def factory():
        gens.append(_BadOff(dev))
        return gens[-1]

    runner = AutoTestRunner(cfg, dev, generator_factory=factory)
    runner.start({"start": 1000, "stop": 1000, "window": 20})
    st = _wait(runner)
    # A failed off is retried once, then surfaced — never a silent 'done'.
    assert st["state"] == "error" and st["i"] == 1
    assert "may still be ON" in st["error"]
    assert gens[0].off_calls == 2 and gens[0].closed


def test_generator_open_failure_is_reported_by_start(tmp_path):
    cfg = _cfg(tmp_path)

    def factory():
        raise ArinstError("Arinst SSA TG R3 not found; pass the port explicitly")

    runner = AutoTestRunner(cfg, DeviceManager(cfg), generator_factory=factory)
    with pytest.raises(RuntimeError, match="generator: Arinst SSA TG R3 not found"):
        runner.start({"start": 1000, "stop": 1010})
    assert not runner.running
    st = runner.status()
    assert st["state"] == "error" and st["error"].startswith("generator:")
    assert runner.result() is None
    json.dumps(st, allow_nan=False)


def test_cable_loss_shifts_the_expected_level(tmp_path):
    cfg = _cfg(tmp_path)
    dev = DeviceManager(cfg)
    runner, _ = _runner(cfg, dev)
    runner.start({"start": 1000, "stop": 1010, "step": 5, "window": 20,
                  "level_dbm": -14, "cable_loss": 10})
    st = _wait(runner)
    assert st["state"] == "done"
    assert st["expected_dbm"] == -24.0
    assert st["summary"]["n_pass"] == 0 and st["summary"]["n_fail"] == 3


def test_default_factory_uses_mock_generator_under_mock(tmp_path):
    cfg = _cfg(tmp_path)
    dev = DeviceManager(cfg)
    runner = AutoTestRunner(cfg, dev)
    runner.start({"start": 1000, "stop": 1000, "window": 20})
    st = _wait(runner)
    assert st["state"] == "done" and st["i"] == 1
    assert abs(st["points"][0]["level"] + 14) < 4
    assert dev.mock_tone is None


def test_window_respects_a_band_gap():
    # offset 6300 opens a 6200-6400 hole: points in it have no window, and
    # windows next to it are cut at its edges instead of reaching across.
    cfg = Config(mock=True, device_mode="1_device_full_range", offset2_mhz=6300.0)
    assert autotest.band_gap(cfg) == (6200.0, 6400.0)
    assert autotest.window_for(cfg, 6300, 100) is None
    assert autotest.window_for(cfg, 6180, 100) == (6130, 6200)
    assert autotest.window_for(cfg, 6420, 100) == (6400, 6470)
    assert autotest.band_gap(Config(mock=True, device_mode="1_device_full_range")) is None


def test_gap_points_do_not_abort_the_run(tmp_path):
    cfg = Config(mock=True, results_dir=tmp_path, device_mode="1_device_full_range",
                 offset2_mhz=6300.0)
    cfg.ensure_dirs()
    runner = AutoTestRunner(cfg, DeviceManager(cfg))
    runner.start({"start": 6190, "stop": 6410, "step": 20, "window": 20})
    st = _wait(runner)
    assert st["state"] == "done", st["error"]
    gap = [pt for pt in st["points"] if 6200 < pt["f"] < 6400]
    assert gap and all(pt["level"] is None for pt in gap)
    assert all(pt["level"] is not None for pt in st["points"] if pt not in gap)


def test_result_carries_run_state_and_plan(tmp_path):
    cfg = _cfg(tmp_path)
    runner = AutoTestRunner(cfg, DeviceManager(cfg))
    first = runner.start({"start": 1000, "stop": 1010, "step": 5, "window": 20})["run_id"]
    _wait(runner)
    res = runner.result()
    assert res["state"] == "done" and res["n_planned"] == 3 and res["error"] is None
    assert runner.start({"start": 1000, "stop": 1000, "window": 20})["run_id"] == first + 1
    _wait(runner)
