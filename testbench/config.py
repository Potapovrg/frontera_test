"""Runtime configuration for the test bench app.

Single dataclass, populated from CLI args in app.py. Mirrors the
config.py convention in frontera_ml (dataclass tree + resolve() for paths).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Hardware topologies, named exactly as sdr.device_mode in frontera_ml
# (frontera_ml/config.py:21) so the two projects stay talkable about:
#   1_device_low_range  - one Frontera, 100-6200 MHz, shifter untouched
#   1_device_full_range - one Frontera behind the shifter, alternating passes:
#                         shift off -> low band, shift on -> high band
#   2_device            - frontera1 direct (low), frontera2 behind the shifter
#                         (high); the shifter stays permanently on
DEVICE_MODES = ("1_device_low_range", "1_device_full_range", "2_device")

DEVICE_MODE_LABELS = {
    "1_device_low_range": "1 Frontera — up to 6.2 GHz",
    "1_device_full_range": "1 Frontera + shifter — up to 12.2 GHz",
    "2_device": "2 Frontera + shifter — up to 12.2 GHz",
}

# Native tuning span of the Frontera itself. The shifted band is produced by
# sweeping this same span and re-labelling the result by offset2_mhz, so the
# device is never commanded above DEV_MAX_MHZ (see frontera_ml/scan/dual.py:68).
DEV_MIN_MHZ = 100.0
# 6200 is MEASURED, not spec'd (frontera_ml SDRConfig.HW_MAX_MHZ): 6200.0 MHz is
# the highest live bin, 6201.0 reads a constant -115 dBm "no lock" sentinel.
DEV_MAX_MHZ = 6200.0


@dataclass
class Config:
    http_host: str = "0.0.0.0"
    http_port: int = 8080

    device_port: str = ""       # blank -> /dev/frontera1 symlink, else probe
    mock: bool = False

    # -- hardware topology --------------------------------------------------
    # Default is the historical single-device behaviour; the UI switches this
    # at runtime via DeviceManager.set_mode().
    device_mode: str = "1_device_low_range"
    device_port2: str = ""      # Frontera behind the shifter; blank -> resolve
    switcher_port: str = ""     # blank -> /dev/switcher, else ping-probe
    offset2_mhz: float = 6000.0  # added to the shifted device's measured freqs
    switcher_settle_ms: int = 100  # MAX2870 relock wait after a real toggle

    results_dir: Path = field(default_factory=lambda: Path("results"))

    # Sweep defaults (tuned for a stable one-shot trace, not a fast waterfall)
    default_start_mhz: float = 100.0
    default_stop_mhz: float = 6200.0
    default_step_mhz: float = 1.0
    samples: int = 3
    timeout_ms: int = 20
    scan_timeout_s: float = 8.0

    # -- topology predicates (mirror frontera_ml/config.py:48-72) -----------
    def mode(self) -> str:
        """Validated device_mode; unknown values fall back to the safe default."""
        if self.device_mode in DEVICE_MODES:
            return self.device_mode
        logger.warning("unknown device_mode=%r; falling back to %r (valid: %s)",
                       self.device_mode, DEVICE_MODES[0], ", ".join(DEVICE_MODES))
        return DEVICE_MODES[0]

    def uses_second_device(self) -> bool:
        """True when a second physical Frontera is part of the setup."""
        return self.mode() == "2_device"

    def switcher_enabled(self) -> bool:
        """True when the shifter takes part at all."""
        return self.mode() != "1_device_low_range"

    def switcher_always_high(self) -> bool:
        """True when the shifter must stay on for the whole session."""
        return self.mode() == "2_device"

    def max_stop_mhz(self) -> float:
        """Highest frequency the current topology can actually measure."""
        if self.switcher_enabled():
            return self.offset2_mhz + DEV_MAX_MHZ
        return DEV_MAX_MHZ

    def mode_label(self) -> str:
        return DEVICE_MODE_LABELS.get(self.mode(), self.mode())

    # -- paths --------------------------------------------------------------
    def resolve(self, *parts: str) -> Path:
        return self.results_dir.joinpath(*parts)

    @property
    def db_path(self) -> Path:
        return self.resolve("journal.db")

    @property
    def data_dir(self) -> Path:
        return self.resolve("data")

    @property
    def plots_dir(self) -> Path:
        return self.resolve("plots")

    @property
    def reports_dir(self) -> Path:
        return self.resolve("reports")

    def ensure_dirs(self) -> None:
        for d in (self.results_dir, self.data_dir, self.plots_dir, self.reports_dir):
            d.mkdir(parents=True, exist_ok=True)
