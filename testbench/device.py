"""Device access layer — serializes all sweeps behind one lock so the two
comparison blocks (A/B) can never collide on the serial port.

Mirrors the reconnect-on-failure pattern in FronteraSweepThread
(frontera_interface.py), but as a simple synchronous call rather than a
background thread, since the web handler already runs each sweep on its own
request thread.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import numpy as np

from frontera_interface import FronteraInterface, find_sa6

logger = logging.getLogger(__name__)


@dataclass
class SweepResult:
    freq_mhz: np.ndarray
    power_dbm: np.ndarray


class DeviceError(RuntimeError):
    """Raised when a sweep cannot be completed (device unreachable/timeout)."""


class DeviceManager:
    """Owns one FronteraInterface connection (or a mock) and serializes sweeps."""

    def __init__(
        self,
        port: str = "/dev/ttyACM0",
        mock: bool = False,
        samples: int = 3,
        timeout_ms: int = 20,
        scan_timeout_s: float = 8.0,
    ) -> None:
        self.port = port
        self.mock = mock
        self.samples = samples
        self.timeout_ms = timeout_ms
        self.scan_timeout_s = scan_timeout_s
        self._lock = threading.Lock()
        self._dev: FronteraInterface | None = None

        if not mock:
            self._connect_or_raise()

    def _connect_or_raise(self) -> None:
        dev = FronteraInterface()
        if dev.connect(self.port):
            self._dev = dev
            logger.info("connected to Frontera on %s", self.port)
            return

        # The device can re-enumerate under a different node after a USB
        # disconnect (e.g. /dev/ttyACM0 -> /dev/ttyACM1), so fall back to
        # probing before giving up.
        logger.warning("could not open %s, probing for the device...", self.port)
        found = find_sa6()
        if found and dev.connect(found):
            self.port = found
            self._dev = dev
            logger.info("connected to Frontera on %s (auto-detected)", self.port)
            return

        raise DeviceError(f"could not open serial port {self.port} (auto-detect also failed)")

    def sweep(self, start_mhz: float, stop_mhz: float, step_mhz: float) -> SweepResult:
        """Run one sweep. Thread-safe; retries once by reconnecting on failure."""
        if self.mock:
            return self._mock_sweep(start_mhz, stop_mhz, step_mhz)

        with self._lock:
            result = self._scan(start_mhz, stop_mhz, step_mhz)
            if result is None:
                logger.warning("sweep failed, reconnecting and retrying once")
                self._reconnect()
                result = self._scan(start_mhz, stop_mhz, step_mhz)
            if result is None:
                raise DeviceError(
                    f"scan {start_mhz}-{stop_mhz} MHz failed after reconnect+retry"
                )
            return SweepResult(freq_mhz=result.freq_mhz, power_dbm=result.power_dbm)

    def _scan(self, start_mhz: float, stop_mhz: float, step_mhz: float):
        assert self._dev is not None
        try:
            return self._dev.scan_chunked_mhz(
                start_mhz,
                stop_mhz,
                step_mhz=step_mhz,
                samples=self.samples,
                timeout_ms=self.timeout_ms,
                timeout=self.scan_timeout_s,
            )
        except OSError as exc:
            # Raw serial I/O errors (device unplugged/reset mid-scan, e.g.
            # termios "Input/output error") surface here instead of the
            # graceful None that frontera_interface returns on a plain
            # timeout. Treat them the same way so sweep()'s reconnect+retry
            # kicks in, rather than letting the exception escape all the way
            # up to the HTTP handler and kill the connection with no response.
            logger.error("scan I/O error: %s", exc)
            return None

    def _reconnect(self) -> None:
        if self._dev is not None:
            try:
                self._dev.disconnect()
            except OSError:
                pass
        time.sleep(1.0)
        self._connect_or_raise()

    def _mock_sweep(self, start_mhz: float, stop_mhz: float, step_mhz: float) -> SweepResult:
        """Synthesize a plausible spectrum for UI/PDF testing without hardware."""
        time.sleep(0.3)  # emulate acquisition latency
        freq = np.arange(start_mhz, stop_mhz + step_mhz / 2, step_mhz, dtype=np.float32)
        noise_floor = -108.0
        power = noise_floor + np.random.normal(0.0, 1.5, size=freq.size).astype(np.float32)

        rng = np.random.default_rng()
        for _ in range(rng.integers(2, 5)):
            center = rng.uniform(start_mhz, stop_mhz)
            width = rng.uniform(5.0, 40.0)
            peak_db = rng.uniform(15.0, 35.0)
            power += (peak_db * np.exp(-0.5 * ((freq - center) / max(width, 1e-3)) ** 2)).astype(
                np.float32
            )

        return SweepResult(freq_mhz=freq, power_dbm=power)

    def close(self) -> None:
        if self._dev is not None:
            self._dev.disconnect()
