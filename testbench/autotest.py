"""Auto Test: step the Arinst SSA-TG R3 generator across a band and check that
the Frontera reads the expected level at every step.

At each generator frequency f the R3 is retuned (fixed output level), given
``cfg.generator_settle_ms`` to settle, and the Frontera sweeps only a narrow
window centred on f. The maximum in that window is the measured level; the
point passes when it is within ``tolerance`` of ``level_dbm - cable_loss``.

Sweeping a small window instead of the whole band per step is what makes a
1 MHz-step run over 100-6200 MHz (~6100 points) feasible at all — and it is
still tens of minutes, so the run lives on a background thread and the web
layer polls ``status()``.

Safety: the generator is a transmitter on the bench. Whatever happens in the
run (stop, device failures, generator errors, bugs), the thread's ``finally``
turns the RF output off and closes the port before anything else.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import asdict, dataclass, fields

import numpy as np

from . import plotting, storage
from .arinst_tg import GEN_POWER_DBM, MAX_FREQ_HZ, MIN_FREQ_HZ, ArinstError, ArinstTG
from .config import DEV_MAX_MHZ, DEV_MIN_MHZ, Config
from .device import _SERIAL_ERRORS, DeviceError

logger = logging.getLogger(__name__)

# The point budget keeps one run's in-memory arrays and the status payload sane
# (100-12000 MHz at 0.1 MHz is ~119k points — clearly a typo, not a test plan).
MAX_POINTS = 100_000

# A single failed sweep is noise worth recording; this many in a row means the
# analyzer is gone and the rest of the run would only burn time.
MAX_CONSECUTIVE_FAILURES = 5

# Exceptions that mean "the generator itself failed" — abort at once, because
# we can no longer be sure what it is transmitting.
_GENERATOR_ERRORS = (ArinstError,) + _SERIAL_ERRORS


@dataclass
class AutoTestParams:
    start: float
    stop: float
    step: float = 1.0
    level_dbm: float = -14.0
    window: float = 100.0
    analyzer_step: float = 1.0
    cable_loss: float = 0.0
    tolerance: float = 3.0
    conditions: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "AutoTestParams":
        """Build from a JSON/form dict. Blank values count as missing, so an
        emptied optional field in the UI falls back to its default."""
        kwargs: dict = {}
        for f in fields(cls):
            raw = d.get(f.name)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                if f.name in ("start", "stop"):
                    raise ValueError(f"'{f.name}' is required")
                continue
            if f.name == "conditions":
                kwargs[f.name] = str(raw).strip()
                continue
            try:
                kwargs[f.name] = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"'{f.name}' must be a number, got {raw!r}") from None
        return cls(**kwargs)

    def _n_points(self) -> int:
        # The epsilon keeps 100..100.3 step 0.1 at 4 points despite 0.3/0.1
        # evaluating to 2.9999999999999996.
        return int(math.floor((self.stop - self.start) / self.step + 1e-9)) + 1

    def validate(self, cfg: Config) -> None:
        """Raise ValueError with a message fit for the UI."""
        for name in ("start", "stop", "step", "level_dbm", "window",
                     "analyzer_step", "cable_loss", "tolerance"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"'{name}' must be a finite number")

        # Both the analyzer (100 MHz) and the generator (24 MHz) floors apply.
        lo = max(DEV_MIN_MHZ, MIN_FREQ_HZ / 1e6)
        hi = min(cfg.max_stop_mhz(), MAX_FREQ_HZ / 1e6)
        if self.start < lo:
            raise ValueError(f"start must be >= {lo:g} MHz")
        if self.stop > hi:
            raise ValueError(
                f"stop must be <= {hi:g} MHz in mode '{cfg.mode_label()}'")
        if self.start > self.stop:
            raise ValueError("start must be <= stop")
        if self.step <= 0:
            raise ValueError("step must be > 0")
        n = self._n_points()
        if n > MAX_POINTS:
            raise ValueError(
                f"{n} points requested; at most {MAX_POINTS} — increase the step")
        if self.level_dbm not in GEN_POWER_DBM.values():
            allowed = ", ".join(str(v) for v in GEN_POWER_DBM.values())
            raise ValueError(f"generator level must be one of {allowed} dBm")
        if self.analyzer_step <= 0:
            raise ValueError("analyzer step must be > 0")
        if self.window < 2 * self.analyzer_step:
            raise ValueError("window must be at least 2 × analyzer step")
        if self.tolerance < 0:
            raise ValueError("tolerance must be >= 0")

    @property
    def power_index(self) -> int:
        for idx, dbm in GEN_POWER_DBM.items():
            if dbm == self.level_dbm:
                return idx
        raise ValueError(f"no generator power index for {self.level_dbm} dBm")

    @property
    def expected_dbm(self) -> float:
        return self.level_dbm - self.cable_loss

    def frequencies(self) -> np.ndarray:
        """start..stop inclusive. Built by index rather than np.arange so the
        last point is neither dropped nor overshot by accumulated float error;
        rounded to 1 Hz so commanded frequencies are clean integers in Hz."""
        i = np.arange(self._n_points(), dtype=float)
        return np.round(self.start + i * self.step, 6)

    def as_dict(self) -> dict:
        return asdict(self)


def band_gap(cfg: Config) -> tuple | None:
    """The unmeasurable hole between the low and the shifted band, if the
    offset leaves one (never with the standard +6000; see device._stitch)."""
    if not cfg.switcher_enabled():
        return None
    gap_hi = cfg.offset2_mhz + DEV_MIN_MHZ
    return (DEV_MAX_MHZ, gap_hi) if gap_hi > DEV_MAX_MHZ else None


def window_for(cfg: Config, f_mhz: float, width: float) -> tuple | None:
    """Analyzer window of total `width` centred on f, clamped to what the
    current topology can measure. Near a range edge the window is cut short
    on that side rather than shifted, so f always stays at (or near) its
    centre and the peak search never drifts onto unrelated spectrum.

    Returns None when f lies inside a band gap: there is nothing to measure,
    and a window reaching into the neighbouring band would only report the
    maximum of unrelated spectrum there. A window next to a gap is cut at it.
    """
    lo = max(DEV_MIN_MHZ, f_mhz - width / 2.0)
    hi = min(cfg.max_stop_mhz(), f_mhz + width / 2.0)
    gap = band_gap(cfg)
    if gap is not None:
        if gap[0] < f_mhz < gap[1]:
            return None
        if f_mhz <= gap[0]:
            hi = min(hi, gap[0])
        else:
            lo = max(lo, gap[1])
    return lo, hi


class _MockGenerator:
    """Stand-in for ArinstTG under --mock: injects the tone into the mock sweep."""

    def __init__(self, device) -> None:
        self.device = device
        self.on = False
        self.freq_hz: int | None = None
        self.closed = False

    def set_generator(self, freq_hz: int, on: bool, power_index: int = 0) -> None:
        # Same checks as the real driver, so mock runs catch the same mistakes.
        freq_hz = int(freq_hz)
        if not (MIN_FREQ_HZ <= freq_hz <= MAX_FREQ_HZ):
            raise ValueError(f"generator frequency out of {MIN_FREQ_HZ}..{MAX_FREQ_HZ} Hz")
        if power_index not in GEN_POWER_DBM:
            raise ValueError("power_index must be 0..7")
        self.freq_hz = freq_hz
        self.on = bool(on)
        self.device.mock_tone = (freq_hz / 1e6, GEN_POWER_DBM[power_index]) if on else None

    def generator_off(self, freq_hz: int = 1_000_000_000) -> None:
        self.on = False
        self.device.mock_tone = None

    def close(self) -> None:
        self.closed = True


def _r(x, nd: int):
    """Round for JSON; NaN/inf become None (JSON has no NaN)."""
    x = float(x)
    return round(x, nd) if math.isfinite(x) else None


class AutoTestRunner:
    """Runs one auto test at a time on a background thread."""

    def __init__(self, cfg: Config, device, generator_factory=None) -> None:
        self.cfg = cfg
        self.device = device
        self._factory = generator_factory or self._default_generator
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._state = "idle"
        self._error: str | None = None
        self._params: AutoTestParams | None = None
        self._n = 0
        self._points: list = []   # (f, level, peak_f, passed)
        self._t0: float | None = None
        self._started_utc: str | None = None
        self._setup: str | None = None
        self._device_mode: str | None = None
        self._npz: str | None = None
        self._png: str | None = None
        # Bumped per start() so a page can tell a new run from the old one even
        # when the point counts happen to line up.
        self._run_id = 0
        # Generator port as seen at start; status() must not ask the device
        # while a run is going (device.status() waits for the sweep lock).
        self._generator = {"port": None, "source": "none"}

    def _default_generator(self):
        if self.cfg.mock:
            return _MockGenerator(self.device)
        # Not the cached topology port: after a replug that node may now belong
        # to another unit. ArinstTG(None) looks the R3 up by USB name, fresh.
        return ArinstTG(self.cfg.generator_port or None)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._state == "running"

    # -- control ------------------------------------------------------------
    def start(self, params: dict) -> dict:
        p = AutoTestParams.from_dict(params)
        p.validate(self.cfg)
        freqs = p.frequencies()

        # Check-and-reserve atomically so two concurrent start() calls cannot
        # both pass the "not running" check.
        with self._lock:
            if self._state == "running":
                raise RuntimeError("auto test already running")
            self._state = "running"
            self._error = None
            self._params = p
            self._n = int(freqs.size)
            self._points = []
            self._t0 = time.monotonic()
            self._started_utc = storage.utc_now_str()
            self._setup = self.cfg.mode_label()
            self._device_mode = self.cfg.mode()
            self._npz = None
            self._png = None
            self._run_id += 1
            self._stop.clear()

        gen_info = self._device_generator()
        # Open synchronously (outside the lock — it is serial I/O) so a missing
        # R3 is reported by the start request itself, not minutes later.
        try:
            gen = self._factory()
        except Exception as exc:  # noqa: BLE001 — anything here means "no generator"
            logger.error("auto test: could not open the generator: %s", exc)
            with self._lock:
                self._state = "error"
                self._error = f"generator: {exc}"
            raise RuntimeError(f"generator: {exc}") from exc

        port = getattr(gen, "port", None)
        with self._lock:
            self._generator = ({"port": port, "source": gen_info.get("source", "probe")}
                               if port else gen_info)
        logger.info("auto test: %d points %.3f-%.3f MHz step %g, %g dBm, window %g MHz",
                    freqs.size, p.start, p.stop, p.step, p.level_dbm, p.window)
        self._thread = threading.Thread(target=self._run, args=(gen, p, freqs),
                                        name="autotest", daemon=True)
        try:
            self._thread.start()
        except Exception as exc:  # noqa: BLE001 — e.g. "can't start new thread"
            # The worker owns the generator's shutdown; without it, do it here.
            for action in (gen.generator_off, gen.close):
                try:
                    action()
                except Exception:  # noqa: BLE001
                    logger.exception("auto test: generator cleanup failed")
            with self._lock:
                self._state = "error"
                self._error = f"could not start the worker: {exc}"
            raise RuntimeError(f"could not start the worker: {exc}") from exc
        return self.status(0)

    def shutdown(self, timeout: float = 30.0) -> None:
        """Stop a run and wait for its cleanup (generator off) — for server
        exit: the worker is a daemon thread and would otherwise be killed with
        the RF output still on."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                logger.error("auto test did not stop within %.0f s — "
                             "the generator may still be ON", timeout)

    def stop(self) -> dict:
        # The thread notices before its next point; the current one finishes.
        self._stop.set()
        return self.status(0)

    # -- worker -------------------------------------------------------------
    def _run(self, gen, p: AutoTestParams, freqs: np.ndarray) -> None:
        error: str | None = None
        consecutive = 0
        power_index = p.power_index
        expected = p.expected_dbm
        try:
            for f in freqs:
                if self._stop.is_set():
                    break
                f = float(f)
                window = window_for(self.cfg, f, p.window)
                if window is None:
                    # Inside a band gap: nothing to measure, not an analyzer
                    # failure — record it, but don't count it towards an abort.
                    with self._lock:
                        self._points.append((f, math.nan, math.nan, False))
                    continue
                try:
                    gen.set_generator(int(round(f * 1e6)), True, power_index)
                except _GENERATOR_ERRORS as exc:
                    error = f"generator failed at {f:g} MHz: {exc}"
                    logger.error("auto test: %s", error)
                    break
                time.sleep(self.cfg.generator_settle_ms / 1000.0)

                lo, hi = window
                try:
                    sweep = self.device.sweep(lo, hi, p.analyzer_step)
                except (DeviceError,) + _SERIAL_ERRORS as exc:
                    consecutive += 1
                    logger.warning("auto test: measurement at %g MHz failed (%d in a row): %s",
                                   f, consecutive, exc)
                    with self._lock:
                        self._points.append((f, math.nan, math.nan, False))
                    if consecutive >= MAX_CONSECUTIVE_FAILURES:
                        error = (f"{consecutive} consecutive measurement failures; "
                                 f"last: {exc}")
                        break
                    continue

                consecutive = 0
                level, peak_f = plotting.peak_info(sweep.freq_mhz, sweep.power_dbm)
                passed = bool(math.isfinite(level) and abs(level - expected) <= p.tolerance)
                with self._lock:
                    self._points.append((f, level, peak_f, passed))
        except Exception as exc:  # noqa: BLE001 — never leave the state stuck at 'running'
            logger.exception("auto test crashed")
            error = f"unexpected error: {exc}"
        finally:
            # Off and close are independent: a failing off must not keep the
            # port open, and nothing below may run with the RF still on.
            off_error = None
            for attempt in range(2):
                try:
                    gen.generator_off()
                    off_error = None
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.exception("auto test: could not turn the generator off "
                                     "(attempt %d)", attempt + 1)
                    off_error = (f"could not turn the generator off ({exc}) — RF output "
                                 "may still be ON, power-cycle the R3")
            try:
                gen.close()
            except Exception:  # noqa: BLE001
                logger.exception("auto test: could not close the generator")

            save_error = self._save(p)

            # Final state last, so a client that sees 'done' can fetch the PNG.
            # A save failure is reported in `error` but does not turn a good
            # (or stopped) run into an 'error' run, nor mask the run's own error.
            with self._lock:
                msgs = [m for m in (error, off_error, save_error) if m]
                self._error = "; ".join(msgs) if msgs else None
                if error is not None or off_error is not None:
                    self._state = "error"
                elif self._stop.is_set():
                    self._state = "stopped"
                else:
                    self._state = "done"
                state, done = self._state, len(self._points)
            logger.info("auto test finished: %s (%d/%d points)%s", state, done, len(freqs),
                        f" — {'; '.join(msgs)}" if msgs else "")

    def _save(self, p: AutoTestParams) -> str | None:
        """Write npz + png; returns an error message or None. Never raises."""
        arr = self._arrays()
        if arr is None:
            return None
        gen_freq, level, peak_f, passed = arr
        stamp = storage.make_stamp()
        try:
            npz = storage.save_auto(self.cfg, stamp, gen_freq, level, peak_f, passed)
            png = plotting.auto_png(
                self.cfg, stamp, gen_freq, level, passed, p.expected_dbm, p.tolerance,
                title=f"Auto test — {p.conditions or self._started_utc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("auto test: saving results failed")
            return f"saving results failed: {exc}"
        with self._lock:
            self._npz, self._png = npz, png
        return None

    def _arrays(self):
        with self._lock:
            pts = list(self._points)
        if not pts:
            return None
        gen_freq = np.array([x[0] for x in pts], dtype=float)
        level = np.array([x[1] for x in pts], dtype=float)
        peak_f = np.array([x[2] for x in pts], dtype=float)
        passed = np.array([x[3] for x in pts], dtype=bool)
        return gen_freq, level, peak_f, passed

    # -- reporting ----------------------------------------------------------
    def status(self, since: int = 0) -> dict:
        with self._lock:
            running = self._state == "running"
            generator = dict(self._generator)
        if not running:
            # Safe when idle: no sweep holds the device lock for long. Never
            # call it under our own lock.
            generator = self._device_generator()

        with self._lock:
            state = self._state
            p = self._params
            pts = list(self._points)
            n = self._n if state != "idle" else 0
            error = self._error
            png = self._png
            t0 = self._t0
            run_id = self._run_id

        i = len(pts)
        eta = None
        if state == "running" and i > 0 and t0 is not None:
            eta = round((time.monotonic() - t0) / i * max(n - i, 0), 1)

        levels = np.array([x[1] for x in pts], dtype=float)
        fin = levels[np.isfinite(levels)]
        n_pass = sum(1 for x in pts if x[3])
        summary = {
            "n_pass": n_pass,
            "n_fail": i - n_pass,
            "min": _r(fin.min(), 3) if fin.size else None,
            "max": _r(fin.max(), 3) if fin.size else None,
            "mean": _r(fin.mean(), 3) if fin.size else None,
        }

        return {
            "state": state,
            "run_id": run_id,
            "error": error,
            "i": i,
            "n": n,
            "eta_s": eta,
            "expected_dbm": float(p.expected_dbm) if p is not None and state != "idle" else None,
            "tolerance_db": float(p.tolerance) if p is not None and state != "idle" else None,
            "generator": generator,
            "points": [{"f": _r(f, 6), "level": _r(lv, 3), "peak_f": _r(pf, 3), "pass": bool(ok)}
                       for f, lv, pf, ok in pts[max(0, int(since)):]],
            "next": i,
            "summary": summary,
            "png_url": f"/plot/{png}" if png else None,
            "params": p.as_dict() if p is not None else None,
        }

    def result(self) -> dict | None:
        with self._lock:
            state = self._state
            p = self._params
            npz, png = self._npz, self._png
            setup, mode, started = self._setup, self._device_mode, self._started_utc
            error, n_planned = self._error, self._n
        if state not in ("done", "stopped", "error"):
            return None
        arr = self._arrays()
        if arr is None:
            return None
        gen_freq, level, peak_f, passed = arr
        n_pass = int(passed.sum())
        return {
            "params": p,
            "gen_freq": gen_freq,
            "level": level,
            "peak_freq": peak_f,
            "passed": passed,
            "n_pass": n_pass,
            "n_fail": int(passed.size - n_pass),
            "npz": npz,
            "png": png,
            "setup": setup,
            "device_mode": mode,
            "started_utc": started,
            "state": state,
            "error": error,
            "n_planned": n_planned,
        }

    def _device_generator(self) -> dict:
        try:
            g = self.device.status()["ports"].get("generator") or {}
            return {"port": g.get("port"), "source": g.get("source", "none")}
        except Exception:  # noqa: BLE001 — status must always answer
            return {"port": None, "source": "none"}
