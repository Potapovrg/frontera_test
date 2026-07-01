#!/usr/bin/env python3
"""
run_scan.py
===========
Entry point that runs *on the OrangePi* against the Frontera / Arinst SA6
spectrum analyzer, which enumerates there as /dev/ttyACM0.

Usage
-----
    python3 run_scan.py                       # default 500-1500 MHz, 5 MHz step
    python3 run_scan.py --port /dev/ttyACM0 --start 500 --stop 1500 --step 5
"""
from __future__ import annotations

import argparse
import sys

from frontera_interface import FronteraInterface, find_sa6


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a Frontera spectrum scan.")
    ap.add_argument("--port", default="/dev/ttyACM0",
                    help="serial port (default: /dev/ttyACM0; 'auto' = probe)")
    ap.add_argument("--start", type=float, default=500.0, help="start MHz")
    ap.add_argument("--stop", type=float, default=1500.0, help="stop MHz")
    ap.add_argument("--step", type=float, default=5.0, help="step MHz")
    ap.add_argument("--timeout", type=float, default=5.0, help="response timeout s")
    args = ap.parse_args()

    port = args.port
    if port == "auto":
        port = find_sa6()
        if not port:
            print("ERROR: no SA6/Frontera device found on any serial port")
            return 2
        print(f"auto-detected device on {port}")

    with FronteraInterface() as dev:
        if not dev.connect(port):
            print(f"ERROR: could not open {port}")
            return 2
        print(f"connected to {port}")

        result = dev.scan_mhz(args.start, args.stop, args.step, timeout=args.timeout)
        if result is None:
            print("ERROR: scan returned no data (timeout / parse error)")
            return 3

        n = result.freq_mhz.size
        peak_i = int(result.power_dbm.argmax())
        print(
            f"scan OK: {n} points  {result.start_freq_mhz:.1f}-"
            f"{result.stop_freq_mhz:.1f} MHz  step {result.step_mhz:.3f} MHz  "
            f"{result.elapsed_ms} ms"
        )
        print(
            f"  peak: {result.power_dbm[peak_i]:.1f} dBm @ "
            f"{result.freq_mhz[peak_i]:.1f} MHz"
        )
        print(
            f"  power range: {result.power_dbm.min():.1f} .. "
            f"{result.power_dbm.max():.1f} dBm"
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
