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
