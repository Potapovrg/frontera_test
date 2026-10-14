"""Arinst SSA-TG R3 tracking-generator / analyzer client (binary API).

Not the SA6/Frontera ASCII protocol: the R3 speaks Arinst's packet API, from
the vendor's SSA_R3_API_Example.zip (https://arinst.net/download-apk.php,
firmware 1.4.5 Basic / 2.4.5 PRO or later). Packet, little-endian:

    0xBB | cmd (0xC0-0xCF) | u16 payload_len | payload | 2-byte Fletcher checksum

The checksum covers everything before it. Errors come back as cmd 0xFC with an
empty payload. Verified on hardware: ECHO and SET_GENERATOR_POINT (state off).
The device's ECHO returns only 2 payload bytes while the length field says
more, so echo() compares against the header only.
"""
from __future__ import annotations

import logging
import struct
import time

import serial

logger = logging.getLogger(__name__)

HEAD = 0xBB
CMD_ECHO = 0xC0
CMD_SPECTRUM_U8 = 0xC1
CMD_SPECTRUM_FLOAT = 0xC2
CMD_SET_GENERATOR = 0xC3
CMD_TRACKING = 0xC4
CMD_ERROR = 0xFC

BAUD = 115200  # USB CDC: ignored by the device
USB_NAME = "Arinst_SSA_R3"  # substring of /dev/serial/by-id name

MIN_FREQ_HZ = 24_000_000
MAX_FREQ_HZ = 12_000_000_000
MIN_SPAN_HZ = 5_000_000

# Generator output power index -> dBm (3 dB steps)
GEN_POWER_DBM = {i: -8 - 3 * i for i in range(8)}
RF_IN = {"att15": 0, "direct": 1, "amp15": 2}   # -15 dB / 0 dB / +15 dB
BW_KHZ = {2.5: 0, 5: 1, 10: 2, 25: 3}
SPEED = {"fast": 0, "precise": 1, "turbo": 2}


class ArinstError(RuntimeError):
    pass


def fletcher(data: bytes) -> bytes:
    """8-bit Fletcher checksum (CK_A, CK_B), as in fletcher_checksum.m."""
    a = b = 0
    for x in data:
        a = (a + x) & 0xFF
        b = (b + a) & 0xFF
    return bytes((a, b))


def build_packet(cmd: int, payload: bytes = b"") -> bytes:
    pkt = bytes((HEAD, cmd)) + struct.pack("<H", len(payload)) + payload
    return pkt + fletcher(pkt)


def _check_range(start_hz: int, stop_hz: int, what: str) -> None:
    if not (MIN_FREQ_HZ <= start_hz < stop_hz <= MAX_FREQ_HZ):
        raise ValueError(f"{what}: need {MIN_FREQ_HZ} <= start < stop <= {MAX_FREQ_HZ} Hz")
    if stop_hz - start_hz < MIN_SPAN_HZ:
        raise ValueError(f"{what}: span must be >= {MIN_SPAN_HZ} Hz")


def find_arinst_tg() -> str | None:
    """Locate the R3 by its distinctive USB product string (unlike the STM32
    units it does not share a VID:PID-only identity with anything else here)."""
    from serial.tools import list_ports
    for p in list_ports.comports():
        if USB_NAME.replace("_", " ") in (p.product or "") or USB_NAME in (p.description or ""):
            return p.device
    return None


