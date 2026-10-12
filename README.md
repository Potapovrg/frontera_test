# Frontera Test Bench

Web app for running comparison tests on the Arinst SA6 / Frontera spectrum
analyzer, attached to an OrangePi Zero 3 as `/dev/ttyACM0`.

- **Comparison Test** (`/`) — a bench-configuration panel (see below) plus two
  identical sweep blocks (Block A "before", Block B "after" a hardware change),
  each with start/stop/step frequency, an editable test-conditions note, and a
  spectrum plot. Once both blocks have a result, **Generate PDF report** builds
  a white-background PDF with both spectra, an overlay, and a difference (B−A)
  plot.
- **Test Journal** (`/journal`) — every generated report is logged to
  SQLite (`results/journal.db`) with the bench setup used for each block and
  links to the binary sweep data (`.npz`), plot PNGs, and the PDF.

## Bench configurations (up to 12.2 GHz)

The Frontera itself tunes 100–6200 MHz (6200 is the measured top bin, as in
frontera_ml's `SDRConfig.HW_MAX_MHZ`; it is uncalibrated above the spec'd 6000).
The product's frequency-shifter module (STM32 + MAX2870) down-converts an RF
input by 6000 MHz, so the high band is measured as an ordinary native sweep
whose frequencies are re-labelled — **the analyzer is never commanded above
6200 MHz**. The shifted band is floored at 6200 MHz (not 6100) so it never
overlaps the low band; coverage is 100–12200 MHz.

Three topologies, picked from the dropdown at the top of the Comparison Test
page (no restart needed; `--device-mode` only sets the starting value):

| Mode | Hardware | Coverage |
|---|---|---|
| `1_device_low_range` | 1 Frontera | 100–6200 MHz, shifter untouched |
| `1_device_full_range` | 1 Frontera behind the shifter | two passes: `shift off` → low band, `shift on` → high band |
| `2_device` | frontera1 direct + frontera2 behind the shifter | low and high band, shifter permanently on |

Both 12 GHz modes produce **one stitched trace** (one `.npz`, one plot, the
usual report). With the standard +6000 MHz offset the two bands meet at 6200 MHz
(that one bin is measured twice) and there is no gap. If `--offset2-mhz` is
set so that a real hole opens between the bands, the trace carries one NaN
sample there so the gap shows as a break, and a range entirely inside it is rejected.

The panel also has shifter diagnostics (`ping` / `status` / `shift on` /
`shift off` / `reset`) for testing the shifter module on its own.

### USB port assignment

All three units report the same USB identity (VID:PID `0483:5740`; both
Fronteras even share serial `00000000029A`), so ports are resolved as:
explicit CLI flag → udev symlink → behavioural probe (the shifter answers
`ping` with `0xAC1B3C55`, a Frontera answers a `scn20` probe). The shifter is
always resolved first so the SDR probe never fires `scn20` at the STM32.

For `2_device` the symlinks matter: without them the two Fronteras are
indistinguishable and the low/high assignment is arbitrary. Create them once
with the udev script from the sibling project (works for a direct port; behind
a USB hub, pass `--device-port` / `--device-port2` explicitly instead):

```bash
bash /home/test/Project_Frontera/frontera_ml/deploy/orangepi/setup_frontera_usb_ports.sh
```

The panel warns when it had to fall back to probing with two Fronteras in play.

## Run locally (no hardware)

```bash
python3 app.py --mock --port 8080
```

Open `http://localhost:8080/`.

## Run on the OrangePi (real device)

```bash
python3 app.py --port 8080                       # device_port defaults to /dev/ttyACM0
python3 app.py --port 8080 --device-mode 2_device  # start in the two-device topology
```

Missing hardware is no longer fatal at startup — the server comes up and the
bench panel reports what could not be found, so the operator can fix the
cabling or pick a different mode without restarting.

The Frontera can re-enumerate under a different node after a USB
disconnect/reset (e.g. `/dev/ttyACM0` -> `/dev/ttyACM1`). If `--device-port`
fails to open, `DeviceManager` automatically falls back to probing all
serial ports (`find_sa6()`) and switches to whatever port responds, both at
startup and on reconnect after a failed sweep — no manual `--device-port`
update needed.

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
  config.py             Config dataclass (topology, ports, paths, sweep defaults)
  hardware.py           USB port resolution (symlink -> probe) + topology report
  device.py             band planning, lock-guarded passes, stitching, --mock spectra
  switcher.py           vendored frequency-shifter driver (STM32/MAX2870)
  db.py                 JournalDB — thread-safe SQLite wrapper (comparisons table)
  storage.py             binary sweep persistence (.npz) + safe file-path helper
  plotting.py            matplotlib spectrum PNG + comparison PDF (white background)
  server.py              ThreadingHTTPServer routes
  pages.py               inline HTML/CSS/JS for both pages
frontera_interface.py    vendored SA6/Frontera driver (see /home/test/frontera_ml for origin)
tests/test_bands.py      band-plan / stitching unit tests (pytest, no hardware)
run_scan.py              simple one-shot scan CLI (smoke test)
deploy.py                paramiko SFTP deploy + remote run/detach
```

`switcher.py` and `frontera_interface.py` are vendored copies of
`frontera_ml/frontera_ml/sdr/` — make changes upstream and re-copy.

Results (SQLite DB, `.npz` sweep data, PNG plots, PDF reports) are written
to `results/` at runtime and are not committed to git.
