# Frontera Test Bench

Web app for running comparison tests on the Arinst SA6 / Frontera spectrum
analyzer, attached to an OrangePi Zero 3 as `/dev/ttyACM0`.

- **Comparison Test** (`/`) — two identical sweep blocks (Block A "before",
  Block B "after" a hardware change), each with start/stop/step frequency,
  an editable test-conditions note, and a spectrum plot. Once both blocks
  have a result, **Generate PDF report** builds a white-background PDF with
  both spectra, an overlay, and a difference (B−A) plot.
- **Test Journal** (`/journal`) — every generated report is logged to
  SQLite (`results/journal.db`) with links to the binary sweep data
  (`.npz`), plot PNGs, and the PDF.

## Run locally (no hardware)

```bash
python3 app.py --mock --port 8080
```

Open `http://localhost:8080/`.

## Run on the OrangePi (real device)

```bash
python3 app.py --port 8080                    # device_port defaults to /dev/ttyACM0
```

## Deploy from WSL

```bash
python3 deploy.py --run "python3 app.py --port 8080" --detach   # upload + start in background
python3 deploy.py --no-run                                       # upload only
```

`--detach` starts the server with `nohup` so it survives SSH disconnect;
output goes to `server.log` in the remote project directory. Then browse to
`http://192.168.10.2:8080/`.

## Layout

```
app.py                 entry point (argparse, wires device/db/server)
testbench/
  config.py             Config dataclass (ports, paths, sweep defaults)
  device.py             DeviceManager — lock-guarded sweeps, reconnect, --mock synth spectra
  db.py                 JournalDB — thread-safe SQLite wrapper (comparisons table)
  storage.py             binary sweep persistence (.npz) + safe file-path helper
  plotting.py            matplotlib spectrum PNG + comparison PDF (white background)
  server.py              ThreadingHTTPServer routes
  pages.py               inline HTML/CSS/JS for both pages
frontera_interface.py    vendored SA6/Frontera driver (see /home/test/frontera_ml for origin)
run_scan.py              simple one-shot scan CLI (smoke test)
deploy.py                paramiko SFTP deploy + remote run/detach
```

Results (SQLite DB, `.npz` sweep data, PNG plots, PDF reports) are written
to `results/` at runtime and are not committed to git.
