"""Device access layer — turns a requested frequency range into one or more
hardware passes, runs them behind a single lock, and stitches the result.

Three topologies are supported (``Config.device_mode``, named as in
frontera_ml):

* ``1_device_low_range``  — one Frontera, 100-6200 MHz, shifter untouched.
* ``1_device_full_range`` — one Frontera behind the shifter, two passes:
  ``shift off`` for the low band, ``shift on`` for the high band.
* ``2_device``            — frontera1 sweeps the low band directly, frontera2
  sits behind the shifter and sweeps the high band; the shifter stays on.

The device is **never** commanded above 6200 MHz. The high band is an ordinary
native sweep whose measured frequencies are re-labelled by ``offset2_mhz``
(the MAX2870 down-converts 6000-12200 -> 0-6200). As in frontera_ml
(scan/dual.py ``pass_native_range``) the high band is floored at the native
ceiling (6200), not at ``DEV_MIN_MHZ + offset2_mhz`` (6100), so it cannot
overlap the low band; the one shared 6200 bin is tolerated. If the offset is
changed so that a real hole opens between the bands, ``_stitch`` marks it.

Everything still serialises behind one lock so the two comparison blocks (A/B)
can never collide on a serial port. The reconnect-on-failure pattern mirrors
FronteraSweepThread (frontera_interface.py), but as a simple synchronous call:
the web handler already runs each sweep on its own request thread.
"""
from __future__ import annotations

import logging
import termios
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from frontera_interface import FronteraInterface, find_sa6

from . import hardware
from .config import (
    DEV_MAX_MHZ,
    DEV_MIN_MHZ,
    DEVICE_MODE_LABELS,
    DEVICE_MODES,
    Config,
)
from .switcher import ManagedSwitcher, Switcher

logger = logging.getLogger(__name__)

# Low-level failures from an unplugged/replugged serial device surface as
# either OSError (e.g. serial.SerialException, which subclasses it) or a
# bare termios.error ("Input/output error") — termios.error is its own
# exception class and does NOT subclass OSError, so both must be listed
# explicitly or termios-raised errors slip past an `except OSError`.
_SERIAL_ERRORS = (OSError, termios.error)

# Switcher() also raises a bare RuntimeError when its VID:PID auto-detect
# finds nothing.
_OPEN_ERRORS = _SERIAL_ERRORS + (RuntimeError,)

# Which analyzers each mode needs, and which one carries which band.
_ANALYZERS_BY_MODE = {
    "1_device_low_range": ("frontera1",),
    "1_device_full_range": ("frontera2",),
    "2_device": ("frontera1", "frontera2"),
}


@dataclass
class SweepResult:
    freq_mhz: np.ndarray
    power_dbm: np.ndarray
    # One entry per hardware pass, for the journal/report: what was actually
    # measured and how it was re-labelled.
    bands: list = field(default_factory=list)


@dataclass
class BandPass:
    """One hardware pass: what to command, and how to label what comes back."""
    label: str            # "low" | "high"
    analyzer: str         # "frontera1" | "frontera2"
    dev_start: float      # commanded to the device (always <= DEV_MAX_MHZ)
    dev_stop: float
    offset_mhz: float     # added to the measured frequencies
    shifter_high: bool | None  # None = do not touch the shifter

    @property
    def true_start(self) -> float:
        return self.dev_start + self.offset_mhz

    @property
    def true_stop(self) -> float:
        return self.dev_stop + self.offset_mhz

    def as_dict(self) -> dict:
        return {"label": self.label, "analyzer": self.analyzer,
                "start": self.true_start, "stop": self.true_stop,
                "offset": self.offset_mhz}


class DeviceError(RuntimeError):
    """Raised when a sweep cannot be completed (device unreachable/timeout)."""


