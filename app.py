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
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from testbench.config import Config
from testbench.db import JournalDB
from testbench.device import DeviceError, DeviceManager
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
    ap.add_argument("--device-port", default="/dev/ttyACM0")
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--mock", action="store_true", help="synthesize spectra, no hardware")
    args = ap.parse_args()

    cfg = Config(
        http_host=args.host,
        http_port=args.port,
        device_port=args.device_port,
        mock=args.mock,
        results_dir=Path(args.results_dir),
    )
    cfg.ensure_dirs()

    try:
        device = DeviceManager(
            port=cfg.device_port,
            mock=cfg.mock,
            samples=cfg.samples,
            timeout_ms=cfg.timeout_ms,
            scan_timeout_s=cfg.scan_timeout_s,
        )
    except DeviceError as exc:
        logger.error("cannot start: %s", exc)
        return 1

    db = JournalDB(str(cfg.db_path))
    httpd = serve(cfg, device, db)
    logger.info("mock mode: %s", cfg.mock)
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
