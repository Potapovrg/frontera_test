"""
frontera_interface.py
=====================
Python interface library for the Arinst SA6 / Frontera spectrum analyzer.

Implements the Arinst SA6 USB-serial protocol (legacy scn20 mode) so the
device can be controlled from a PC without the Android app.

Protocol summary
----------------
- USB serial: 115200 baud, 8N1, no flow control
- Commands:   ASCII text terminated with \\r\\n
- Command index: 5-bit rolling counter (0-31), echoed in every response
- Scan response: binary-encoded spectrum data followed by ``complete\\r\\n``
- Amplitude encoding: ``(800 - encoded_11bit) / 10.0``  [dBm]

Quick start
-----------
Single range (up to ~600 points)::

    from frontera_interface import FronteraInterface, find_sa6

    port = find_sa6()          # auto-detect
    with FronteraInterface() as dev:
        dev.connect(port)
        result = dev.scan_mhz(500, 1500, 5)
        for pt in result.points:
            print(f"{pt.freq_mhz:.1f} MHz  {pt.amplitude_dbm:.1f} dBm")

Wide range with automatic chunking::

    from frontera_interface import FronteraInterface, find_sa6

    port = find_sa6()
    with FronteraInterface() as dev:
        dev.connect(port)
        # Scans 100–6000 MHz in ~600-point chunks, with persistent buffer.
        # Repeated calls for the same range update only changed chunks —
        # failed chunks retain their previous data (no chart flicker).
        result = dev.scan_chunked_mhz(100.0, 6000.0, step_mhz=1.0)
        for pt in result.points:
            print(f"{pt.freq_mhz:.1f} MHz  {pt.amplitude_dbm:.1f} dBm")

Dependencies
------------
    pip install pyserial
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
import serial
import serial.tools.list_ports

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------
_BAUD_RATE      = 115_200
_INDEX_MASK     = 0x1F          # 5-bit rolling counter: values 0-31
_INTERMED_FREQ  = 10_700_000    # Hz – hardware IF constant, cannot change
_COMPLETE_MARK  = b"complete\r\n"

# Maximum spectrum points the device reliably returns in a single scan command.
# Wider ranges must be split into chunks of at most this many points.
MAX_POINTS_PER_CHUNK = 600

# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------

@dataclass
class SpectrumPoint:
    """Single frequency/amplitude sample from a spectrum sweep."""
    freq_mhz:      float   # centre frequency in MHz
    amplitude_dbm: float   # amplitude in dBm


@dataclass
class FullSweepChunk:
    """
    A single chunk task for a full-range sweep.

    Mirrors ``ScanTask.FullSweepChunk`` in ``UsbDataInteractorImpl.kt``.
    """
    start_mhz:     float   # chunk start frequency in MHz
    stop_mhz:      float   # chunk stop  frequency in MHz
    step_mhz:      float   # frequency step in MHz
    chunk_index:   int     # 0-based position in the chunk_data array
    is_last_chunk: bool    # True for the final chunk of the sweep


@dataclass
class ScanResult:
    """Result of a full spectrum scan command.

    The spectrum is stored as parallel float32 numpy arrays (``freq_mhz`` /
    ``power_dbm``) — decoded straight from the device's binary block without an
    intermediate per-point object list, which is the hot path for the detector.
    The :attr:`points` property reconstructs the legacy ``List[SpectrumPoint]``
    view lazily for the few offline callers that still expect it.
    """
    start_freq_mhz: float
    stop_freq_mhz:  float
    step_mhz:       float
    attenuation_db: float           # attenuation applied during the scan
    freq_mhz:  np.ndarray = field(default_factory=lambda: np.empty(0, np.float32))
    power_dbm: np.ndarray = field(default_factory=lambda: np.empty(0, np.float32))
    elapsed_ms:     int   = 0       # time reported by device (ms)
    cmd_index:      int   = -1      # rolling command index echoed by device

    @property
    def points(self) -> List[SpectrumPoint]:
        """Legacy per-point view, rebuilt on demand (not used in the hot path)."""
        return [SpectrumPoint(float(f), float(p))
                for f, p in zip(self.freq_mhz, self.power_dbm)]


# ---------------------------------------------------------------------------
# Module-level helper: auto-detect the SA6 port
# ---------------------------------------------------------------------------

def find_sa6() -> Optional[str]:
    """
    Probe USB/ACM serial ports and return the first one that responds
    to an SA6 scan command.  Returns None if no SA6 is found.

    Strategy
    --------
    1. List COM/ACM ports whose description or device path looks USB-related.
    2. Send a minimal scn20 probe (5800–5810 MHz, 1 MHz step, index=1).
    3. If the response contains ``b"scn20"`` this is the SA6 port.
    """
    candidates = [
        p.device
        for p in serial.tools.list_ports.comports()
        if ("USB"    in (p.description or "").upper()
            or "USB"    in (p.device or "").upper()
            or "ACM"    in (p.device or "").upper()
            or "SERIAL" in (p.description or "").upper()
            or "CH340"  in (p.description or "").upper()
            or "CP210"  in (p.description or "").upper()
            or "FTDI"   in (p.description or "").upper()
            or "PL230"  in (p.description or "").upper())
    ]

    if not candidates:
        logger.debug("find_sa6: no USB serial ports found")
        return None

    # Build a minimal probe command (small frequency range for fast response)
    probe = _build_scn20(
        start_hz      = 5_800_000_000,
        stop_hz       = 5_810_000_000,
        step_hz       = 1_000_000,
        attenuation_db= 0.0,
        timeout_ms    = 200,
        samples       = 20,
        index         = 1,
    )

    for port in candidates:
        logger.debug("find_sa6: probing %s", port)
        try:
            with serial.Serial(port, _BAUD_RATE, timeout=2.0) as s:
                time.sleep(0.1)
                s.reset_input_buffer()
                s.write(probe)
                resp = s.read(256)
            if b"scn20" in resp:
                logger.info("find_sa6: SA6 found on %s", port)
                return port
        except (serial.SerialException, OSError) as exc:
            logger.debug("find_sa6: %s – %s", port, exc)

    logger.debug("find_sa6: SA6 not found on any port")
    return None


# ---------------------------------------------------------------------------
# Internal command builders
# ---------------------------------------------------------------------------

def _build_scn20(
    start_hz: int,
    stop_hz:  int,
    step_hz:  int,
    attenuation_db: float,
    timeout_ms: int,
    samples:    int,
    index:      int,
) -> bytes:
    """Build a scn20 ASCII command byte string."""
    fmt_att = int(attenuation_db * 100) + 10_000
    cmd = (
        f"scn20 {start_hz} {stop_hz} {step_hz} "
        f"{timeout_ms} {samples} {_INTERMED_FREQ} {fmt_att} {index}\r\n"
    )
    return cmd.encode("ascii")


# ---------------------------------------------------------------------------
# Main interface class
# ---------------------------------------------------------------------------

class FronteraInterface:
    """
    Synchronous Python interface for the Arinst SA6 / Frontera spectrum
    analyzer connected via USB serial.

    Example
    -------
    ::

        dev = FronteraInterface()
        dev.connect("COM3")             # or "/dev/ttyUSB0"
        result = dev.scan_mhz(500, 1500, 5)
        dev.disconnect()

    or as a context manager::

        with FronteraInterface() as dev:
            dev.connect("COM3")
            result = dev.scan_mhz(500, 1500, 5)
    """

    def __init__(self) -> None:
        self._serial: Optional[serial.Serial] = None
        self._index:  int = 0           # rolling 5-bit command counter
        # Stored from last scan command; needed to parse the response
        self._last_stop_hz: int = 0
        self._last_step_hz: int = 0
        # Persistent chunk buffer for scan_chunked_mhz().
        # Survives across repeated calls for the same range so that a failed
        # chunk retry keeps the previous sweep's data (no chart flicker).
        self._chunk_data:      List[Optional[Tuple[np.ndarray, np.ndarray]]] = []
        self._chunk_range_key: tuple = ()   # (start_mhz, stop_mhz, step_mhz)
        self._needs_drain: bool = False     # set after timeout/parse failure; cleared by _drain_port

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "FronteraInterface":
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, port: str, open_timeout: float = 3.0) -> bool:
        """
        Open the serial port connected to the SA6 device.

        Parameters
        ----------
        port:
            Serial port name, e.g. ``"COM3"`` or ``"/dev/ttyUSB0"``.
        open_timeout:
            Seconds to wait for the port to open (unused on most platforms,
            kept for API clarity).

        Returns
        -------
        bool
            True on success, False on error.
        """
        try:
            self._serial = serial.Serial(
                port     = port,
                baudrate = _BAUD_RATE,
                bytesize = serial.EIGHTBITS,
                parity   = serial.PARITY_NONE,
                stopbits = serial.STOPBITS_ONE,
                timeout  = 0.1,
            )
            logger.info("Connected to %s at %d baud", port, _BAUD_RATE)
            return True
        except serial.SerialException as exc:
            logger.error("Failed to connect to %s: %s", port, exc)
            self._serial = None
            return False

    def disconnect(self) -> None:
        """Close the serial port."""
        if self._serial and self._serial.is_open:
            self._serial.close()
            logger.info("Disconnected")
        self._serial = None

    @property
    def is_connected(self) -> bool:
        """True if the serial port is currently open."""
        return self._serial is not None and self._serial.is_open

    # ------------------------------------------------------------------
    # Spectrum scan
    # ------------------------------------------------------------------

    def scan(
        self,
        start_hz:       int,
        stop_hz:        int,
        step_hz:        int,
        attenuation_db: float = 0.0,
        timeout_ms:     int   = 200,
        samples:        int   = 20,
        timeout:        float = 5.0,
    ) -> Optional[ScanResult]:
        """
        Run a full spectrum scan and return the result.

        Parameters
        ----------
        start_hz:
            Start frequency in Hz (e.g. ``500_000_000`` for 500 MHz).
        stop_hz:
            Stop frequency in Hz.
        step_hz:
            Frequency step in Hz.
        attenuation_db:
            RF attenuation in dB (default 0.0).  Encoded as
            ``int(attenuation_db * 100) + 10000`` in the command.
        timeout_ms:
            Dwell time per frequency step in milliseconds (default 200).
            Lower = faster scan, potentially noisier readings.
        samples:
            Measurements averaged per step (default 20).
            Higher = better SNR, but slower.
        timeout:
            Maximum seconds to wait for the device response (default 5.0).

        Returns
        -------
        ScanResult or None
            Parsed scan result, or ``None`` on timeout / parse error.
        """
        self._require_connected()

        self._last_stop_hz = stop_hz
        self._last_step_hz = step_hz

        idx  = self._next_index()
        cmd  = _build_scn20(start_hz, stop_hz, step_hz,
                            attenuation_db, timeout_ms, samples, idx)
        if self._needs_drain:
            self._drain_port()
            self._needs_drain = False
        self._serial.reset_input_buffer()   # clear stale bytes from any previous timeout
        self._send_raw(cmd)
        logger.debug("TX scan cmd index=%d  %d–%d Hz  step=%d Hz",
                     idx, start_hz, stop_hz, step_hz)

        raw = self._read_until(_COMPLETE_MARK, timeout, extend_s=0.5)
        if raw is None:
            logger.warning("scan: no response within %.1fs", timeout)
            self._needs_drain = True
            return None

        result = self._parse_scan(raw, stop_hz, step_hz)
        if result is not None:
            result.attenuation_db = attenuation_db
        else:
            self._needs_drain = True    # parse failure — buffer may have garbage
        return result

    def scan_mhz(
        self,
        start_mhz: float,
        stop_mhz:  float,
        step_mhz:  float = 1.0,
        **kwargs,
    ) -> Optional[ScanResult]:
        """
        Convenience wrapper for :meth:`scan` that accepts MHz values.

        Parameters
        ----------
        start_mhz:
            Start frequency in MHz (e.g. ``500.0``).
        stop_mhz:
            Stop frequency in MHz.
        step_mhz:
            Step size in MHz (default ``1.0``).
        **kwargs:
            Forwarded to :meth:`scan` (``attenuation_db``, ``timeout_ms``,
            ``samples``, ``timeout``).

        Returns
        -------
        ScanResult or None
        """
        return self.scan(
            start_hz = int(start_mhz * 1_000_000),
            stop_hz  = int(stop_mhz  * 1_000_000),
            step_hz  = int(step_mhz  * 1_000_000),
            **kwargs,
        )

    def scan_chunked_mhz(
        self,
        start_mhz: float,
        stop_mhz:  float,
        step_mhz:  float = 1.0,
        **kwargs,
    ) -> Optional[ScanResult]:
        """
        Scan a wide frequency range by splitting it into chunks of at most
        ``MAX_POINTS_PER_CHUNK`` points each, then assembling the results into
        a single :class:`ScanResult`.

        Mirrors the initialisation block of ``runSimpleScanLoop`` in
        ``UsbDataInteractorImpl.kt``:

        1. **Chunk calculation** – the range is divided into sub-ranges of at
           most ``MAX_POINTS_PER_CHUNK × step_mhz`` MHz each.  If the final
           chunk would be narrower than 20 MHz it is merged into the preceding
           chunk instead.
        2. **Persistent chunk_data** – a per-instance buffer stores the last
           successful result for every chunk slot.  On repeated calls for the
           same ``(start_mhz, stop_mhz, step_mhz)`` range, only the chunks
           that return data are updated; failed chunks retain their previous
           values so the caller always receives a complete spectrum.
        3. **Task queue** – a :class:`FullSweepChunk` object is built for every
           chunk and processed in order (each with one automatic retry).

        Parameters
        ----------
        start_mhz:
            Start of the full sweep range in MHz (e.g. ``100.0``).
        stop_mhz:
            End of the full sweep range in MHz (e.g. ``6000.0``).
        step_mhz:
            Frequency step in MHz (default ``1.0``).
        **kwargs:
            Forwarded to :meth:`scan_mhz`
            (``attenuation_db``, ``timeout_ms``, ``samples``, ``timeout``).

        Returns
        -------
        ScanResult or None
            Combined result containing the accumulated points from all chunk
            slots.  Returns ``None`` only if *every* slot is still empty (i.e.
            the very first call fails completely).
        """
        chunk_size_mhz = max(MAX_POINTS_PER_CHUNK * step_mhz, 50.0)
        total_range    = stop_mhz - start_mhz
        raw_chunks     = math.ceil(total_range / chunk_size_mhz)
        last_size      = total_range - (raw_chunks - 1) * chunk_size_mhz
        num_chunks     = (
            raw_chunks - 1
            if raw_chunks > 1 and last_size < 20.0
            else raw_chunks
        )

        # Invalidate the persistent buffer when the range or step changes.
        range_key = (start_mhz, stop_mhz, step_mhz)
        if range_key != self._chunk_range_key or len(self._chunk_data) != num_chunks:
            self._chunk_data      = [None for _ in range(num_chunks)]
            self._chunk_range_key = range_key
            logger.debug(
                "scan_chunked_mhz: new range %.1f–%.1f MHz  "
                "step=%.3f MHz  %d chunks × %.0f MHz",
                start_mhz, stop_mhz, step_mhz, num_chunks, chunk_size_mhz,
            )

        task_queue = self._build_full_sweep_tasks(
            start_mhz, stop_mhz, step_mhz, chunk_size_mhz, num_chunks
        )

        for task in task_queue:
            logger.debug(
                "scan_chunked_mhz: chunk %d/%d  %.1f–%.1f MHz",
                task.chunk_index + 1, num_chunks, task.start_mhz, task.stop_mhz,
            )
            result = self._scan_with_retry(
                task.start_mhz, task.stop_mhz, task.step_mhz, **kwargs
            )
            if result is None:
                logger.error(
                    "scan_chunked_mhz: chunk %d/%d failed after retry, "
                    "keeping previous data",
                    task.chunk_index + 1, num_chunks,
                )
            else:
                self._chunk_data[task.chunk_index] = (result.freq_mhz, result.power_dbm)
                logger.debug(
                    "scan_chunked_mhz: chunk %d/%d → %d points",
                    task.chunk_index + 1, num_chunks, result.freq_mhz.size,
                )

        filled = [c for c in self._chunk_data if c is not None]
        if not filled:
            return None

        freq  = np.concatenate([c[0] for c in filled])
        power = np.concatenate([c[1] for c in filled])

        return ScanResult(
            start_freq_mhz = start_mhz,
            stop_freq_mhz  = stop_mhz,
            step_mhz       = step_mhz,
            attenuation_db = kwargs.get("attenuation_db", 0.0),
            freq_mhz       = freq,
            power_dbm      = power,
        )

    def _build_full_sweep_tasks(
        self,
        start_mhz:      float,
        stop_mhz:       float,
        step_mhz:       float,
        chunk_size_mhz: float,
        num_chunks:     int,
    ) -> List[FullSweepChunk]:
        """
        Build the ordered list of :class:`FullSweepChunk` tasks that cover
        ``start_mhz`` → ``stop_mhz``.

        Mirrors ``buildFullSweepTasks()`` in ``UsbDataInteractorImpl.kt``.
        """
        tasks = []
        for i in range(num_chunks):
            chunk_start = start_mhz + i * chunk_size_mhz
            chunk_stop  = (
                stop_mhz
                if i == num_chunks - 1
                else min(chunk_start + chunk_size_mhz, stop_mhz)
            )
            tasks.append(FullSweepChunk(
                start_mhz     = chunk_start,
                stop_mhz      = chunk_stop,
                step_mhz      = step_mhz,
                chunk_index   = i,
                is_last_chunk = (i == num_chunks - 1),
            ))
        return tasks

    def _scan_with_retry(
        self,
        start_mhz: float,
        stop_mhz:  float,
        step_mhz:  float,
        **kwargs,
    ) -> Optional[ScanResult]:
        """
        Scan a range and retry once on timeout.

        Mirrors ``scanWithRetry()`` in ``UsbDataInteractorImpl.kt``.
        """
        result = self.scan_mhz(start_mhz, stop_mhz, step_mhz, **kwargs)
        if result is None:
            logger.warning(
                "_scan_with_retry: timeout %.1f–%.1f MHz, retrying…",
                start_mhz, stop_mhz,
            )
            result = self.scan_mhz(start_mhz, stop_mhz, step_mhz, **kwargs)
            if result is None:
                logger.error(
                    "_scan_with_retry: failed after retry: %.1f–%.1f MHz",
                    start_mhz, stop_mhz,
                )
        return result

    # ------------------------------------------------------------------
    # Generator control
    # ------------------------------------------------------------------

    def set_frequency(
        self,
        addr:    int,
        freq_hz: int,
        wait:    bool = True,
    ) -> bool:
        """
        Set the frequency of a signal generator.

        Parameters
        ----------
        addr:
            Generator address (1–7).
        freq_hz:
            Target frequency in Hz.
        wait:
            If True, wait for a ``success`` response (up to 1 s).

        Returns
        -------
        bool
            True if acknowledged (or *wait* is False), False on timeout.
        """
        self._require_connected()
        idx = self._next_index()
        self._send_str(f"scf {addr} {freq_hz} {idx}\r\n")
        if wait:
            raw = self._read_until(b"success", timeout=1.0)
            return raw is not None and b"success" in raw
        return True

    def generator_on(
        self,
        addr:    int,
        time_ms: int,
        wait:    bool = True,
    ) -> bool:
        """
        Turn a signal generator ON for a specified duration.

        Parameters
        ----------
        addr:
            Generator address (1–7).
        time_ms:
            Duration in milliseconds.
        wait:
            If True, wait for a ``complete`` response (up to 1 s).

        Returns
        -------
        bool
            True if acknowledged (or *wait* is False), False on timeout.
        """
        self._require_connected()
        idx = self._next_index()
        self._send_str(f"gon {addr} {time_ms} {idx}\r\n")
        if wait:
            raw = self._read_until(b"complete", timeout=1.0)
            return raw is not None and b"complete" in raw
        return True

    def generator_off(
        self,
        addr: int,
        wait: bool = True,
    ) -> bool:
        """
        Turn a signal generator OFF.

        Parameters
        ----------
        addr:
            Generator address (1–7).
        wait:
            If True, wait for a ``complete`` response (up to 1 s).

        Returns
        -------
        bool
            True if acknowledged (or *wait* is False), False on timeout.
        """
        self._require_connected()
        idx = self._next_index()
        self._send_str(f"goff {addr} {idx}\r\n")
        if wait:
            raw = self._read_until(b"complete", timeout=1.0)
            return raw is not None and b"complete" in raw
        return True

    def test_device(self, addr: int) -> bool:
        """
        Send a TEST command to check whether device *addr* is online.

        Note: only relevant in the modern (non-legacy) Frontera protocol.
        The SA6 operates in legacy mode and does not send TEST responses.

        Parameters
        ----------
        addr:
            Device address (0 = analyzer, 1–7 = generators).

        Returns
        -------
        bool
            True if ``complete`` is received within 0.5 s.
        """
        self._require_connected()
        idx = self._next_index()
        self._send_str(f"test {addr} {idx}\r\n")
        raw = self._read_until(b"complete", timeout=0.5)
        return raw is not None and b"complete" in raw

    def reset_index(self, start: int = 0) -> None:
        """Reset the rolling command index counter."""
        self._index = start & _INDEX_MASK

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _next_index(self) -> int:
        idx = self._index
        self._index = (self._index + 1) & _INDEX_MASK
        return idx

    def _require_connected(self) -> None:
        if not self.is_connected:
            raise ConnectionError(
                "Device not connected. Call connect() first."
            )

    def _send_raw(self, data: bytes) -> None:
        assert self._serial is not None
        self._serial.write(data)
        self._serial.flush()

    def _send_str(self, command: str) -> None:
        logger.debug("TX: %r", command.strip())
        self._send_raw(command.encode("ascii"))

    def _read_until(
        self,
        marker:   bytes,
        timeout:  float,
        extend_s: float = 5.0,
    ) -> Optional[bytes]:
        """
        Read bytes from the serial port and accumulate them until *marker*
        appears anywhere in the buffer, or *timeout* seconds elapse.

        If the primary deadline fires while partial data is already in the
        buffer (the device is mid-response), a soft extension of *extend_s*
        seconds is granted so the in-progress response can complete without
        having to drain and retry.  The extension only activates when data
        has been received; an empty buffer on timeout returns ``None``
        immediately.

        Returns
        -------
        bytes or None
            The full accumulated buffer (including *marker*), or None on timeout.
        """
        assert self._serial is not None
        buffer   = b""
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            # Use a short read timeout so we stay responsive to the deadline
            self._serial.timeout = min(0.05, max(0.001, remaining))
            chunk = self._serial.read(4096)
            if chunk:
                buffer += chunk
                if marker in buffer:
                    return buffer

        # Primary deadline expired. If we already have partial data the device
        # is still transmitting — give it a short grace period to finish.
        if buffer:
            logger.debug(
                "_read_until: primary timeout after %.1fs (%d bytes), extending %.1fs",
                timeout, len(buffer), extend_s,
            )
            deadline2 = time.monotonic() + extend_s
            while time.monotonic() < deadline2:
                remaining = deadline2 - time.monotonic()
                self._serial.timeout = min(0.05, max(0.001, remaining))
                chunk = self._serial.read(4096)
                if chunk:
                    buffer += chunk
                    if marker in buffer:
                        logger.debug("_read_until: completed in extension window")
                        return buffer

        logger.warning(
            "_read_until: timeout after %.1fs+%.1fs, buffer=%d bytes",
            timeout, extend_s, len(buffer),
        )
        return None

    def _drain_port(self, max_wait_s: float = 10.0) -> int:
        """Read and discard the remainder of any in-flight device response.

        Reads until the ``complete\\r\\n`` end-marker is found (the SA6 always
        terminates a scan with it) or until the port has been silent for 0.5 s,
        whichever comes first.  A hard cap of *max_wait_s* prevents blocking
        forever if the device hangs.

        Waiting for ``complete`` (rather than just a brief silence) is critical:
        the SA6 takes several seconds to finish a scan, so a short silence check
        would return while the device is still mid-transmission, causing the
        next command to arrive before the previous response ends.

        Returns the total number of bytes discarded.
        """
        if self._serial is None:
            return 0
        discarded = 0
        buffer = b""
        deadline = time.monotonic() + max_wait_s
        while time.monotonic() < deadline:
            self._serial.timeout = 0.5      # wait up to 0.5 s for the next chunk
            chunk = self._serial.read(4096)
            if not chunk:
                break                       # 0.5 s of silence → device is idle
            buffer    += chunk
            discarded += len(chunk)
            if _COMPLETE_MARK in buffer:
                break                       # found end-of-scan marker → done
        if discarded:
            found = _COMPLETE_MARK in buffer
            logger.debug(
                "_drain_port: discarded %d bytes (complete_found=%s)",
                discarded, found,
            )
        return discarded

    # ------------------------------------------------------------------
    # Scan response parser  (mirrors PacketParser.kt + SpectrumParser.kt)
    # ------------------------------------------------------------------

    def _parse_scan(
        self,
        data:    bytes,
        stop_hz: int,
        step_hz: int,
    ) -> Optional[ScanResult]:
        """
        Parse a complete scn20 (or scn22) response packet.

        Packet layout
        -------------
        ::

            \\r\\nscn20 <startHz> <cmdIndex>\\r\\n
            <binary_spectrum_bytes>
            <FF> <FF>
            <elapsedMs>\\r\\n
            complete\\r\\n

        Each spectrum point is 2 bytes, big-endian:
        - bits 15–11: rolling point index (integrity check, wraps at 32)
        - bits 10–0:  encoded amplitude
        - ``amplitude_dBm = (800 - encoded) / 10.0``
        """
        try:
            text = data.decode("ascii", errors="replace")

            # 1. Locate the scn20 / scn22 header
            for cmd_tag in ("scn20", "scn22"):
                hdr_start = text.find(cmd_tag)
                if hdr_start != -1:
                    break
            else:
                logger.error("_parse_scan: no scn20/scn22 header found")
                return None

            hdr_end = text.find("\r\n", hdr_start)
            if hdr_end == -1:
                logger.error("_parse_scan: header end (\\r\\n) not found")
                return None

            parts = text[hdr_start:hdr_end].split()
            if len(parts) < 3:
                logger.error("_parse_scan: header too short: %r", parts)
                return None

            try:
                start_hz  = int(parts[1])
                cmd_index = int(parts[2])
            except ValueError as exc:
                logger.error("_parse_scan: cannot parse header fields: %s", exc)
                return None

            # 2. Extract binary block: after header \r\n, before 'complete'
            bin_start    = hdr_end + 2          # skip the header \r\n
            complete_pos = data.rfind(b"complete")
            if complete_pos == -1:
                logger.error("_parse_scan: 'complete' marker not found")
                return None

            binary_block = data[bin_start:complete_pos]

            # 3. Find the LAST FF FF pair (end-of-spectrum marker)
            ff_pos = _find_last_ff_pair(binary_block)
            if ff_pos == -1:
                logger.error("_parse_scan: FF FF terminator not found")
                return None

            # 4. Split spectrum bytes and elapsed-time tail
            spectrum_bytes = binary_block[:ff_pos]       # data before FF FF
            tail           = binary_block[ff_pos + 2:]  # elapsed time + \r\n

            elapsed_str = tail.decode("ascii", errors="ignore")
            elapsed_ms  = int(
                "".join(c for c in elapsed_str if c.isdigit()) or "0"
            )

            # 5. Decode amplitude points
            start_mhz = start_hz / 1_000_000.0
            stop_mhz  = stop_hz  / 1_000_000.0
            step_mhz  = step_hz  / 1_000_000.0
            freq, power = _decode_spectrum(spectrum_bytes, start_mhz, step_mhz)

            logger.debug(
                "_parse_scan: index=%d  %.1f–%.1f MHz  %d points  %d ms",
                cmd_index, start_mhz, stop_mhz, freq.size, elapsed_ms,
            )

            return ScanResult(
                start_freq_mhz = start_mhz,
                stop_freq_mhz  = stop_mhz,
                step_mhz       = step_mhz,
                attenuation_db = 0.0,   # overwritten by scan() after return
                freq_mhz       = freq,
                power_dbm      = power,
                elapsed_ms     = elapsed_ms,
                cmd_index      = cmd_index,
            )

        except Exception as exc:  # noqa: BLE001
            logger.error("_parse_scan: unexpected error: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Module-level pure functions (no device state required)
# ---------------------------------------------------------------------------

def _find_last_ff_pair(data: bytes) -> int:
    """
    Return the byte index of the **last** ``0xFF 0xFF`` pair in *data*,
    or -1 if not found.

    We search backwards (like ``findLastFFPairIndex`` in PacketParser.kt)
    so that any ``FF FF`` values that appear inside the spectrum data
    (valid but rare) do not confuse the parser — only the terminator at
    the very end of the data block is used.
    """
    return data.rfind(b"\xff\xff")


def _decode_spectrum(
    data:      bytes,
    start_mhz: float,
    step_mhz:  float,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Decode raw spectrum binary data into ``(freq_mhz, power_dbm)`` float32 arrays.

    Each point is 2 bytes, big-endian:

    - Bits 15–11 (5 bits): rolling point index ``i % 32`` (integrity check,
      not enforced — decode is vectorized for the detector hot path).
    - Bits 10–0  (11 bits): encoded amplitude.

    Amplitude formula (from Arinst documentation, corrected per test suite)::

        amplitude_dBm = (800 - encoded_amplitude) / 10.0

    Parameters
    ----------
    data:
        Raw bytes of spectrum data (NOT including the FF FF terminator).
    start_mhz:
        Frequency of the first point in MHz.
    step_mhz:
        Frequency step between points in MHz.
    """
    n = len(data) // 2    # each point = 2 bytes
    words = np.frombuffer(data, dtype=">u2", count=n)          # big-endian u16
    encoded = (words & 0x07FF).astype(np.int32)               # 11-bit amplitude
    power = ((800 - encoded) / 10.0).astype(np.float32)        # dBm
    freq = (start_mhz + step_mhz * np.arange(n)).astype(np.float32)
    return freq, power