def band_plan(cfg: Config, start_mhz: float, stop_mhz: float) -> list:
    """Split a *true* frequency range into hardware passes.

    Pure function — no hardware, no locking — so the band arithmetic can be
    tested on its own. Raises ``DeviceError`` if the range cannot be measured
    by the current topology at all.
    """
    mode = cfg.mode()

    if mode == "1_device_low_range":
        if stop_mhz > DEV_MAX_MHZ:
            raise DeviceError(
                f"mode '{cfg.mode_label()}' only measures up to {DEV_MAX_MHZ:.0f} MHz; "
                f"requested {stop_mhz:.0f} MHz — enable the frequency shifter"
            )
        return [BandPass("low", "frontera1", start_mhz, stop_mhz, 0.0, None)]

    offset = cfg.offset2_mhz
    # In 1_device_full_range the single unit sits behind the shifter, so its
    # low band needs an explicit passthrough. In 2_device the low unit is in
    # front of the shifter and must not touch it (frontera_ml/scan/dual.py:377).
    single = mode == "1_device_full_range"
    low_analyzer = "frontera2" if single else "frontera1"
    low_shifter = False if single else None

    passes: list = []
    if start_mhz < DEV_MAX_MHZ:
        passes.append(BandPass("low", low_analyzer, start_mhz,
                               min(stop_mhz, DEV_MAX_MHZ), 0.0, low_shifter))

    # Floored at the native ceiling so the shifted band never overlaps the low one.
    high_floor = max(offset + DEV_MIN_MHZ, DEV_MAX_MHZ)
    if stop_mhz > high_floor:
        passes.append(BandPass(
            "high", "frontera2",
            max(start_mhz, high_floor) - offset,
            min(stop_mhz, offset + DEV_MAX_MHZ) - offset,
            offset, True,
        ))

    if not passes:
        raise DeviceError(
            f"range {start_mhz:.0f}-{stop_mhz:.0f} MHz falls entirely inside the "
            f"{DEV_MAX_MHZ:.0f}-{high_floor:.0f} MHz gap between the bands "
            f"(the shifter moves its input down by {offset:.0f} MHz, so the high "
            f"band starts at {high_floor:.0f} MHz)"
        )
    return passes


class _Analyzer:
    """One physical Frontera: owns its connection and its own reconnect."""

    def __init__(self, name: str, port: str, samples: int, timeout_ms: int,
                 scan_timeout_s: float, exclude=()) -> None:
        self.name = name
        self.port = port
        self.samples = samples
        self.timeout_ms = timeout_ms
        self.scan_timeout_s = scan_timeout_s
        self.exclude = tuple(exclude)
        self._dev: FronteraInterface | None = None

    def connect(self) -> None:
        """Open the port, falling back to a probe. Raises DeviceError."""
        dev = FronteraInterface()
        if self._try_connect(dev, self.port):
            self._dev = dev
            logger.info("%s: connected on %s", self.name, self.port)
            return

        # The device can re-enumerate under a different node after a USB
        # disconnect (e.g. /dev/ttyACM0 -> /dev/ttyACM1), so fall back to
        # probing before giving up. `exclude` keeps the probe off the shifter
        # and off the other Frontera.
        logger.warning("%s: could not open %s, probing for the device...",
                       self.name, self.port)
        found = find_sa6(exclude=self.exclude)
        if found and self._try_connect(dev, found):
            self.port = found
            self._dev = dev
            logger.info("%s: connected on %s (auto-detected)", self.name, self.port)
            return

        raise DeviceError(f"could not open {self.port} (auto-detect did not help either)")

    def _try_connect(self, dev: FronteraInterface, port: str) -> bool:
        try:
            return dev.connect(port)
        except _SERIAL_ERRORS as exc:
            logger.warning("%s: connect to %s failed: %s", self.name, port, exc)
            return False

    def scan(self, start_mhz: float, stop_mhz: float, step_mhz: float):
        """Run one sweep in device space. Retries once by reconnecting."""
        result = self._scan(start_mhz, stop_mhz, step_mhz)
        if result is None:
            logger.warning("%s: sweep failed, reconnecting and retrying once", self.name)
            self._reconnect()
            result = self._scan(start_mhz, stop_mhz, step_mhz)
        if result is None:
            raise DeviceError(
                f"{self.name}: scan {start_mhz:.0f}-{stop_mhz:.0f} MHz failed "
                "after reconnect and retry"
            )
        return result

    def _scan(self, start_mhz: float, stop_mhz: float, step_mhz: float):
        if self._dev is None:
            return None
        try:
            return self._dev.scan_chunked_mhz(
                start_mhz,
                stop_mhz,
                step_mhz=step_mhz,
                samples=self.samples,
                timeout_ms=self.timeout_ms,
                timeout=self.scan_timeout_s,
            )
        except _SERIAL_ERRORS as exc:
            # Raw serial I/O errors (device unplugged/reset mid-scan, e.g.
            # termios "Input/output error") surface here instead of the
            # graceful None that frontera_interface returns on a plain
            # timeout. Treat them the same way so scan()'s reconnect+retry
            # kicks in, rather than letting the exception escape all the way
            # up to the HTTP handler and kill the connection with no response.
            logger.error("%s: scan I/O error: %s", self.name, exc)
            return None

    def _reconnect(self) -> None:
        self.close()
        time.sleep(1.0)
        self.connect()

    def close(self) -> None:
        if self._dev is not None:
            try:
                self._dev.disconnect()
            except _SERIAL_ERRORS:
                pass
            self._dev = None


