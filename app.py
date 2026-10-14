#!/usr/bin/env python3
"""
app.py
======
Entry point for the Frontera comparison-test web app. Runs on the OrangePi
(device at /dev/ttyACM0) and serves the Comparison Test page + Test Journal
over HTTP.

Usage
-----
    python3 app.py                          # real device on /dev/ttyACM0, port 8080
    python3 app.py --mock                   # no hardware needed, synthetic spectra
    python3 app.py --device-port /dev/ttyACM1 --port 8090
    python3 app.py --device-mode 2_device   # two Fronteras + frequency shifter

--device-mode only sets the *starting* topology; the operator switches it in
the web UI without restarting the server.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from testbench.config import DEVICE_MODES, Config
from testbench.db import JournalDB
from testbench.device import DeviceManager
from testbench.server import serve

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("testbench.app")


def main() -> int:
    ap = argparse.ArgumentParser(description="Frontera comparison test web app.")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--device-port", default="",
                    help="low-band Frontera; blank/failed -> /dev/frontera1 or probe")
    ap.add_argument("--device-port2", default="",
                    help="Frontera behind the shifter; blank -> /dev/frontera2 or probe")
    ap.add_argument("--switcher-port", default="",
                    help="frequency shifter; blank -> /dev/switcher or ping-probe")
    ap.add_argument("--device-mode", default=DEVICE_MODES[0], choices=DEVICE_MODES,
                    help="starting hardware topology (switchable in the UI)")
    ap.add_argument("--offset2-mhz", type=float, default=6000.0,
                    help="frequency the shifter moves the high band by")
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--mock", action="store_true", help="synthesize spectra, no hardware")
    args = ap.parse_args()

    cfg = Config(
        http_host=args.host,
        http_port=args.port,
        device_port=args.device_port,
        device_port2=args.device_port2,
        switcher_port=args.switcher_port,
        device_mode=args.device_mode,
        offset2_mhz=args.offset2_mhz,
        mock=args.mock,
        results_dir=Path(args.results_dir),
    )
    cfg.ensure_dirs()

    # Deliberately not fatal: with a runtime mode switcher in the UI, missing
    # hardware is a state the operator has to see and fix (wrong mode selected,
    # shifter not plugged in yet), not a reason to refuse to start.
    device = DeviceManager(cfg)
    if device.error:
        logger.error("hardware incomplete: %s", device.error)
        logger.error("server is up — pick a mode / check the connection in the web UI")

    db = JournalDB(str(cfg.db_path))
    httpd = serve(cfg, device, db)
    logger.info("mode: %s | mock: %s", cfg.mode_label(), cfg.mock)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        device.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
