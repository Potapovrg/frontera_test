#!/usr/bin/env python3
"""switcher.py — Frequency shifter control via STM32 USB-UART

Vendored verbatim from frontera_ml/frontera_ml/sdr/switcher.py (same convention
as frontera_interface.py). Keep edits upstream so the two copies stay in sync.

Protocol (CR+LF terminated):
    ping       → ping 0xAC1B3C55 v<version>
    status     → shifter on | shifter off
    shift on   → shifter on   (включено смещение / frequency mixing enabled)
    shift off  → shifter off  (включен транзит / passthrough enabled)
    reset      → reset        (сброс схемы MAX2870, выход выключен)

Usage:
    python3 switcher.py ping
    python3 switcher.py status
    python3 switcher.py shift on
    python3 switcher.py shift off
    python3 switcher.py reset
    python3 switcher.py --port /dev/ttyACM1 ping

Auto-detects STM32 Virtual COM Port (0483:5740) if no port specified.
"""

import logging
import os
import sys
import threading
import time
import serial
import serial.tools.list_ports

logger = logging.getLogger(__name__)


# ─── device discovery ────────────────────────────────────────────────────────

VID = 0x0483
PID = 0x5740

# Ping reply magic — used to positively identify the switcher when VID:PID
# alone is ambiguous (both Frontera units also report 0483:5740).
MAGIC = "0xAC1B3C55"

DEFAULT_SYMLINK = "/dev/switcher"


def find_device():
    """Find the STM32 Virtual COM Port by VID:PID. Returns first match or None."""
    for port in serial.tools.list_ports.comports():
        if port.vid == VID and port.pid == PID:
            return port.device
    return None


def resolve_switcher_port(explicit: str = "", exclude=()) -> "str | None":
    """Locate the switcher's serial port, positively identifying it.

    Both Frontera units present the same USB VID:PID (and serial) as the
    switcher's STM32 Virtual COM Port, so plain VID:PID auto-detect
    (``find_device``) can hand back a Frontera. Resolution order:

    1. ``explicit`` if given (e.g. ``sdr.switcher_port`` from config).
    2. ``/dev/switcher`` (udev symlink), if that node exists.
    3. Enumerate every 0483:5740 port, drop any that resolve (via realpath)
       to something in ``exclude`` (the known Frontera device paths), and
       ``ping`` each survivor — accept only a reply containing ``MAGIC``.

    Returns ``None`` if nothing verifies.
    """
    if explicit:
        return explicit
    if os.path.exists(DEFAULT_SYMLINK):
        return DEFAULT_SYMLINK

    excluded_real = {os.path.realpath(e) for e in exclude if e}
    candidates = [p.device for p in serial.tools.list_ports.comports()
                  if p.vid == VID and p.pid == PID
                  and os.path.realpath(p.device) not in excluded_real]

    for port in candidates:
        try:
            with serial.Serial(port, BAUD, timeout=TIMEOUT) as ser:
                ser.write(b"ping" + LINE_TERM)
                ser.flush()
                time.sleep(0.15)
                resp = ser.read_until(LINE_TERM).decode(errors="replace")
            if MAGIC in resp:
                return port
        except (serial.SerialException, OSError) as exc:
            logger.debug("resolve_switcher_port: %s - %s", port, exc)
    return None


# ─── protocol ────────────────────────────────────────────────────────────────

LINE_TERM = b"\r\n"
TIMEOUT = 0.5
BAUD = 115200  # USB CDC ACM — baud rate is ignored, any value works


class Switcher:
    """Frequency shifter serial client."""

    def __init__(self, port=None):
        if port is None:
            port = find_device()
            if port is None:
                raise RuntimeError(
                    f"Device {VID:04X}:{PID:04X} not found. "
                    "Connect the STM32 USB-UART adapter and retry, "
                    "or specify --port /dev/ttyXXX."
                )
        self.port = port
        self.ser = serial.Serial(port, BAUD, timeout=TIMEOUT)

    def _command(self, cmd):
        """Send command, read response, strip terminators."""
        self.ser.write(cmd.encode() + LINE_TERM)
        self.ser.flush()
        time.sleep(0.15)
        resp = self.ser.read_until(LINE_TERM)
        return resp.decode(errors="replace").rstrip("\r\n")

    def ping(self):
        """Check device presence and firmware version."""
        return self._command("ping")

    def status(self):
        """Query shifter state: 'shifter on' or 'shifter off'."""
        return self._command("status")

    def shift_on(self):
        """Enable frequency mixing (shifter on)."""
        return self._command("shift on")

    def shift_off(self):
        """Enable passthrough (shifter off)."""
        return self._command("shift off")

    def reset(self):
        """Reset MAX2870 circuit, output disabled."""
        return self._command("reset")

    def close(self):
        self.ser.close()


# ─── managed wrapper (used by the detection pipeline) ────────────────────────