class _MockSwitcher:
    """Stand-in for ManagedSwitcher under --mock: remembers the state, no I/O."""

    def __init__(self) -> None:
        self.state: bool | None = None

    def set_high(self, high: bool) -> bool:
        self.state = high
        return True

    def verify(self, expected_high: bool) -> bool:
        self.state = expected_high
        return True

    def healthy(self) -> bool:
        return True

    def close(self) -> None:
        pass


class DeviceManager:
    """Owns the analyzers and the shifter for one topology, serializes sweeps."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._lock = threading.RLock()
        self._analyzers: dict = {}
        self._switcher = None
        self._topology: dict = {}
        self._unit_errors: dict = {}  # per-unit reason, for pass-level messages
        self.error: str | None = None
        self._open()

    # -- lifecycle ----------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        """Switch topology at runtime. Never raises — check ``.error`` after."""
        with self._lock:
            self.cfg.device_mode = mode
            self._open()

    def _open(self) -> None:
        """(Re)resolve ports and open everything the current mode needs."""
        with self._lock:
            self._close_locked()
            self._topology = hardware.resolve_topology(self.cfg)
            self._unit_errors = {}
            # Failures worth surfacing on top of resolve_topology's warnings.
            # A unit that simply wasn't discovered is already covered there, so
            # only *open* failures get added here — otherwise the panel says
            # the same thing twice in two different wordings.
            failures: list = []

            if self.cfg.mock:
                self._switcher = _MockSwitcher() if self.cfg.switcher_enabled() else None
                self.error = None
                logger.info("mock topology: %s", self.cfg.mode_label())
                return

            ports = {k: (self._topology.get(k) or {}).get("port")
                     for k in ("frontera1", "frontera2", "switcher")}

            if self.cfg.switcher_enabled():
                if not ports["switcher"]:
                    self._unit_errors["switcher"] = "not found on the USB bus"
                else:
                    self._switcher = ManagedSwitcher(
                        ports["switcher"],
                        settle_s=self.cfg.switcher_settle_ms / 1000.0,
                        exclude=[p for k, p in ports.items() if k != "switcher" and p],
                    )
                    if self.cfg.switcher_always_high() and not self._switcher.set_high(True):
                        self._unit_errors["switcher"] = "did not confirm switch-on at startup"
                        failures.append("shifter did not confirm switch-on at startup")

            exclude_all = [p for p in ports.values() if p]
            for key in _ANALYZERS_BY_MODE[self.cfg.mode()]:
                port = ports.get(key)
                if not port:
                    self._unit_errors[key] = "not found on the USB bus"
                    continue
                analyzer = _Analyzer(
                    key, port, self.cfg.samples, self.cfg.timeout_ms,
                    self.cfg.scan_timeout_s,
                    exclude=[p for p in exclude_all if p != port],
                )
                try:
                    analyzer.connect()
                except DeviceError as exc:
                    self._unit_errors[key] = str(exc)
                    failures.append(f"{key}: {exc}")
                    continue
                self._analyzers[key] = analyzer
                # connect() may have re-probed onto a different node.
                self._topology[key]["port"] = analyzer.port

            problems = list(self._topology.get("warnings", [])) + failures
            self.error = "; ".join(problems) if problems else None
            if self.error:
                logger.error("topology %s incomplete: %s", self.cfg.mode(), self.error)

    def _close_locked(self) -> None:
        for analyzer in self._analyzers.values():
            analyzer.close()
        self._analyzers = {}
        if self._switcher is not None:
            self._switcher.close()
            self._switcher = None

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    # -- status -------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            topo = {k: dict(v) for k, v in self._topology.items()
                    if isinstance(v, dict)}
            for key in ("frontera1", "frontera2", "switcher"):
                topo.setdefault(key, {"port": None, "source": "disabled"})
                if self.cfg.mock:
                    topo[key]["open"] = topo[key].get("port") is not None
                elif key == "switcher":
                    topo[key]["open"] = self._switcher is not None
                else:
                    topo[key]["open"] = key in self._analyzers
            return {
                "mode": self.cfg.mode(),
                "mode_label": self.cfg.mode_label(),
                "modes": [{"value": m, "label": DEVICE_MODE_LABELS.get(m, m)}
                          for m in DEVICE_MODES],
                "mock": self.cfg.mock,
                "max_stop_mhz": self.cfg.max_stop_mhz(),
                "offset_mhz": self.cfg.offset2_mhz,
                "band_gap": ([DEV_MAX_MHZ, self.cfg.offset2_mhz + DEV_MIN_MHZ]
                             if self.cfg.offset2_mhz + DEV_MIN_MHZ > DEV_MAX_MHZ else None),
                "switcher_enabled": self.cfg.switcher_enabled(),
                "error": self.error,
                "warnings": self._topology.get("warnings", []),
                "ports": topo,
            }

    # -- sweeping -----------------------------------------------------------
    def sweep(self, start_mhz: float, stop_mhz: float, step_mhz: float) -> SweepResult:
        """Run every pass the topology needs and return one stitched trace."""
        plan = band_plan(self.cfg, start_mhz, stop_mhz)
        with self._lock:
            segments = [self._run_pass(p, step_mhz) for p in plan]
        return _stitch(segments)

    def _run_pass(self, p: BandPass, step_mhz: float) -> tuple:
        if p.shifter_high is not None:
            if self._switcher is None:
                raise DeviceError(
                    f"band {p.true_start:.0f}-{p.true_stop:.0f} MHz needs the "
                    "shifter, but it is unavailable: "
                    f"{self._unit_errors.get('switcher', 'not found')}"
                )
            # verify() rather than set_high(): set_high short-circuits on its
            # cached state and would not notice the STM32 being power-cycled
            # between sweeps. An unconfirmed shifter must mean "this band is
            # down", never "scan anyway" — otherwise real 0-6 GHz data gets
            # re-labelled as fabricated 6-12 GHz (frontera_ml/scan/dual.py:380).
            if not self._switcher.verify(p.shifter_high):
                want = "switch-on" if p.shifter_high else "passthrough"
                raise DeviceError(
                    f"shifter did not confirm {want} — band "
                    f"{p.true_start:.0f}-{p.true_stop:.0f} MHz was not scanned "
                    "(the data would be labelled with the wrong frequency)"
                )

        if self.cfg.mock:
            freq, power = _mock_sweep(p.dev_start, p.dev_stop, step_mhz)
        else:
            analyzer = self._analyzers.get(p.analyzer)
            if analyzer is None:
                raise DeviceError(
                    f"{p.analyzer} unavailable: "
                    f"{self._unit_errors.get(p.analyzer, 'not connected')}"
                )
            result = analyzer.scan(p.dev_start, p.dev_stop, step_mhz)
            freq, power = result.freq_mhz, result.power_dbm

        return p, np.asarray(freq, dtype=np.float32) + np.float32(p.offset_mhz), \
            np.asarray(power, dtype=np.float32)

    # -- shifter diagnostics panel -----------------------------------------
    def switcher_command(self, action: str) -> str:
        """Run one raw shifter command for the diagnostics panel.

        Closes the ManagedSwitcher first (it reopens lazily on the next sweep)
        so only one handle is on the tty at a time — and so its cached state is
        dropped, which is exactly right after a manual toggle or reset.
        """
        methods = {"ping": "ping", "status": "status", "shift_on": "shift_on",
                   "shift_off": "shift_off", "reset": "reset"}
        if action not in methods:
            raise DeviceError(f"unknown shifter command: {action}")
        if not self.cfg.switcher_enabled():
            raise DeviceError(
                f"the shifter is not used in mode '{self.cfg.mode_label()}'"
            )

        with self._lock:
            if self.cfg.mock:
                return _mock_switcher_reply(action, self._switcher)

            port = (self._topology.get("switcher") or {}).get("port")
            if not port:
                raise DeviceError("shifter not found")
            if self._switcher is not None:
                self._switcher.close()
            try:
                sw = Switcher(port=port)
            except _OPEN_ERRORS as exc:
                raise DeviceError(f"could not open {port}: {exc}") from exc
            try:
                return getattr(sw, methods[action])()
            except _SERIAL_ERRORS as exc:
                raise DeviceError(f"command {action} failed: {exc}") from exc
            finally:
                sw.close()


def _stitch(segments: list) -> SweepResult:
    """Concatenate passes in true-frequency order, breaking the line at gaps.

    If a real, unmeasurable hole separates two passes (only possible when the
    offset leaves one — with the standard +6000 offset the high band is floored
    at 6200 and abuts the low band), one sample of NaN *power* at the midpoint
    keeps the frequency axis monotonic and finite while making matplotlib break
    the trace there. A distance of about one step is not a hole.
    """
    segments = sorted(segments, key=lambda s: float(s[1][0]) if s[1].size else 0.0)
    freqs: list = []
    powers: list = []
    bands: list = []
    for p, freq, power in segments:
        if freqs and freq.size and freqs[-1].size:
            gap_lo, gap_hi = float(freqs[-1][-1]), float(freq[0])
            step = float(freq[1] - freq[0]) if freq.size > 1 else 0.0
            if gap_hi - gap_lo > 1.5 * step:
                freqs.append(np.array([(gap_lo + gap_hi) / 2.0], dtype=np.float32))
                powers.append(np.array([np.nan], dtype=np.float32))
        freqs.append(freq)
        powers.append(power)
        bands.append(p.as_dict())

    return SweepResult(
        freq_mhz=np.concatenate(freqs),
        power_dbm=np.concatenate(powers),
        bands=bands,
    )


def _mock_sweep(start_mhz: float, stop_mhz: float, step_mhz: float) -> tuple:
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

    return freq, power


def _mock_switcher_reply(action: str, sw) -> str:
    state = getattr(sw, "state", None)
    if action == "ping":
        return "ping 0xAC1B3C55 v0 (mock)"
    if action == "reset":
        if sw is not None:
            sw.state = None
        return "reset"
    if action == "shift_on":
        if sw is not None:
            sw.state = True
        return "shifter on"
    if action == "shift_off":
        if sw is not None:
            sw.state = False
        return "shifter off"
    if state is None:
        return "shifter off"
    return "shifter on" if state else "shifter off"
