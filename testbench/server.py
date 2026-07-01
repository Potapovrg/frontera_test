"""HTTP layer — ThreadingHTTPServer + BaseHTTPRequestHandler, same pattern as
frontera_ml/persistence/log_server.py. Routes tie together device acquisition,
plotting, binary storage and the journal DB.
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import pages, plotting, storage
from .config import Config
from .db import JournalDB
from .device import DeviceError, DeviceManager

logger = logging.getLogger(__name__)


def make_handler(cfg: Config, device: DeviceManager, db: JournalDB):
    # In-memory "last sweep per block" — cleared to a fresh comparison whenever
    # both blocks have been re-run. Guarded by state_lock since each HTTP
    # request runs on its own thread.
    current: dict = {}
    state_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body, ctype: str = "text/html") -> None:
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj), "application/json")

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw or b"{}")

        def _serve_file(self, root, filename: str, ctype: str) -> None:
            try:
                fp = storage.safe_path(root, filename)
            except FileNotFoundError:
                self._send(404, "not found")
                return
            if not fp.is_file():
                self._send(404, "not found")
                return
            self._send(200, fp.read_bytes(), ctype)

        # -- routing -----------------------------------------------------
        def do_GET(self):
            # Strip the query string (e.g. "?t=..." cache-busters) before routing —
            # otherwise it gets treated as part of the filename in /plot,/report,/data.
            path = urlsplit(self.path).path
            if path == "/" or path.startswith("/index"):
                self._send(200, pages.comparison_page())
            elif path == "/journal":
                self._send(200, pages.journal_page())
            elif path == "/api/journal":
                self._send_json(200, db.recent())
            elif path.startswith("/plot/"):
                self._serve_file(cfg.plots_dir, path[len("/plot/"):], "image/png")
            elif path.startswith("/report/"):
                self._serve_file(cfg.reports_dir, path[len("/report/"):], "application/pdf")
            elif path.startswith("/data/"):
                self._serve_file(
                    cfg.data_dir, path[len("/data/"):], "application/octet-stream"
                )
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path == "/api/sweep":
                self._handle_sweep()
            elif self.path == "/api/report":
                self._handle_report()
            elif self.path == "/api/journal/delete":
                self._handle_journal_delete()
            else:
                self.send_response(404)
                self.end_headers()

        # -- handlers ------------------------------------------------------
        def _handle_sweep(self):
            try:
                body = self._read_json()
                block = body.get("block")
                if block not in ("A", "B"):
                    self._send_json(400, {"error": "block must be A or B"})
                    return
                start = float(body["start"])
                stop = float(body["stop"])
                step = float(body["step"])
                conditions = str(body.get("conditions", ""))
                if not (0 < step) or not (start < stop):
                    self._send_json(400, {"error": "invalid start/stop/step"})
                    return
            except (KeyError, ValueError, TypeError) as exc:
                self._send_json(400, {"error": f"bad request: {exc}"})
                return

            try:
                sweep = device.sweep(start, stop, step)
            except DeviceError as exc:
                logger.error("sweep failed: %s", exc)
                self._send_json(502, {"error": str(exc)})
                return

            stamp = storage.make_stamp()
            npz_name = storage.save_sweep(cfg, block, stamp, sweep)
            peak_dbm, peak_freq = plotting.peak_info(sweep.freq_mhz, sweep.power_dbm)
            title = f"Block {block} — {conditions or storage.utc_now_str()}"
            png_name = plotting.spectrum_png(
                cfg, stamp, block, sweep.freq_mhz, sweep.power_dbm, title
            )

            with state_lock:
                current[block] = {
                    "start": start, "stop": stop, "step": step,
                    "conditions": conditions,
                    "freq_mhz": sweep.freq_mhz, "power_dbm": sweep.power_dbm,
                    "npz": npz_name, "png": png_name,
                    "peak_dbm": peak_dbm, "peak_freq": peak_freq,
                    "n_points": int(sweep.freq_mhz.size),
                }

            self._send_json(200, {
                "png_url": f"/plot/{png_name}",
                "peak_dbm": peak_dbm,
                "peak_freq": peak_freq,
                "n_points": int(sweep.freq_mhz.size),
            })

        def _handle_report(self):
            with state_lock:
                rec_a = current.get("A")
                rec_b = current.get("B")
            if rec_a is None or rec_b is None:
                self._send_json(400, {"error": "run both Block A and Block B first"})
                return

            stamp = storage.make_stamp()
            ts_utc = storage.utc_now_str()
            meta = {
                "ts_utc": ts_utc,
                "start_a": rec_a["start"], "stop_a": rec_a["stop"], "step_a": rec_a["step"],
                "start_b": rec_b["start"], "stop_b": rec_b["stop"], "step_b": rec_b["step"],
                "conditions_a": rec_a["conditions"], "conditions_b": rec_b["conditions"],
            }
            pdf_name = plotting.comparison_pdf(
                cfg, stamp,
                rec_a["freq_mhz"], rec_a["power_dbm"],
                rec_b["freq_mhz"], rec_b["power_dbm"],
                meta,
            )

            comparison_id = db.insert({
                "ts_utc": ts_utc,
                "start_a": rec_a["start"], "stop_a": rec_a["stop"], "step_a": rec_a["step"],
                "start_b": rec_b["start"], "stop_b": rec_b["stop"], "step_b": rec_b["step"],
                "conditions_a": rec_a["conditions"], "conditions_b": rec_b["conditions"],
                "npy_a": rec_a["npz"], "npy_b": rec_b["npz"],
                "png_a": rec_a["png"], "png_b": rec_b["png"],
                "peak_dbm_a": rec_a["peak_dbm"], "peak_freq_a": rec_a["peak_freq"],
                "peak_dbm_b": rec_b["peak_dbm"], "peak_freq_b": rec_b["peak_freq"],
                "n_points_a": rec_a["n_points"], "n_points_b": rec_b["n_points"],
                "pdf_path": pdf_name,
            })

            self._send_json(200, {"pdf_url": f"/report/{pdf_name}", "comparison_id": comparison_id})

        def _handle_journal_delete(self):
            body = self._read_json()
            try:
                ids = [int(i) for i in body.get("ids", [])]
            except (TypeError, ValueError):
                self._send_json(400, {"error": "ids must be a list of integers"})
                return

            rows = db.delete(ids)
            for row in rows:
                storage.remove_if_exists(cfg.plots_dir, row.get("png_a") or "")
                storage.remove_if_exists(cfg.plots_dir, row.get("png_b") or "")
                storage.remove_if_exists(cfg.data_dir, row.get("npy_a") or "")
                storage.remove_if_exists(cfg.data_dir, row.get("npy_b") or "")
                storage.remove_if_exists(cfg.reports_dir, row.get("pdf_path") or "")

            self._send_json(200, {"deleted": len(rows)})

    return Handler


def serve(cfg: Config, device: DeviceManager, db: JournalDB) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((cfg.http_host, cfg.http_port), make_handler(cfg, device, db))
    logger.info("test bench server on http://%s:%d", cfg.http_host, cfg.http_port)
    return httpd