class ManagedSwitcher:
    """Thread-safe, idempotent, tolerant-of-absence wrapper around ``Switcher``.

    Unlike the bare ``Switcher`` (raises if the device is missing, sends a
    command unconditionally every call), this is meant to live inside the
    detector's control loop: it opens lazily, skips redundant commands, backs
    off on failure instead of hammering a dead port, and reports success only
    when the shifter's own reply confirms the requested state — an unverified
    toggle must never be treated as "scan anyway" (the caller should treat a
    ``False`` return as "this band is down", not "assume it worked").
    """

    def __init__(self, port: str = "", *, settle_s: float = 0.1,
                retry_s: float = 5.0, exclude=()):
        self._explicit_or_resolved_port = port
        self._exclude = tuple(exclude)
        self._settle_s = float(settle_s)
        self._retry_s = float(retry_s)
        self._lock = threading.Lock()
        self._sw = None            # type: Optional[Switcher]
        self._state = None         # type: Optional[bool]  None = unknown
        self._next_attempt = 0.0
        self._warned_absent = False

    def _ensure_open(self) -> bool:
        if self._sw is not None:
            return True
        now = time.time()
        if now < self._next_attempt:
            return False
        port = resolve_switcher_port(self._explicit_or_resolved_port, self._exclude)
        if port is None:
            if not self._warned_absent:
                logger.warning("switcher: no device found (checked %s + VID:PID "
                               "%04X:%04X); will keep retrying every %.0fs",
                               DEFAULT_SYMLINK, VID, PID, self._retry_s)
                self._warned_absent = True
            self._next_attempt = now + self._retry_s
            return False
        try:
            self._sw = Switcher(port=port)
        except (RuntimeError, serial.SerialException, OSError) as exc:
            logger.warning("switcher: failed to open %s: %s (retry in %.0fs)",
                           port, exc, self._retry_s)
            self._sw = None
            self._next_attempt = now + self._retry_s
            return False
        self._state = None
        self._warned_absent = False
        logger.info("switcher: opened on %s", port)
        return True

    def set_high(self, high: bool) -> bool:
        """Request mixing (high=True) or passthrough (high=False).

        No-op (returns True immediately) if already verified in that state.
        Returns False — without raising — if the device is absent or the
        reply doesn't confirm the requested state.
        """
        with self._lock:
            if self._state is high:
                return True
            if not self._ensure_open():
                return False
            try:
                reply = self._sw.shift_on() if high else self._sw.shift_off()
            except (serial.SerialException, OSError) as exc:
                logger.warning("switcher: command failed: %s", exc)
                self._close_locked()
                return False
            want = "shifter on" if high else "shifter off"
            if want not in reply:
                logger.warning("switcher: unexpected reply to shift %s: %r",
                               "on" if high else "off", reply)
                self._state = None
                return False
            self._state = high
            if self._settle_s > 0:
                time.sleep(self._settle_s)
            return True

    def verify(self, expected_high: bool) -> bool:
        """Re-query the switcher's *actual* state and reconcile the cache.

        ``set_high`` alone is idempotent against ``_state`` — if the STM32 is
        reset or power-cycled independently of this process while ``_state``
        still says "already on", ``set_high`` would never notice the drift and
        would keep skipping the command. Call this periodically (e.g. every
        ~10s in 2_device mode, where a pass never re-arms per cycle) to catch
        that case: query ``status``, and if it disagrees with what's wanted,
        force a real re-command rather than trusting the cache.
        """
        with self._lock:
            if not self._ensure_open():
                return False
            try:
                reply = self._sw.status()
            except (serial.SerialException, OSError) as exc:
                logger.warning("switcher: status query failed: %s", exc)
                self._close_locked()
                return False
            if "shifter on" in reply:
                actual = True
            elif "shifter off" in reply:
                actual = False
            else:
                logger.warning("switcher: unexpected status reply: %r", reply)
                actual = None
            if actual == expected_high:
                self._state = actual
                return True
            logger.warning("switcher: state drifted (status=%r, wanted %s) — "
                           "re-asserting", reply, "on" if expected_high else "off")
            self._state = None  # force set_high() past its idempotent short-circuit
        return self.set_high(expected_high)

    def healthy(self) -> bool:
        with self._lock:
            return self._sw is not None and self._state is not None

    def _close_locked(self) -> None:
        if self._sw is not None:
            try:
                self._sw.close()
            except Exception:
                pass
            self._sw = None
        self._state = None

    def close(self) -> None:
        with self._lock:
            self._close_locked()


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Frequency shifter control (STM32 USB-UART)",
    )
    parser.add_argument(
        "--port", "-p",
        default=None,
        help="Serial port (auto-detect STM32 Virtual COM Port if omitted)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ping", help="Check device and firmware version")
    sub.add_parser("status", help="Query shifter state")
    sp_shift = sub.add_parser("shift", help="shift on | shift off")
    sp_shift.add_argument("action", choices=["on", "off"], help="Enable mixing or passthrough")
    sub.add_parser("reset", help="Reset MAX2870, output disabled")

    args = parser.parse_args()

    try:
        sw = Switcher(port=args.port)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        if args.command == "ping":
            result = sw.ping()
        elif args.command == "status":
            result = sw.status()
        elif args.command == "shift":
            if args.action == "on":
                result = sw.shift_on()
            else:
                result = sw.shift_off()
        elif args.command == "reset":
            result = sw.reset()
        else:
            result = "unknown command"

        print(result)
    finally:
        sw.close()


if __name__ == "__main__":
    main()