# ---------------------------------------------------------------------------
# Sweep thread (waterfall GUI backend)
# ---------------------------------------------------------------------------

class FronteraSweepThread(threading.Thread):
    """
    Daemon thread that continuously sweeps via FronteraInterface.scan_chunked_mhz()
    and publishes completed rows through a lock.

    Public API is identical to SA6SweepThread and HackRFSweepThread so that
    waterfall_gui.py can use any backend interchangeably.
    """

    def __init__(self, port: str, start_mhz: float, stop_mhz: float,
                 step_mhz: float = 1.0, atten_db: float = 0.0):
        super().__init__(daemon=True)
        self.port      = port
        self.start_mhz = start_mhz
        self.stop_mhz  = stop_mhz
        self.step_mhz  = step_mhz
        self.atten_db  = atten_db

        self._lock         = threading.Lock()
        self._latest_freq  = None
        self._latest_power = None
        self.sweep_count   = 0
        self._paused       = False
        self._stop_flag    = False
        self.error_msg     = None

    # ── Public API ────────────────────────────────────────────────────────────

    def get_latest(self):
        """Return thread-safe copies of (freq_array, power_array), or (None, None)."""
        with self._lock:
            if self._latest_freq is None:
                return None, None
            return self._latest_freq.copy(), self._latest_power.copy()

    def pause(self):  self._paused = True
    def resume(self): self._paused = False
    def stop(self):   self._stop_flag = True

    @property
    def paused(self): return self._paused

    # ── Internal ──────────────────────────────────────────────────────────────

    def run(self):
        dev = FronteraInterface()
        if not dev.connect(self.port):
            self.error_msg = f"Cannot open port {self.port}"
            return
        _MAX_CONSECUTIVE_FAILS = 3
        try:
            fail_count = 0
            while not self._stop_flag:
                if self._paused:
                    time.sleep(0.05)
                    continue
                _timeout_ms = 10    # minimal dwell per step
                _samples    = 1     # no averaging — fastest scan
                _timeout_s  = 3.0   # hard deadline; soft extension adds 0.5 s
                result = dev.scan_chunked_mhz(
                    self.start_mhz, self.stop_mhz, self.step_mhz,
                    attenuation_db=float(self.atten_db),
                    timeout_ms=_timeout_ms,
                    samples=_samples,
                    timeout=_timeout_s,
                )
                if result is None:
                    fail_count += 1
                    if fail_count >= _MAX_CONSECUTIVE_FAILS:
                        logger.warning(
                            "FronteraSweepThread: %d consecutive scan failures, reconnecting…",
                            fail_count,
                        )
                        dev.disconnect()
                        time.sleep(1.0)
                        if not dev.connect(self.port):
                            self.error_msg = f"Cannot reconnect to {self.port}"
                            return
                        fail_count = 0
                    continue
                fail_count = 0
                with self._lock:
                    self._latest_freq  = result.freq_mhz
                    self._latest_power = result.power_dbm
                    self.sweep_count  += 1
        except Exception as exc:
            self.error_msg = str(exc)
        finally:
            dev.disconnect()
