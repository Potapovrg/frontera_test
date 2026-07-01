"""Binary sweep storage — raw dBm arrays are the source of truth, saved as
.npz (freq_mhz + power_dbm), following the frontera_ml convention of keeping
raw numeric data on disk independent of any rendered image.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

import numpy as np

from .config import Config
from .device import SweepResult


def make_stamp() -> str:
    """Filesystem-safe timestamp, used as a filename prefix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")


def utc_now_str() -> str:
    """Human-readable UTC timestamp for display/DB (not filename-safe)."""
    return datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M:%S")


def save_sweep(cfg: Config, block: str, stamp: str, sweep: SweepResult) -> str:
    """Save a sweep's raw arrays as .npz under cfg.data_dir. Returns the filename."""
    name = f"{stamp}_{block}.npz"
    np.savez(
        cfg.data_dir / name,
        freq_mhz=sweep.freq_mhz.astype(np.float32),
        power_dbm=sweep.power_dbm.astype(np.float32),
    )
    return name


def load_sweep(cfg: Config, filename: str) -> Tuple[np.ndarray, np.ndarray]:
    """Load a previously saved .npz sweep. Raises FileNotFoundError if missing."""
    fp = safe_path(cfg.data_dir, filename)
    with np.load(fp) as data:
        return data["freq_mhz"], data["power_dbm"]


def safe_path(root: Path, filename: str) -> Path:
    """Resolve filename under root, refusing path traversal (mirrors log_server.py)."""
    name = Path(filename).name
    fp = (root / name).resolve()
    root_resolved = root.resolve()
    if fp.parent != root_resolved:
        raise FileNotFoundError(filename)
    return fp


def remove_if_exists(root: Path, filename: str) -> None:
    """Delete filename under root if present; ignores path traversal / missing file."""
    try:
        fp = safe_path(root, filename)
    except FileNotFoundError:
        return
    try:
        fp.unlink()
    except FileNotFoundError:
        pass
