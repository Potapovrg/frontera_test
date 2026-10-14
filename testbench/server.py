"""HTTP layer — ThreadingHTTPServer + BaseHTTPRequestHandler, same pattern as
frontera_ml/persistence/log_server.py. Routes tie together device acquisition,
plotting, binary storage and the journal DB.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import pages, plotting, storage
from .autotest import AutoTestRunner
from .config import DEVICE_MODES, Config
from .db import JournalDB
from .device import DeviceError, DeviceManager

logger = logging.getLogger(__name__)


_BLOCKED_DURING_AUTO = frozenset(
    {"/api/sweep", "/api/hardware/mode", "/api/hardware/probe", "/api/switcher"})


def make_handler(cfg: Config, device: DeviceManager, db: JournalDB,
                 autotest: AutoTestRunner):
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
            try:
                self._route_get()
            except Exception as exc:  # noqa: BLE001 - last-resort safety net
                self._send_unexpected_error(exc)

        def _route_get(self):
            # Strip the query string (e.g. "?t=..." cache-busters) before routing —
            # otherwise it gets treated as part of the filename in /plot,/report,/data.
            path = urlsplit(self.path).path
            if path == "/" or path.startswith("/index"):
                self._send(200, pages.comparison_page(cfg))
            elif path == "/single":
                self._send(200, pages.single_page(cfg))
            elif path == "/auto":
                self._send(200, pages.auto_page(cfg))
            elif path == "/api/auto/status":
                try:
                    since = int(parse_qs(urlsplit(self.path).query).get("since", ["0"])[0])
                except (ValueError, TypeError):
                    since = 0
                self._send_json(200, autotest.status(max(since, 0)))
            elif path == "/journal":
                self._send(200, pages.journal_page())
            elif path == "/api/journal":
                self._send_json(200, db.recent())
            elif path == "/api/hardware":
                self._send_json(200, device.status())
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
            try:
                self._route_post()
            except Exception as exc:  # noqa: BLE001 - last-resort safety net
                self._send_unexpected_error(exc)

        def _route_post(self):
            # Same urlsplit as _route_get — a query string on a POST would
            # otherwise miss every route and 404.
            path = urlsplit(self.path).path
            # These would measure the generator signal or reopen ports mid-run.
            if path in _BLOCKED_DURING_AUTO and autotest.running:
                self._send_json(409, {"error": "auto test running \u2014 stop it first"})
                return
            if path == "/api/sweep":
                self._handle_sweep()
            elif path == "/api/report":
                self._handle_report()
            elif path == "/api/single/report":
                self._handle_single_report()
            elif path == "/api/journal/delete":
                self._handle_journal_delete()
            elif path == "/api/hardware/mode":
                self._handle_set_mode()
            elif path == "/api/hardware/probe":
                self._handle_probe()
            elif path == "/api/switcher":
                self._handle_switcher()
            elif path == "/api/auto/start":
                self._handle_auto_start()
            elif path == "/api/auto/stop":
                self._send_json(200, autotest.stop())
            elif path == "/api/auto/report":
                self._handle_auto_report()
            else:
                self.send_response(404)
                self.end_headers()

        def _send_unexpected_error(self, exc: Exception) -> None:
            # A route handler raised something we didn't anticipate (e.g. a raw
            # serial I/O error escaping the device layer). Without this, the
            # connection would be dropped with zero bytes of response, which
            # browsers report as a generic "Failed to fetch" with no detail.
            logger.exception("unhandled error handling %s", self.path)
            try:
                self._send_json(500, {"error": f"internal error: {exc}"})
            except Exception:
                pass  # response likely already partially sent; nothing more we can do

        # -- handlers ------------------------------------------------------
        def _handle_set_mode(self):
            body = self._read_json()
            mode = str(body.get("mode", ""))
            if mode not in DEVICE_MODES:
                self._send_json(400, {"error": f"unknown mode {mode!r}"})
                return
            device.set_mode(mode)
            logger.info("device mode -> %s", mode)
            self._send_json(200, device.status())

        def _handle_probe(self):
            """Re-resolve every port without changing the mode."""
            device.set_mode(cfg.mode())
            self._send_json(200, device.status())

        def _handle_switcher(self):
            body = self._read_json()
            action = str(body.get("action", ""))
            try:
                reply = device.switcher_command(action)
            except DeviceError as exc:
                self._send_json(502, {"error": str(exc)})
                return
            self._send_json(200, {"reply": reply, "hardware": device.status()})

        def _handle_sweep(self):
            try:
                body = self._read_json()
                block = body.get("block")
                # "S" is the single-test page's slot, kept apart from A/B so a
                # single run never clobbers a comparison in progress.
                if block not in ("A", "B", "S"):
                    self._send_json(400, {"error": "block must be A, B or S"})
                    return
                start = float(body["start"])
                stop = float(body["stop"])
                step = float(body["step"])
                conditions = str(body.get("conditions", ""))
                if not (0 < step) or not (start < stop):
                    self._send_json(400, {"error": "invalid start/stop/step"})
                    return
                # The topology's real ceiling — 6200 MHz without the shifter,
                # 12200 with it. Previously unchecked, so 12000 could be typed
                # into a plain single-device bench and come back as garbage.
                if stop > cfg.max_stop_mhz():
                    self._send_json(400, {"error":
                        f"mode '{cfg.mode_label()}' measures up to "
                        f"{cfg.max_stop_mhz():.0f} MHz"})
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
            setup = cfg.mode_label()
            label = "Device" if block == "S" else f"Block {block}"
            title = f"{label} — {conditions or storage.utc_now_str()}"
            png_name = plotting.spectrum_png(
                cfg, stamp, block, sweep.freq_mhz, sweep.power_dbm, title,
                mark_peak=(block == "S"),
            )

            with state_lock:
                current[block] = {
                    "start": start, "stop": stop, "step": step,
                    "conditions": conditions,
                    "freq_mhz": sweep.freq_mhz, "power_dbm": sweep.power_dbm,
                    "npz": npz_name, "png": png_name,
                    "peak_dbm": peak_dbm, "peak_freq": peak_freq,
                    "n_points": int(sweep.freq_mhz.size),
                    "device_mode": cfg.mode(), "setup": setup,
                    "bands": json.dumps(sweep.bands),
                }

            self._send_json(200, {
                "png_url": f"/plot/{png_name}",
                "peak_dbm": peak_dbm,
                "peak_freq": peak_freq,
                "n_points": int(sweep.freq_mhz.size),
                "setup": setup,
                "bands": sweep.bands,
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
            # One place where a block's in-memory fields map to the suffixed
            # column/meta names, so the report and the journal row can never
            # drift apart as fields are added.
            record = {"ts_utc": ts_utc}
            for suffix, rec in (("a", rec_a), ("b", rec_b)):
                record.update({
                    f"start_{suffix}": rec["start"],
                    f"stop_{suffix}": rec["stop"],
                    f"step_{suffix}": rec["step"],
                    f"conditions_{suffix}": rec["conditions"],
                    f"npy_{suffix}": rec["npz"],
                    f"png_{suffix}": rec["png"],
                    f"peak_dbm_{suffix}": rec["peak_dbm"],
                    f"peak_freq_{suffix}": rec["peak_freq"],
                    f"n_points_{suffix}": rec["n_points"],
                    f"device_mode_{suffix}": rec["device_mode"],
                    f"bands_{suffix}": rec["bands"],
                })

            meta = dict(record, setup_a=rec_a["setup"], setup_b=rec_b["setup"])
            pdf_name = plotting.comparison_pdf(
                cfg, stamp,
                rec_a["freq_mhz"], rec_a["power_dbm"],
                rec_b["freq_mhz"], rec_b["power_dbm"],
                meta,
            )

            comparison_id = db.insert(dict(record, pdf_path=pdf_name))

            self._send_json(200, {"pdf_url": f"/report/{pdf_name}", "comparison_id": comparison_id})

        def _handle_single_report(self):
            with state_lock:
                rec = current.get("S")
            if rec is None:
                self._send_json(400, {"error": "run the test first"})
                return

            stamp = storage.make_stamp()
            record = {"ts_utc": storage.utc_now_str(), "kind": "single"}
            record.update({
                "start_a": rec["start"], "stop_a": rec["stop"], "step_a": rec["step"],
                "conditions_a": rec["conditions"], "npy_a": rec["npz"], "png_a": rec["png"],
                "peak_dbm_a": rec["peak_dbm"], "peak_freq_a": rec["peak_freq"],
                "n_points_a": rec["n_points"], "device_mode_a": rec["device_mode"],
                "bands_a": rec["bands"],
            })
            pdf_name = plotting.single_pdf(
                cfg, stamp, rec["freq_mhz"], rec["power_dbm"],
                dict(record, setup_a=rec["setup"]),
            )
            comparison_id = db.insert(dict(record, pdf_path=pdf_name))
            self._send_json(200, {"pdf_url": f"/report/{pdf_name}", "comparison_id": comparison_id})

        def _handle_auto_start(self):
            try:
                body = self._read_json()
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                status = autotest.start(body)
            except ValueError as exc:  # includes json.JSONDecodeError
                self._send_json(400, {"error": f"bad request: {exc}"})
                return
            except RuntimeError as exc:
                code = 409 if "already running" in str(exc) else 502
                self._send_json(code, {"error": str(exc)})
                return
            self._send_json(200, status)

        def _handle_auto_report(self):
            if autotest.running:
                self._send_json(409, {"error": "auto test running \u2014 stop it first"})
                return
            res = autotest.result()
            if res is None:
                self._send_json(400, {"error": "run the auto test first"})
                return

            p = res["params"]
            level = [float(x) for x in res["level"]]
            gen_freq = [float(x) for x in res["gen_freq"]]
            expected = float(p.expected_dbm)
            worst = None
            for i, lv in enumerate(level):
                if math.isfinite(lv) and (
                        worst is None or abs(lv - expected) > abs(level[worst] - expected)):
                    worst = i
            record = {
                "ts_utc": storage.utc_now_str(), "kind": "auto",
                "start_a": float(p.start), "stop_a": float(p.stop), "step_a": float(p.step),
                "conditions_a": p.conditions,
                "npy_a": res["npz"], "png_a": res["png"],
                "n_points_a": len(level),
                "device_mode_a": res["device_mode"],
                "gen_level_dbm": float(p.level_dbm),
                "window_mhz": float(p.window),
                "tolerance_db": float(p.tolerance),
                "n_pass": int(res["n_pass"]), "n_fail": int(res["n_fail"]),
                "run_state": res["state"],
                "peak_dbm_a": level[worst] if worst is not None else None,
                "peak_freq_a": gen_freq[worst] if worst is not None else None,
                "bands_a": json.dumps([]),
            }
            meta = dict(record, analyzer_step_mhz=float(p.analyzer_step),
                        cable_loss_db=float(p.cable_loss), expected_dbm=expected,
                        setup_a=res["setup"], n_planned=int(res["n_planned"]),
                        run_error=res["error"])
            pdf_name = plotting.auto_pdf(
                cfg, storage.make_stamp(), res["gen_freq"], res["level"],
                res["peak_freq"], res["passed"], meta)
            comparison_id = db.insert(dict(record, pdf_path=pdf_name))
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


def serve(cfg: Config, device: DeviceManager, db: JournalDB,
          autotest: AutoTestRunner | None = None) -> ThreadingHTTPServer:
    if autotest is None:
        autotest = AutoTestRunner(cfg, device)
    httpd = ThreadingHTTPServer((cfg.http_host, cfg.http_port),
                                make_handler(cfg, device, db, autotest))
    logger.info("test bench server on http://%s:%d", cfg.http_host, cfg.http_port)
    return httpd
