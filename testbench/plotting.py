"""Spectrum PNG rendering and comparison PDF report generation.

Uses matplotlib's Agg backend (headless, no display needed) for PNGs and the
PdfPages backend for the report — both default to a white figure background,
which satisfies the "PDF background should be white" requirement with no
extra dependency (reportlab/fpdf are not installed on the OrangePi and it has
no internet access to fetch them).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Tuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
import numpy as np  # noqa: E402

from .config import Config

logger = logging.getLogger(__name__)


def peak_info(freq: np.ndarray, power: np.ndarray) -> Tuple[float, float]:
    """Return (peak_dbm, peak_freq_mhz).

    NaN-aware: a stitched multi-band trace carries one NaN power sample per
    band gap (see device._stitch), which plain argmax would happily return.
    """
    if not np.any(np.isfinite(power)):
        return float("nan"), float("nan")
    i = int(np.nanargmax(power))
    return float(power[i]), float(freq[i])


def _draw_spectrum(ax, freq: np.ndarray, power: np.ndarray, label: str, color: str) -> None:
    ax.plot(freq, power, color=color, linewidth=0.8, label=label)
    # nanmin, not min: one NaN in a stitched trace would otherwise make the
    # fill baseline NaN and silently drop the fill entirely.
    ax.fill_between(freq, power, np.nanmin(power) - 2, color=color, alpha=0.15)
    ax.set_xlabel("Frequency (MHz)")
    ax.set_ylabel("Power (dBm)")
    ax.grid(True, linewidth=0.4, alpha=0.5)


def _mark_peak(ax, freq: np.ndarray, power: np.ndarray) -> None:
    """Mark the highest-amplitude point with a dot and a "f MHz, X dBm" label."""
    peak_dbm, peak_freq = peak_info(freq, power)
    if not np.isfinite(peak_dbm):
        return
    ax.plot([peak_freq], [peak_dbm], marker="o", color="#da3633", markersize=6, zorder=5)
    ax.annotate(f"{peak_freq:.1f} MHz, {peak_dbm:.1f} dBm", (peak_freq, peak_dbm),
                xytext=(0, 10), textcoords="offset points", ha="center", fontsize=9,
                color="#da3633", weight="bold", zorder=5,
                bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="#da3633", lw=0.6))
    lo, hi = ax.get_ylim()
    ax.set_ylim(lo, hi + 0.08 * (hi - lo))   # headroom so the label is not clipped


def spectrum_png(cfg: Config, stamp: str, block: str, freq: np.ndarray, power: np.ndarray,
                  title: str, mark_peak: bool = False) -> str:
    """Render one spectrum trace to PNG under cfg.plots_dir. Returns the filename."""
    fig, ax = plt.subplots(figsize=(10, 4.5), dpi=150, facecolor="white")
    _draw_spectrum(ax, freq, power, label=block, color="#1f6feb")
    if mark_peak:
        _mark_peak(ax, freq, power)
    ax.set_title(title, fontsize=11)
    fig.tight_layout()

    name = f"{stamp}_{block}.png"
    fig.savefig(cfg.plots_dir / name, facecolor="white")
    plt.close(fig)
    return name


def comparison_pdf(
    cfg: Config,
    stamp: str,
    freq_a: np.ndarray, power_a: np.ndarray,
    freq_b: np.ndarray, power_b: np.ndarray,
    meta: dict,
) -> str:
    """Build the multi-page comparison PDF: title, A, B, overlay, difference (B-A)."""
    name = f"{stamp}_comparison.pdf"
    path = cfg.reports_dir / name

    with PdfPages(path) as pdf:
        _add_title_page(pdf, meta)

        fig, ax = plt.subplots(figsize=(10, 4.5), dpi=150, facecolor="white")
        _draw_spectrum(ax, freq_a, power_a, label="Block A", color="#1f6feb")
        ax.set_title("Block A — before")
        ax.legend(loc="upper right")
        fig.tight_layout()
        pdf.savefig(fig, facecolor="white")
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(10, 4.5), dpi=150, facecolor="white")
        _draw_spectrum(ax, freq_b, power_b, label="Block B", color="#da3633")
        ax.set_title("Block B — after")
        ax.legend(loc="upper right")
        fig.tight_layout()
        pdf.savefig(fig, facecolor="white")
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(10, 4.5), dpi=150, facecolor="white")
        ax.plot(freq_a, power_a, color="#1f6feb", linewidth=0.8, label="Block A")
        ax.plot(freq_b, power_b, color="#da3633", linewidth=0.8, label="Block B")
        ax.set_xlabel("Frequency (MHz)")
        ax.set_ylabel("Power (dBm)")
        ax.grid(True, linewidth=0.4, alpha=0.5)
        ax.set_title("Overlay — A vs B")
        ax.legend(loc="upper right")
        fig.tight_layout()
        pdf.savefig(fig, facecolor="white")
        plt.close(fig)

        diff_freq, diff_db = _difference(freq_a, power_a, freq_b, power_b)
        fig, ax = plt.subplots(figsize=(10, 4.5), dpi=150, facecolor="white")
        ax.plot(diff_freq, diff_db, color="#8250df", linewidth=0.8)
        ax.axhline(0.0, color="#57606a", linewidth=0.6, linestyle="--")
        ax.set_xlabel("Frequency (MHz)")
        ax.set_ylabel("Difference B - A (dB)")
        ax.grid(True, linewidth=0.4, alpha=0.5)
        ax.set_title("Difference (Block B - Block A)")
        fig.tight_layout()
        pdf.savefig(fig, facecolor="white")
        plt.close(fig)

    return name


def single_pdf(cfg: Config, stamp: str, freq: np.ndarray, power: np.ndarray, meta: dict) -> str:
    """Two-page report for one device: title page, then the spectrum with its peak marked."""
    name = f"{stamp}_single.pdf"
    with PdfPages(cfg.reports_dir / name) as pdf:
        fig = plt.figure(figsize=(10, 6.5), dpi=150, facecolor="white")
        fig.text(0.5, 0.92, "Frontera Single Test Report", fontsize=16,
                 ha="center", weight="bold")
        fig.text(0.08, 0.82, f"Generated: {meta['ts_utc']}", fontsize=10)
        fig.text(0.08, 0.72, "Device parameters", fontsize=12, weight="bold", color="#1f6feb")
        fig.text(0.08, 0.66,
                 f"Start: {meta['start_a']:.1f} MHz   Stop: {meta['stop_a']:.1f} MHz   "
                 f"Step: {meta['step_a']:.3f} MHz", fontsize=10)
        fig.text(0.08, 0.625, f"Bench setup: {meta.get('setup_a', '-')}", fontsize=9,
                 color="#57606a")
        fig.text(0.08, 0.59,
                 f"Peak: {meta['peak_dbm_a']:.1f} dBm @ {meta['peak_freq_a']:.1f} MHz",
                 fontsize=10, weight="bold", color="#da3633")
        fig.text(0.08, 0.55, "Test conditions:", fontsize=10, weight="bold")
        fig.text(0.08, 0.50, _wrap(meta.get("conditions_a", "")), fontsize=9, va="top")
        pdf.savefig(fig, facecolor="white")
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(10, 4.5), dpi=150, facecolor="white")
        _draw_spectrum(ax, freq, power, label="Device", color="#1f6feb")
        _mark_peak(ax, freq, power)
        ax.set_title("Spectrum")
        fig.tight_layout()
        pdf.savefig(fig, facecolor="white")
        plt.close(fig)
    return name


def _difference(
    freq_a: np.ndarray, power_a: np.ndarray, freq_b: np.ndarray, power_b: np.ndarray
) -> Tuple[np.ndarray, np.ndarray]:
    """Interpolate B onto A's frequency grid and return (freq, B - A) over the overlap.

    np.interp has no notion of missing data, so B's band-gap NaNs are dropped
    from the interpolation input first. A's own NaNs are left in place — they
    break the difference line at the gap, which is where it belongs.
    """
    lo = max(freq_a.min(), freq_b.min())
    hi = min(freq_a.max(), freq_b.max())
    mask = (freq_a >= lo) & (freq_a <= hi)
    freq = freq_a[mask]
    finite_b = np.isfinite(power_b)
    power_b_on_a = np.interp(freq, freq_b[finite_b], power_b[finite_b])
    return freq, power_b_on_a - power_a[mask]


def _add_title_page(pdf: PdfPages, meta: dict) -> None:
    fig = plt.figure(figsize=(10, 6.5), dpi=150, facecolor="white")
    fig.text(0.5, 0.92, "Frontera Comparison Test Report", fontsize=16,
              ha="center", weight="bold")
    fig.text(0.08, 0.82, f"Generated: {meta['ts_utc']}", fontsize=10)

    fig.text(0.08, 0.72, "Block A parameters", fontsize=12, weight="bold", color="#1f6feb")
    fig.text(
        0.08, 0.66,
        f"Start: {meta['start_a']:.1f} MHz   Stop: {meta['stop_a']:.1f} MHz   "
        f"Step: {meta['step_a']:.3f} MHz",
        fontsize=10,
    )
    fig.text(0.08, 0.625, f"Bench setup: {meta.get('setup_a', '-')}", fontsize=9,
             color="#57606a")
    fig.text(0.08, 0.60, "Test conditions:", fontsize=10, weight="bold")
    fig.text(0.08, 0.54, _wrap(meta.get("conditions_a", "")), fontsize=9, va="top")

    fig.text(0.08, 0.42, "Block B parameters", fontsize=12, weight="bold", color="#da3633")
    fig.text(
        0.08, 0.36,
        f"Start: {meta['start_b']:.1f} MHz   Stop: {meta['stop_b']:.1f} MHz   "
        f"Step: {meta['step_b']:.3f} MHz",
        fontsize=10,
    )
    fig.text(0.08, 0.325, f"Bench setup: {meta.get('setup_b', '-')}", fontsize=9,
             color="#57606a")
    fig.text(0.08, 0.30, "Test conditions:", fontsize=10, weight="bold")
    fig.text(0.08, 0.24, _wrap(meta.get("conditions_b", "")), fontsize=9, va="top")

    pdf.savefig(fig, facecolor="white")
    plt.close(fig)


def _wrap(text: str, width: int = 95) -> str:
    import textwrap
    return "\n".join(textwrap.wrap(text, width=width)) or "(none)"


# ---------------------------------------------------------------- Auto Test

_GREEN = "#1a7f37"


def _draw_auto_level(ax, gen_freq, level, passed, expected_dbm: float, tol_db: float) -> None:
    """Received level vs generator frequency with the tolerance band."""
    gen_freq = np.asarray(gen_freq, float)
    level = np.asarray(level, float)
    passed = np.asarray(passed, bool)
    lo_b, hi_b = expected_dbm - tol_db, expected_dbm + tol_db

    ax.axhspan(lo_b, hi_b, color="#3fb950", alpha=0.18, label=f"Tolerance ±{tol_db:g} dB")
    ax.axhline(expected_dbm, color="#57606a", linewidth=0.9, linestyle="--",
               label=f"Expected {expected_dbm:.1f} dBm")
    ax.plot(gen_freq, level, color="#1f6feb", linewidth=1.0, marker="o", markersize=3,
            label="Measured level")

    finite = np.isfinite(level)
    bad = finite & ~passed
    if np.any(bad):
        ax.plot(gen_freq[bad], level[bad], linestyle="none", marker="o", markersize=6,
                color="#da3633", zorder=5, label="Out of tolerance")

    # y-limits: always include the band (and all finite data), with a margin.
    vals = [lo_b, hi_b] + (list(level[finite]) if np.any(finite) else [])
    lo, hi = min(vals), max(vals)
    pad = 0.08 * (hi - lo) if hi > lo else 1.0
    lo_y, hi_y = lo - pad, hi + pad
    if np.any(~finite):
        lo_y -= 0.06 * (hi_y - lo_y)   # room for the failure markers
    ax.set_ylim(lo_y, hi_y)

    nan = ~np.isfinite(level)
    if np.any(nan):
        y = lo_y + 0.03 * (hi_y - lo_y)
        ax.plot(gen_freq[nan], np.full(int(nan.sum()), y), linestyle="none", marker="x",
                markersize=7, color="#da3633", zorder=5, clip_on=False,
                label="No measurement")

    ax.set_xlabel("Generator frequency (MHz)")
    ax.set_ylabel("Received level (dBm)")
    ax.grid(True, linewidth=0.4, alpha=0.5)
    ax.legend(loc="upper right", fontsize=8)


def auto_png(cfg: Config, stamp: str, gen_freq, level, passed,
             expected_dbm: float, tol_db: float, title: str) -> str:
    """Render the Auto Test level plot to `{stamp}_AUTO.png` in cfg.plots_dir."""
    fig, ax = plt.subplots(figsize=(10, 4.5), dpi=150, facecolor="white")
    _draw_auto_level(ax, gen_freq, level, passed, expected_dbm, tol_db)
    ax.set_title(title, fontsize=11)
    fig.tight_layout()
    name = f"{stamp}_AUTO.png"
    fig.savefig(cfg.plots_dir / name, facecolor="white")
    plt.close(fig)
    return name


def auto_pdf(cfg: Config, stamp: str, gen_freq, level, peak_freq, passed, meta: dict) -> str:
    """Three-page Auto Test report: summary, level plot, frequency-error plot."""
    gen_freq = np.asarray(gen_freq, float)
    level = np.asarray(level, float)
    peak_freq = np.asarray(peak_freq, float)
    passed = np.asarray(passed, bool)
    name = f"{stamp}_auto.pdf"
    n_pass, n_fail = int(meta["n_pass"]), int(meta["n_fail"])
    total = n_pass + n_fail
    planned = int(meta.get("n_planned") or total)
    # A run that was stopped or aborted proves nothing about the rest of the
    # band: never call it PASS, and say where it ended.
    complete = meta.get("run_state", "done") == "done" and total >= planned
    ok = complete and n_fail == 0 and n_pass > 0

    with PdfPages(cfg.reports_dir / name) as pdf:
        fig = plt.figure(figsize=(10, 6.5), dpi=150, facecolor="white")
        fig.text(0.5, 0.94, "Frontera Auto Test Report", fontsize=16, ha="center", weight="bold")
        fig.text(0.08, 0.88, f"Generated: {meta['ts_utc']}", fontsize=10)

        fig.text(0.08, 0.82, "Generator (Arinst SSA-TG R3)", fontsize=12, weight="bold",
                 color="#1f6feb")
        fig.text(0.08, 0.775,
                 f"Start: {meta['start_a']:.1f} MHz   Stop: {meta['stop_a']:.1f} MHz   "
                 f"Step: {meta['step_a']:.3f} MHz   Level: {meta['gen_level_dbm']:.1f} dBm",
                 fontsize=10)

        fig.text(0.08, 0.71, "Measurement", fontsize=12, weight="bold", color="#1f6feb")
        fig.text(0.08, 0.665,
                 f"Window: {meta['window_mhz']:.2f} MHz   "
                 f"Analyzer step: {meta['analyzer_step_mhz']:.3f} MHz   "
                 f"Cable loss: {meta['cable_loss_db']:.1f} dB", fontsize=10)
        fig.text(0.08, 0.627,
                 f"Expected level: {meta['expected_dbm']:.1f} dBm ± {meta['tolerance_db']:.1f} dB",
                 fontsize=10)
        fig.text(0.08, 0.592, f"Bench setup: {meta.get('setup_a', '-')}", fontsize=9,
                 color="#57606a")

        verdict = "PASS" if ok else ("FAIL" if complete or n_fail else "INCOMPLETE")
        color = _GREEN if ok else ("#da3633" if verdict == "FAIL" else "#9a6700")
        fig.text(0.08, 0.52, verdict, fontsize=28 if verdict != "INCOMPLETE" else 22,
                 weight="bold", color=color, va="center")
        fig.text(0.36 if verdict == "INCOMPLETE" else 0.25, 0.52,
                 f"{n_pass} / {total} points within tolerance", fontsize=12, va="center")
        if not complete:
            last = f" at {gen_freq[-1]:.1f} MHz" if gen_freq.size else ""
            why = meta.get("run_error") or meta.get("run_state", "stopped")
            fig.text(0.08, 0.485,
                     f"Run {meta.get('run_state', 'stopped')}{last}: {total} of {planned} "
                     f"planned points measured ({_wrap(str(why), 70)})",
                     fontsize=9, color="#9a6700", va="top")

        fin = np.isfinite(level)
        if np.any(fin):
            dev = level - float(meta["expected_dbm"])
            i = int(np.nanargmax(np.where(fin, np.abs(dev), -np.inf)))
            stats = (f"Level min / max / mean: {np.nanmin(level):.1f} / {np.nanmax(level):.1f} / "
                     f"{np.nanmean(level):.1f} dBm   Worst deviation: {dev[i]:+.2f} dB "
                     f"@ {gen_freq[i]:.1f} MHz")
        else:
            stats = "Level statistics: no valid points"
        fig.text(0.08, 0.45, stats, fontsize=9.5)

        fig.text(0.08, 0.39, "Test conditions:", fontsize=10, weight="bold")
        fig.text(0.08, 0.35, _wrap(meta.get("conditions_a", "")), fontsize=9, va="top")
        pdf.savefig(fig, facecolor="white")
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(10, 4.5), dpi=150, facecolor="white")
        _draw_auto_level(ax, gen_freq, level, passed, float(meta["expected_dbm"]),
                         float(meta["tolerance_db"]))
        ax.set_title("Received level vs generator frequency")
        fig.tight_layout()
        pdf.savefig(fig, facecolor="white")
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(10, 4.5), dpi=150, facecolor="white")
        ax.plot(gen_freq, peak_freq - gen_freq, color="#8250df", linewidth=1.0,
                marker="o", markersize=3)
        ax.axhline(0.0, color="#57606a", linewidth=0.6, linestyle="--")
        ax.set_xlabel("Generator frequency (MHz)")
        ax.set_ylabel("Frequency error (MHz)")
        ax.grid(True, linewidth=0.4, alpha=0.5)
        ax.set_title("Peak frequency error (measured − generator)")
        fig.tight_layout()
        pdf.savefig(fig, facecolor="white")
        plt.close(fig)
    return name
