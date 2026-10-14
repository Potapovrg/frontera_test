"""Arinst TG R3 packet layer, against a fake serial port."""
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from testbench import arinst_tg as at  # noqa: E402


class FakeSerial:
    def __init__(self, reply=b""):
        self.reply, self.sent = reply, b""

    def reset_input_buffer(self): pass
    def flush(self): pass
    def close(self): pass
    def write(self, b): self.sent += b

    def read(self, n):
        out, self.reply = self.reply[:n], self.reply[n:]
        return out


def test_packets_match_hardware_capture():
    # Bytes captured against the real device (see session notes).
    assert at.build_packet(0xC0).hex() == "bbc000007b2c"
    pl = struct.pack("<QBB", 1_000_000_000, 0, 7)
    assert at.build_packet(0xC3, pl).hex() == "bbc30a0000ca9a3b0000000000072e27"


def test_echo_and_generator_ack():
    tg = at.ArinstTG(ser=FakeSerial(at.build_packet(0xC0)))
    assert tg.echo()
    tg = at.ArinstTG(ser=FakeSerial(at.build_packet(0xC3)))
    tg.generator_off()
    assert tg.ser.sent == at.build_packet(0xC3, struct.pack("<QBB", 10**9, 0, 7))


def test_error_and_bad_checksum():
    tg = at.ArinstTG(ser=FakeSerial(at.build_packet(0xFC)))
    with pytest.raises(at.ArinstError):
        tg.set_generator(10**9, True)
    bad = bytearray(at.build_packet(0xC3)); bad[-1] ^= 1
    with pytest.raises(at.ArinstError):
        at.ArinstTG(ser=FakeSerial(bytes(bad))).set_generator(10**9, True)


def test_spectrum_decodes_mags_then_freqs():
    mags = np.array([-80, -70, -60], "<f4"); freqs = np.array([100, 101, 102], "<f4")
    tg = at.ArinstTG(ser=FakeSerial(at.build_packet(0xC2, mags.tobytes() + freqs.tobytes())))
    f, p = tg.spectrum(100_000_000, 200_000_000)
    assert f.tolist() == [100, 101, 102] and p.tolist() == [-80, -70, -60]


def test_argument_validation():
    tg = at.ArinstTG(ser=FakeSerial())
    for bad in (lambda: tg.set_generator(1000, True),
                lambda: tg.set_generator(10**9, True, 8),
                lambda: tg.spectrum(100_000_000, 101_000_000)):
        with pytest.raises(ValueError):
            bad()
