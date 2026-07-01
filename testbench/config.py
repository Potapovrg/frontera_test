"""Runtime configuration for the test bench app.

Single dataclass, populated from CLI args in app.py. Mirrors the
config.py convention in frontera_ml (dataclass tree + resolve() for paths).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    http_host: str = "0.0.0.0"
    http_port: int = 8080

    device_port: str = "/dev/ttyACM0"
    mock: bool = False

    results_dir: Path = field(default_factory=lambda: Path("results"))

    # Sweep defaults (tuned for a stable one-shot trace, not a fast waterfall)
    default_start_mhz: float = 100.0
    default_stop_mhz: float = 6000.0
    default_step_mhz: float = 1.0
    samples: int = 3
    timeout_ms: int = 20
    scan_timeout_s: float = 8.0

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
