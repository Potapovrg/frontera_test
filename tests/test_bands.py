"""Band-plan arithmetic and stitching — the error-prone part of multi-band mode.

No hardware and no serial port: band_plan() is pure, and everything else runs
through --mock. Mirrors frontera_ml/tests/test_device_mode.py (pass layout per
mode, "unverified shifter => no scan") and test_dual.py (100-6200, shifted band floored at 6200).

    python3 -m pytest tests/ -q

(If a broken third-party pytest plugin is installed system-wide, prefix with
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1.)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from testbench.config import DEV_MAX_MHZ, Config  # noqa: E402
from testbench.device import DeviceError, DeviceManager, band_plan  # noqa: E402


def cfg(mode: str, **kw) -> Config:
    return Config(device_mode=mode, mock=True, **kw)


def layout(plan):
    """(label, analyzer, dev_start, dev_stop, offset, shifter_high) per pass."""
    return [(p.label, p.analyzer, p.dev_start, p.dev_stop, p.offset_mhz, p.shifter_high)
            for p in plan]


# -- band_plan --------------------------------------------------------------

def test_low_range_is_one_untouched_pass():
    assert layout(band_plan(cfg("1_device_low_range"), 100, 6200)) == [
        ("low", "frontera1", 100, 6200, 0.0, None)
    ]


def test_low_range_rejects_above_6200_instead_of_truncating():
    # Silently clamping to 6200 would return a trace that looks like the
    # requested one but isn't — the operator has to be told to add the shifter.
    with pytest.raises(DeviceError, match="6200"):
        band_plan(cfg("1_device_low_range"), 100, 12200)


def test_full_range_alternates_one_device_around_the_shifter():
    assert layout(band_plan(cfg("1_device_full_range"), 100, 12200)) == [
        ("low", "frontera2", 100, 6200.0, 0.0, False),
        ("high", "frontera2", 200.0, 6200.0, 6000.0, True),
    ]


def test_two_device_splits_across_units_and_never_touches_the_low_shifter():
    assert layout(band_plan(cfg("2_device"), 100, 12200)) == [
        ("low", "frontera1", 100, 6200.0, 0.0, None),
        ("high", "frontera2", 200.0, 6200.0, 6000.0, True),
    ]


@pytest.mark.parametrize("mode", ["1_device_full_range", "2_device"])
def test_device_is_never_commanded_above_its_native_top(mode):
    for start, stop in [(100, 12200), (7000, 12200), (5900, 6300), (100, 6200)]:
        for p in band_plan(cfg(mode), start, stop):
            assert p.dev_stop <= DEV_MAX_MHZ
            assert p.true_stop <= stop + 1e-6


def test_high_only_range_skips_the_low_pass():
    plan = band_plan(cfg("2_device"), 7000, 12200)
    assert [p.label for p in plan] == ["high"]
    assert (plan[0].dev_start, plan[0].dev_stop) == (1000.0, 6200.0)


def test_stop_exactly_at_the_edge_stays_single_band():
    assert [p.label for p in band_plan(cfg("2_device"), 100, 6200)] == ["low"]


def test_range_spanning_the_edge_produces_both_passes():
    plan = band_plan(cfg("2_device"), 6000, 6400)
    assert [(p.label, p.dev_start, p.dev_stop) for p in plan] == [
        ("low", 6000, 6200.0), ("high", 200.0, 400.0)
    ]


def test_high_band_is_floored_at_the_native_ceiling_not_at_6100():
    # frontera_ml scan/dual.py: offset (6000) < native ceiling (6200), so the
    # shifted band must not claim 6100-6200 that the low band already covers.
    plan = band_plan(cfg("2_device"), 6100, 6300)
    assert [(p.label, p.true_start) for p in plan] == [("low", 6100), ("high", 6200.0)]


def test_range_entirely_inside_a_band_gap_is_rejected():
    # Only exists when the offset leaves a real hole: 6200 -> 6600 here.
    with pytest.raises(DeviceError, match="6600"):
        band_plan(cfg("2_device", offset2_mhz=6500.0), 6300, 6500)


def test_offset_is_configurable():
    plan = band_plan(cfg("2_device", offset2_mhz=5000.0), 100, 11000)
    assert plan[1].offset_mhz == 5000.0
    assert plan[1].true_start == 6200.0   # floored at the native ceiling, not 5100


# -- stitching --------------------------------------------------------------

def test_stitched_trace_is_continuous_with_the_standard_offset():
    result = DeviceManager(cfg("2_device")).sweep(100, 12200, 20)

    assert np.all(np.diff(result.freq_mhz) >= 0), "frequency axis must not go backwards"
    assert not np.isnan(result.power_dbm).any(), "no hole at the standard offset"
    assert result.freq_mhz[-1] == pytest.approx(12200.0)
    assert [b["label"] for b in result.bands] == ["low", "high"]


def test_stitched_trace_is_monotonic_with_exactly_one_gap_break():
    result = DeviceManager(cfg("2_device", offset2_mhz=6500.0)).sweep(100, 12700, 20)

    assert np.all(np.diff(result.freq_mhz) > 0), "frequency axis must stay monotonic"
    nan_at = np.flatnonzero(np.isnan(result.power_dbm))
    assert nan_at.size == 1, "one break per band gap, and no NaN anywhere else"
    assert DEV_MAX_MHZ < result.freq_mhz[nan_at[0]] < 6600.0
    assert result.freq_mhz[0] == pytest.approx(100.0)
    assert result.freq_mhz[-1] == pytest.approx(12700.0)
    assert [b["label"] for b in result.bands] == ["low", "high"]


def test_single_band_sweep_has_no_nan():
    result = DeviceManager(cfg("1_device_low_range")).sweep(100, 6200, 20)
    assert not np.isnan(result.power_dbm).any()
    assert result.bands == [
        {"label": "low", "analyzer": "frontera1", "start": 100.0, "stop": 6200.0,
         "offset": 0.0}
    ]


def test_peak_info_ignores_the_gap_nan():
    from testbench import plotting

    result = DeviceManager(cfg("2_device", offset2_mhz=6500.0)).sweep(100, 12700, 20)
    peak_dbm, peak_freq = plotting.peak_info(result.freq_mhz, result.power_dbm)
    assert np.isfinite(peak_dbm) and np.isfinite(peak_freq)
    assert peak_dbm == pytest.approx(np.nanmax(result.power_dbm))


def test_difference_survives_gaps_in_both_traces():
    from testbench import plotting

    dm = DeviceManager(cfg("2_device", offset2_mhz=6500.0))
    a = dm.sweep(100, 12700, 20)
    b = dm.sweep(100, 12700, 20)
    freq, diff = plotting._difference(a.freq_mhz, a.power_dbm, b.freq_mhz, b.power_dbm)
    # Only A's own gap sample may be NaN — B must have been interpolated
    # across its gap rather than poisoning the neighbouring samples.
    assert np.isnan(diff).sum() == 1
    assert freq.size == a.freq_mhz.size


# -- shifter safety ---------------------------------------------------------

def test_unverified_shifter_fails_the_sweep_instead_of_mislabelling_it():
    """A shifter that won't confirm its state means "this band is down".

    Scanning anyway would re-label real 0-6 GHz data as 6-12 GHz — the exact
    failure frontera_ml/scan/dual.py:380 guards against.
    """
    dm = DeviceManager(cfg("2_device"))
    dm._switcher.verify = lambda expected_high: False

    with pytest.raises(DeviceError, match="shifter"):
        dm.sweep(100, 12200, 20)


def test_missing_shifter_fails_the_high_band_but_not_the_low_one():
    dm = DeviceManager(cfg("2_device"))
    dm._switcher = None

    assert dm.sweep(100, 6200, 20).freq_mhz.size > 0
    with pytest.raises(DeviceError, match="shifter"):
        dm.sweep(100, 12200, 20)


def test_low_range_mode_builds_no_switcher_at_all():
    assert DeviceManager(cfg("1_device_low_range"))._switcher is None


def test_switcher_command_rejected_when_the_mode_has_no_shifter():
    dm = DeviceManager(cfg("1_device_low_range"))
    with pytest.raises(DeviceError, match="not used"):
        dm.switcher_command("ping")


def test_unknown_switcher_command_is_rejected():
    dm = DeviceManager(cfg("2_device"))
    with pytest.raises(DeviceError, match="unknown shifter"):
        dm.switcher_command("rm -rf")


# -- runtime mode switching -------------------------------------------------

def test_set_mode_retargets_the_ceiling_and_the_plan():
    dm = DeviceManager(cfg("1_device_low_range"))
    assert dm.status()["max_stop_mhz"] == 6200.0

    dm.set_mode("2_device")
    assert dm.status()["max_stop_mhz"] == 12200.0
    assert dm.status()["switcher_enabled"] is True
    assert dm.sweep(100, 12200, 50).freq_mhz[-1] == pytest.approx(12200.0)

    dm.set_mode("1_device_low_range")
    assert dm.status()["max_stop_mhz"] == 6200.0
    with pytest.raises(DeviceError):
        dm.sweep(100, 12200, 50)


def test_unknown_mode_falls_back_instead_of_crashing():
    c = Config(device_mode="bogus", mock=True)
    assert c.mode() == "1_device_low_range"
    assert c.max_stop_mhz() == 6200.0