class ArinstTG:
    def __init__(self, port: str | None = None, timeout: float = 5.0, ser=None):
        port = port or find_arinst_tg()
        if ser is None and not port:
            raise ArinstError("Arinst SSA TG R3 not found; pass the port explicitly")
        self.port = port
        self.ser = ser or serial.Serial(port, BAUD, timeout=timeout)

    def close(self) -> None:
        self.ser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # -- transport ---------------------------------------------------------
    def _read_exact(self, n: int) -> bytes:
        buf = b""
        deadline = time.monotonic() + 20.0
        while len(buf) < n:
            chunk = self.ser.read(n - len(buf))
            if chunk:
                buf += chunk
            elif time.monotonic() > deadline:
                raise ArinstError(f"timeout: got {len(buf)} of {n} bytes")
        return buf

    def _transact(self, cmd: int, payload: bytes = b"") -> bytes:
        self.ser.reset_input_buffer()
        self.ser.write(build_packet(cmd, payload))
        self.ser.flush()
        head = self._read_exact(4)
        if head[0] != HEAD:
            raise ArinstError(f"bad header byte 0x{head[0]:02X}")
        (n,) = struct.unpack("<H", head[2:4])
        body = self._read_exact(n)
        crc = self._read_exact(2)
        if crc != fletcher(head + body):
            raise ArinstError("checksum mismatch in reply")
        if head[1] == CMD_ERROR:
            raise ArinstError(f"device reported error for command 0x{cmd:02X}")
        if head[1] != cmd:
            raise ArinstError(f"unexpected reply 0x{head[1]:02X} to 0x{cmd:02X}")
        return body

    # -- commands ----------------------------------------------------------
    def echo(self) -> bool:
        """Liveness check; sends an empty payload and expects it back."""
        try:
            return self._transact(CMD_ECHO) == b""
        except ArinstError:
            return False

    def set_generator(self, freq_hz: int, on: bool, power_index: int = 0) -> None:
        """Set the tracking-generator point. power_index 0..7 = -8..-29 dBm."""
        freq_hz = int(freq_hz)
        if not (MIN_FREQ_HZ <= freq_hz <= MAX_FREQ_HZ):
            raise ValueError(f"generator frequency out of {MIN_FREQ_HZ}..{MAX_FREQ_HZ} Hz")
        if power_index not in GEN_POWER_DBM:
            raise ValueError("power_index must be 0..7")
        self._transact(CMD_SET_GENERATOR,
                       struct.pack("<QBB", freq_hz, 1 if on else 0, power_index))

    def generator_off(self, freq_hz: int = 1_000_000_000) -> None:
        self.set_generator(freq_hz, on=False, power_index=7)

    def spectrum(self, start_hz: int, stop_hz: int, rf_in: str = "direct",
                 bw_khz: float = 25, speed: str = "fast"):
        """Float spectrum. Returns (freq_mhz, power_dbm) numpy arrays."""
        import numpy as np
        _check_range(start_hz, stop_hz, "spectrum")
        payload = struct.pack("<QQBBB", int(start_hz), int(stop_hz),
                              RF_IN[rf_in], BW_KHZ[bw_khz], SPEED[speed])
        body = self._transact(CMD_SPECTRUM_FLOAT, payload)
        data = np.frombuffer(body, dtype="<f4")
        half = data.size // 2
        return data[half:half * 2].astype(float), data[:half].astype(float)

    def tracking(self, start_hz: int, stop_hz: int, rf_in: str = "direct",
                 power_index: int = 0):
        """Tracking scan (generator follows the receiver). Returns (freq_mhz, mag_db).
        Through-calibration must be done by the host."""
        import numpy as np
        _check_range(start_hz, stop_hz, "tracking")
        if power_index not in GEN_POWER_DBM:
            raise ValueError("power_index must be 0..7")
        payload = struct.pack("<QQBB", int(start_hz), int(stop_hz),
                              RF_IN[rf_in], power_index)
        body = self._transact(CMD_TRACKING, payload)
        data = np.frombuffer(body, dtype="<f4")
        half = data.size // 2
        return data[half:half * 2].astype(float), data[:half].astype(float)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Arinst SSA-TG R3 quick check")
    ap.add_argument("--port", default=None)
    ap.add_argument("cmd", choices=("echo", "gen-off", "gen-on"))
    ap.add_argument("--freq-mhz", type=float, default=1000.0)
    ap.add_argument("--power-index", type=int, default=7)
    a = ap.parse_args()
    with ArinstTG(a.port) as tg:
        if a.cmd == "echo":
            print("echo ok" if tg.echo() else "echo FAILED")
        elif a.cmd == "gen-off":
            tg.generator_off(int(a.freq_mhz * 1e6)); print("generator off")
        else:
            tg.set_generator(int(a.freq_mhz * 1e6), True, a.power_index)
            print(f"generator ON {a.freq_mhz} MHz, {GEN_POWER_DBM[a.power_index]} dBm")
