"""Inline HTML/CSS/JS for the two pages this app serves.

Same approach as frontera_ml/persistence/log_server.py: no template engine,
no static assets folder — pages are Python string constants, dark themed,
vanilla JS + fetch().
"""
from __future__ import annotations

import json

from .config import DEVICE_MODE_LABELS

_MODE_LABELS_JSON = json.dumps(DEVICE_MODE_LABELS, ensure_ascii=False)

_STYLE = """
body{background:#0d1117;color:#c9d1d9;font-family:monospace;margin:16px;max-width:1100px}
h1{color:#58a6ff;font-size:20px}
h2{font-size:15px;margin:0 0 8px 0}
a{color:#58a6ff}
.block{border:1px solid #30363d;border-radius:6px;padding:14px;margin-bottom:18px;background:#161b22}
.block.a h2{color:#1f6feb}
.block.b h2{color:#da3633}
.block.auto h2{color:#3fb950}
label{display:inline-block;min-width:80px;font-size:13px;color:#8b949e}
input[type=number]{width:90px;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;
  border-radius:4px;padding:3px 6px;margin:4px 8px 4px 0}
textarea{width:100%;height:54px;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;
  border-radius:4px;padding:6px;font-family:monospace;font-size:12px;box-sizing:border-box}
.row{margin-bottom:6px}
button{background:#238636;color:#fff;border:none;padding:6px 14px;border-radius:4px;
  cursor:pointer;font-family:monospace;font-size:13px;margin-top:6px}
button:disabled{background:#30363d;color:#6e7681;cursor:not-allowed}
button.report{background:#8250df}
button.danger{background:#da3633}
.status{font-size:12px;color:#8b949e;margin-top:6px;min-height:16px}
img.spectrum{max-width:100%;margin-top:10px;border:1px solid #30363d;border-radius:4px;
  background:#fff}
table{border-collapse:collapse;width:100%;margin-top:10px}
th,td{border:1px solid #30363d;padding:4px 8px;text-align:left;font-size:12px;vertical-align:top}
th{background:#161b22;color:#8b949e}
tr:nth-child(even){background:#161b22}
th.chk,td.chk{width:24px;text-align:center}
.cond{color:#8b949e;font-size:11px;margin-top:3px;white-space:pre-wrap;max-width:280px}
nav{margin-bottom:14px;font-size:13px}
nav a{margin-right:14px}
.block.hw h2{color:#d29922}
select{background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:4px;
  padding:3px 6px;font-family:monospace;font-size:13px;margin:4px 8px 4px 0;min-width:320px}
button.ghost{background:#21262d;border:1px solid #30363d;color:#c9d1d9;margin-right:6px}
button.small{padding:3px 10px;font-size:12px;margin-right:6px}
.ports{font-size:12px;color:#8b949e;margin:8px 0 2px 0;line-height:1.7}
.ports .unit{display:inline-block;min-width:92px;padding-right:8px;color:#c9d1d9}
.ports .src{color:#6e7681}
.ok{color:#3fb950}
.bad{color:#f85149}
.warn{color:#d29922;font-size:12px;margin-top:6px;white-space:pre-wrap}
"""

_NAV = """<nav><a href="/">Comparison Test</a><a href="/single">Single Test</a><a href="/auto">Auto Test</a><a href="/journal">Test Journal</a></nav>"""


_HARDWARE_HTML = """
<div class="block hw" id="hw">
  <h2>Bench configuration</h2>
  <div class="row">
    <label>Mode</label>
    <select id="hw_mode"></select>
    <button class="ghost small" id="hw_probe">Detect hardware</button>
  </div>
  <div class="ports" id="hw_ports"></div>
  <div class="warn" id="hw_warn"></div>
  <div class="row" id="sw_panel" style="margin-top:10px">
    <label>Shifter</label>
    <button class="ghost small" data-sw="ping">ping</button>
    <button class="ghost small" data-sw="status">status</button>
    <button class="ghost small" data-sw="shift_on">shift on</button>
    <button class="ghost small" data-sw="shift_off">shift off</button>
    <button class="ghost small" data-sw="reset">reset</button>
    <span class="status" id="sw_reply" style="display:inline-block;margin-left:6px"></span>
  </div>
</div>
"""


def _block_html(block: str, title: str, cfg, heading: str = "") -> str:
    return f"""
<div class="block {block.lower()}" id="block_{block}">
  <h2>{heading or 'Block ' + block} — {title}</h2>
  <div class="row">
    <label>Start (MHz)</label>
    <input type=number step=any id="{block}_start" value={cfg.default_start_mhz:g} class=param>
    <label>Stop (MHz)</label>
    <input type=number step=any id="{block}_stop" value={cfg.default_stop_mhz:g} class=param>
    <label>Step (MHz)</label>
    <input type=number step=any id="{block}_step" value={cfg.default_step_mhz:g} class=param>
  </div>
  <div class="row">
    <label style="vertical-align:top">Conditions</label>
    <textarea id="{block}_conditions"></textarea>
  </div>
  <button id="{block}_run">Start test</button>
  <div class="status" id="{block}_status"></div>
  <img class="spectrum" id="{block}_img" style="display:none">
</div>
"""


# Shared by both pages' scripts.
_COMMON_JS = """
function escapeHtml(s){
  const div = document.createElement('div');
  div.textContent = s ?? '';
  return div.innerHTML;
}

// Handlers answer errors as {"error": "..."} JSON; fall back to the raw body
// so an unexpected non-JSON 500 still says something useful.
async function errorText(r){
  const raw = await r.text();
  try { return JSON.parse(raw).error || raw; } catch(e) { return raw; }
}
"""

_HARDWARE_JS = """
let HW = null;   // latest /api/hardware payload
// -- hardware panel ---------------------------------------------------------
// Seeded with the value the stop inputs are rendered with, so the very first
// render can already retune it if the bench started in a 12 GHz mode.
let lastMax = __DEFAULT_STOP__;
const UNIT_NAMES = {frontera1:'Frontera 1', frontera2:'Frontera 2', switcher:'Shifter',
                    generator:'Generator (Arinst TG R3)'};
const SOURCE_NAMES = {explicit:'set manually', symlink:'udev symlink', probe:'auto-probe',
                      mock:'mock', none:'not found', disabled:'not used'};

function renderHardware(){
  if (!HW) return;
  const sel = document.getElementById('hw_mode');
  if (!sel.options.length){
    for (const m of HW.modes){
      const o = document.createElement('option');
      o.value = m.value; o.textContent = m.label;
      sel.appendChild(o);
    }
  }
  sel.value = HW.mode;

  const lines = [];
  const keys = ['frontera1','frontera2','switcher'];
  if (SHOW_GENERATOR) keys.push('generator');
  for (const key of keys){
    const p = (HW.ports && HW.ports[key]) || (key === 'generator' ? null : {});
    if (!p) continue;
    if (p.source === 'disabled') continue;
    const ok = !!p.port;
    lines.push(`<div><span class="unit">${UNIT_NAMES[key]}</span>` +
      `<span class="${ok ? 'ok' : 'bad'}">${escapeHtml(p.port || '— not found')}</span>` +
      `<span class="src"> (${SOURCE_NAMES[p.source] || p.source}` +
      `${ok ? (p.open ? ', open' : ', closed') : ''})</span></div>`);
  }
  const gap = HW.band_gap;
  lines.push(`<div class="src">Range up to ${HW.max_stop_mhz.toFixed(0)} MHz` +
    (HW.switcher_enabled && gap ? `, gap between bands ${gap[0].toFixed(0)}–${gap[1].toFixed(0)} MHz` : '') +
    (HW.mock ? ' — MOCK, no hardware' : '') + '</div>');
  document.getElementById('hw_ports').innerHTML = lines.join('');

  const warn = (HW.error ? ['ERROR: ' + HW.error] : []).concat(HW.warnings || []);
  document.getElementById('hw_warn').textContent = warn.join('\\n');

  document.getElementById('sw_panel').style.display = HW.switcher_enabled ? '' : 'none';

  onHardware(HW);
  lastMax = HW.max_stop_mhz;
}

async function loadHardware(){
  try {
    const r = await fetch('/api/hardware');
    HW = await r.json();
    renderHardware();
  } catch(e) {
    document.getElementById('hw_warn').textContent = 'Could not get hardware status: ' + e.message;
  }
}

async function hardwarePost(url, body, btn){
  const warnEl = document.getElementById('hw_warn');
  if (btn) btn.disabled = true;
  warnEl.textContent = 'Probing hardware...';
  try {
    const r = await fetch(url, {method:'POST', body: JSON.stringify(body || {})});
    if (!r.ok) throw new Error(await errorText(r));
    HW = await r.json();
    renderHardware();
  } catch(e) {
    warnEl.textContent = 'ERROR: ' + e.message;
  } finally {
    if (btn) btn.disabled = false;
  }
}

document.getElementById('hw_mode').addEventListener('change', (e) => {
  hardwarePost('/api/hardware/mode', {mode: e.target.value}, e.target);
});
document.getElementById('hw_probe').addEventListener('click', (e) => {
  hardwarePost('/api/hardware/probe', {}, e.target);
});

for (const btn of document.querySelectorAll('#sw_panel button[data-sw]')){
  btn.addEventListener('click', async () => {
    const out = document.getElementById('sw_reply');
    btn.disabled = true;
    out.textContent = '...';
    try {
      const r = await fetch('/api/switcher',
        {method:'POST', body: JSON.stringify({action: btn.dataset.sw})});
      if (!r.ok) throw new Error(await errorText(r));
      const j = await r.json();
      out.textContent = j.reply;
      HW = j.hardware;
      renderHardware();
    } catch(e) {
      out.textContent = 'ERROR: ' + e.message;
    } finally {
      btn.disabled = false;
    }
  });
}

loadHardware();
"""

_SCRIPT = """
<script>
""" + _COMMON_JS + """
const BLOCKS = __BLOCKS__;
const SINGLE = __SINGLE__;
const REPORT_URL = '__REPORT_URL__';
const SHOW_GENERATOR = false;
const state = {};
for (const b of BLOCKS) state[b] = {edited:false, done:false};

function todayStr(){
  const d = new Date();
  const pad = n => String(n).padStart(2, '0');
  return `${pad(d.getUTCDate())}.${pad(d.getUTCMonth()+1)}.${d.getUTCFullYear()} ` +
         `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} UTC`;
}

function defaultConditions(block){
  const start = document.getElementById(block+'_start').value;
  const stop  = document.getElementById(block+'_stop').value;
  const step  = document.getElementById(block+'_step').value;
  const setup = HW ? `; bench: ${HW.mode_label}` : '';
  return `${todayStr()}; Sweep ${start}-${stop} MHz, step ${step} MHz${setup}`;
}

function onHardware(hw){
  // Retune the frequency inputs to what this topology can actually reach.
  // The stop value follows the ceiling only while it still sits on the
  // previous ceiling — i.e. the operator hasn't typed their own span — so
  // switching to a 12 GHz setup gives a 12 GHz sweep without clobbering a
  // deliberately narrowed range. Above the new ceiling it is always clamped.
  for (const block of BLOCKS){
    const stopEl = document.getElementById(block+'_stop');
    const value = parseFloat(stopEl.value);
    stopEl.max = hw.max_stop_mhz;
    if (value === lastMax || value > hw.max_stop_mhz) stopEl.value = hw.max_stop_mhz;
    refreshDefault(block);
  }
}

""" + _HARDWARE_JS + """
function refreshDefault(block){
  if (state[block].edited) return;
  document.getElementById(block+'_conditions').value = defaultConditions(block);
}

for (const block of BLOCKS){
  document.getElementById(block+'_conditions').addEventListener('input', () => {
    state[block].edited = true;
  });
  for (const f of ['start','stop','step']){
    document.getElementById(block+'_'+f).addEventListener('input', () => refreshDefault(block));
  }
  refreshDefault(block);

  document.getElementById(block+'_run').addEventListener('click', async () => {
    const statusEl = document.getElementById(block+'_status');
    const runBtn = document.getElementById(block+'_run');
    runBtn.disabled = true;
    statusEl.textContent = 'Sweeping...';
    try {
      const body = {
        block,
        start: parseFloat(document.getElementById(block+'_start').value),
        stop: parseFloat(document.getElementById(block+'_stop').value),
        step: parseFloat(document.getElementById(block+'_step').value),
        conditions: document.getElementById(block+'_conditions').value,
        single: SINGLE,
      };
      const r = await fetch('/api/sweep', {method:'POST', body: JSON.stringify(body)});
      if (!r.ok) throw new Error(await errorText(r));
      const j = await r.json();
      const img = document.getElementById(block+'_img');
      img.src = j.png_url + '?t=' + Date.now();
      img.style.display = 'block';
      statusEl.textContent = `OK: ${j.n_points} points, peak ${j.peak_dbm.toFixed(1)} dBm @ ${j.peak_freq.toFixed(1)} MHz`;
      state[block].done = true;
      updateReportButton();
    } catch(e) {
      statusEl.textContent = 'ERROR: ' + e.message;
    } finally {
      runBtn.disabled = false;
    }
  });
}

function updateReportButton(){
  document.getElementById('report_btn').disabled = !BLOCKS.every(b => state[b].done);
}

document.getElementById('report_btn').addEventListener('click', async () => {
  const statusEl = document.getElementById('report_status');
  const btn = document.getElementById('report_btn');
  btn.disabled = true;
  statusEl.textContent = 'Generating PDF...';
  try {
    const r = await fetch(REPORT_URL, {method:'POST'});
    if (!r.ok) throw new Error(await errorText(r));
    const j = await r.json();
    statusEl.innerHTML = `Report ready: <a href="${j.pdf_url}" target=_blank>open PDF</a> ` +
      `(journal entry #${j.comparison_id}, see <a href="/journal">Test Journal</a>)`;
  } catch(e) {
    statusEl.textContent = 'ERROR: ' + e.message;
  } finally {
    updateReportButton();
  }
});
</script>
"""

def _script(cfg, blocks: str, single: bool, report_url: str) -> str:
    # The default stop doubles as "the operator hasn't customised this yet",
    # which is what lets a mode switch retune it (see _SCRIPT's lastMax).
    return (_SCRIPT.replace("__DEFAULT_STOP__", f"{cfg.default_stop_mhz:g}")
            .replace("__BLOCKS__", blocks)
            .replace("__SINGLE__", "true" if single else "false")
            .replace("__REPORT_URL__", report_url))


def single_page(cfg) -> str:
    script = _script(cfg, "['S']", True, "/api/single/report")
    return f"""<!doctype html><html><head><meta charset=utf-8>
<title>Frontera — Single Test</title>
<style>{_STYLE}</style></head><body>
{_NAV}
<h1>Single Test</h1>
{_HARDWARE_HTML}
{_block_html('S', 'single device', cfg, heading='Device')}
<button class="report" id="report_btn" disabled>Generate PDF report</button>
<div class="status" id="report_status"></div>
{script}
</body></html>"""


def comparison_page(cfg) -> str:
    script = _script(cfg, "['A','B']", False, "/api/report")
    return f"""<!doctype html><html><head><meta charset=utf-8>
<title>Frontera — Comparison Test</title>
<style>{_STYLE}</style></head><body>
{_NAV}
<h1>Comparison Test</h1>
{_HARDWARE_HTML}
{_block_html('A', 'before hardware change', cfg)}
{_block_html('B', 'after hardware change', cfg)}
<button class="report" id="report_btn" disabled>Generate PDF report</button>
<div class="status" id="report_status"></div>
{script}
</body></html>"""

_AUTO_HTML = """
<div class="block auto" id="block_auto">
  <h2>Generator sweep — Arinst TG R3 → Frontera</h2>
  <div class="row">
    <label>Generator</label>
    <label style="min-width:0">Start (MHz)</label>
    <input type=number step=any id="auto_start" value={start:g} class=param>
    <label style="min-width:0">Stop (MHz)</label>
    <input type=number step=any id="auto_stopf" value={stop:g} class=param>
    <label style="min-width:0">Step (MHz)</label>
    <input type=number step=any id="auto_step" value=1 class=param>
    <label style="min-width:0">Level (dBm)</label>
    <select id="auto_level" class=param style="min-width:0">
      <option value="-8">-8</option><option value="-11">-11</option>
      <option value="-14" selected>-14</option><option value="-17">-17</option>
      <option value="-20">-20</option><option value="-23">-23</option>
      <option value="-26">-26</option><option value="-29">-29</option>
    </select>
  </div>
  <div class="row">
    <label>Measurement</label>
    <label style="min-width:0">Window (MHz)</label>
    <input type=number step=any id="auto_window" value=100 class=param
      title="total width of the Frontera sweep centred on the generator frequency">
    <label style="min-width:0">Analyzer step (MHz)</label>
    <input type=number step=any id="auto_astep" value={astep:g} class=param>
  </div>
  <div class="row">
    <label>Check</label>
    <label style="min-width:0">Cable loss (dB)</label>
    <input type=number step=any id="auto_loss" value=0 class=param>
    <label style="min-width:0">Tolerance (± dB)</label>
    <input type=number step=any id="auto_tol" value=3 class=param>
    <span id="auto_expected" class="status" style="display:inline-block"></span>
  </div>
  <div class="row">
    <label style="vertical-align:top">Conditions</label>
    <textarea id="auto_conditions"></textarea>
  </div>
  <button id="auto_run">Start auto test</button>
  <button id="auto_stop" class="danger" disabled>Stop</button>
  <div class="status" id="auto_status"></div>
  <canvas id="auto_canvas" style="width:100%;height:360px;background:#fff;border:1px solid #30363d;border-radius:4px;margin-top:10px"></canvas>
  <img class="spectrum" id="auto_img" style="display:none">
</div>
"""

_AUTO_PRE_JS = """
const SHOW_GENERATOR = true;
const GEN_MAX_MHZ = 12000;
const REPORT_URL = '/api/auto/report';
"""

_AUTO_JS = """
const $ = id => document.getElementById(id);
const PARAM_IDS = ['auto_start','auto_stopf','auto_step','auto_level','auto_window',
                   'auto_astep','auto_loss','auto_tol'];
let condEdited = false;
let points = [];        // accumulated {f, level, peak_f, pass}
let nextIdx = 0;
let last = null;        // last status payload
let pollTimer = null;
let wasRunning = false;
let runId = null;       // server run_id the accumulated points belong to
let polling = false;    // one status request at a time, or points get appended twice

function todayStr(){
  const d = new Date();
  const pad = n => String(n).padStart(2, '0');
  return `${pad(d.getUTCDate())}.${pad(d.getUTCMonth()+1)}.${d.getUTCFullYear()} ` +
         `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} UTC`;
}
const num = id => parseFloat($(id).value);
const fmt = (v, d) => (v < 0 ? '\\u2212' : '') + Math.abs(v).toFixed(d);

function expectedNow(){ return num('auto_level') - num('auto_loss'); }

function refreshExpected(){
  const e = expectedNow(), t = num('auto_tol');
  $('auto_expected').textContent = (isFinite(e) && isFinite(t))
    ? `expected ${fmt(e,1)} dBm \\u00b1 ${t.toFixed(1)} dB` : '';
}

function defaultConditions(){
  const setup = HW ? `; bench: ${HW.mode_label}` : '';
  return `${todayStr()}; Gen ${$('auto_start').value}-${$('auto_stopf').value} MHz ` +
    `step ${$('auto_step').value} MHz @ ${$('auto_level').value} dBm; ` +
    `window ${$('auto_window').value} MHz; tol \\u00b1${$('auto_tol').value} dB${setup}`;
}

function refreshDefault(){
  if (!condEdited) $('auto_conditions').value = defaultConditions();
}

function onHardware(hw){
  // Same retune logic as the block pages, capped at the generator's maximum.
  const stopEl = $('auto_stopf');
  const cap = Math.min(hw.max_stop_mhz, GEN_MAX_MHZ);
  const value = parseFloat(stopEl.value);
  stopEl.max = cap;
  if (value === Math.min(lastMax, GEN_MAX_MHZ) || value > cap) stopEl.value = cap;
  refreshDefault();
}

$('auto_conditions').addEventListener('input', () => { condEdited = true; });
for (const id of PARAM_IDS){
  $(id).addEventListener('input', () => { refreshExpected(); refreshDefault(); redraw(); });
  $(id).addEventListener('change', () => { refreshExpected(); refreshDefault(); redraw(); });
}
refreshExpected();
refreshDefault();

// -- running state / locking ---------------------------------------------------
function isRunning(){ return !!last && last.state === 'running'; }

function applyLock(){
  const run = isRunning();
  for (const id of PARAM_IDS) $(id).disabled = run;
  $('auto_conditions').disabled = run;
  $('auto_run').disabled = run;
  $('auto_stop').disabled = !run;
  $('hw_mode').disabled = run;
  $('hw_probe').disabled = run;
  for (const b of document.querySelectorAll('#sw_panel button[data-sw]')) b.disabled = run;
  const st = last ? last.state : 'idle';
  $('report_btn').disabled = !((st === 'done' || st === 'stopped' || st === 'error') && points.length > 0);
}

function showStatus(){
  const el = $('auto_status');
  if (!last || last.state === 'idle'){ el.textContent = ''; return; }
  const s = last.summary || {};
  const counts = `pass ${s.n_pass ?? 0}, fail ${s.n_fail ?? 0}`;
  const pct = last.n ? (100 * last.i / last.n).toFixed(1) : '0.0';
  const stats = (s.min != null && s.max != null && s.mean != null)
    ? `; min ${fmt(s.min,1)} / max ${fmt(s.max,1)} / mean ${fmt(s.mean,1)} dBm` : '';
  if (last.state === 'running'){
    const eta = last.eta_s == null ? '?' : Math.max(1, Math.round(last.eta_s / 60));
    el.textContent = `Running ${last.i}/${last.n} (${pct}%), ETA ${eta} min \\u2014 ${counts}`;
  } else if (last.state === 'done'){
    el.textContent = `Done: ${last.i}/${last.n} points \\u2014 ${counts}${stats}`;
  } else if (last.state === 'stopped'){
    el.textContent = `Stopped: ${last.i}/${last.n} points \\u2014 ${counts}${stats}`;
  } else if (last.state === 'error'){
    el.textContent = `ERROR: ${last.error || 'unknown'} (${last.i}/${last.n} points, ${counts})`;
  }
}

// -- plot ----------------------------------------------------------------------
function niceStep(range, target){
  const raw = range / target;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const r = raw / mag;
  return (r < 1.5 ? 1 : r < 3 ? 2 : r < 7 ? 5 : 10) * mag;
}

function redraw(){
  const cv = $('auto_canvas');
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  if (!W || !H) return;
  cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.fillStyle = '#fff'; g.fillRect(0, 0, W, H);

  const p = (last && last.params) || null;
  const x0 = p ? p.start : num('auto_start'), x1 = p ? p.stop : num('auto_stopf');
  let exp = last && last.expected_dbm != null ? last.expected_dbm : expectedNow();
  let tol = last && last.tolerance_db != null ? last.tolerance_db : num('auto_tol');
  if (!isFinite(exp)) exp = -14;
  if (!isFinite(tol)) tol = 3;
  let ymin = exp - tol, ymax = exp + tol;
  for (const pt of points){
    if (pt.level != null && isFinite(pt.level)){
      ymin = Math.min(ymin, pt.level); ymax = Math.max(ymax, pt.level);
    }
  }
  const pad = Math.max((ymax - ymin) * 0.08, 1);
  ymin -= pad; ymax += pad;
  const L = 56, R = 12, T = 12, B = 36;
  const pw = W - L - R, ph = H - T - B;
  const xr = (x1 > x0) ? (x1 - x0) : 1;
  const X = f => L + (f - x0) / xr * pw;
  const Y = v => T + (ymax - v) / (ymax - ymin) * ph;

  // tolerance band + expected line
  g.fillStyle = 'rgba(63,185,80,0.18)';
  g.fillRect(L, Y(exp + tol), pw, Y(exp - tol) - Y(exp + tol));
  g.strokeStyle = '#2da44e'; g.lineWidth = 1; g.setLineDash([6, 4]);
  g.beginPath(); g.moveTo(L, Y(exp)); g.lineTo(L + pw, Y(exp)); g.stroke();
  g.setLineDash([]);

  // axes, ticks, labels
  g.strokeStyle = '#888'; g.fillStyle = '#333'; g.font = '11px monospace';
  g.strokeRect(L, T, pw, ph);
  g.textAlign = 'center'; g.textBaseline = 'top';
  const xs = niceStep(xr, 8);
  for (let v = Math.ceil(x0 / xs) * xs; v <= x1 + 1e-9; v += xs){
    g.strokeStyle = '#e4e4e4'; g.beginPath(); g.moveTo(X(v), T); g.lineTo(X(v), T + ph); g.stroke();
    g.fillText(String(+v.toPrecision(7)), X(v), T + ph + 4);
  }
  g.textAlign = 'right'; g.textBaseline = 'middle';
  const ys = niceStep(ymax - ymin, 6);
  for (let v = Math.ceil(ymin / ys) * ys; v <= ymax + 1e-9; v += ys){
    g.strokeStyle = '#e4e4e4'; g.beginPath(); g.moveTo(L, Y(v)); g.lineTo(L + pw, Y(v)); g.stroke();
    g.fillText(String(+v.toPrecision(6)), L - 5, Y(v));
  }
  g.textAlign = 'center'; g.textBaseline = 'bottom';
  g.fillText('Generator frequency, MHz', L + pw / 2, H - 2);
  g.save(); g.translate(12, T + ph / 2); g.rotate(-Math.PI / 2);
  g.textBaseline = 'top'; g.fillText('Level, dBm', 0, 0); g.restore();

  // trace (break at missing values)
  g.strokeStyle = '#1f6feb'; g.lineWidth = 1.2; g.beginPath();
  let pen = false;
  for (const pt of points){
    if (pt.level == null || !isFinite(pt.level)){ pen = false; continue; }
    if (pen) g.lineTo(X(pt.f), Y(pt.level)); else { g.moveTo(X(pt.f), Y(pt.level)); pen = true; }
  }
  g.stroke();
  // failures
  for (const pt of points){
    if (pt.level != null && isFinite(pt.level)){
      if (!pt.pass){
        g.fillStyle = '#da3633'; g.beginPath(); g.arc(X(pt.f), Y(pt.level), 3, 0, 2 * Math.PI); g.fill();
      }
    } else {
      const x = X(pt.f), y = T + ph - 6;
      g.strokeStyle = '#da3633'; g.lineWidth = 1.5; g.beginPath();
      g.moveTo(x - 3, y - 3); g.lineTo(x + 3, y + 3); g.moveTo(x + 3, y - 3); g.lineTo(x - 3, y + 3);
      g.stroke();
    }
  }
}
window.addEventListener('resize', redraw);

// -- server polling ------------------------------------------------------------
function restoreParams(p){
  if (!p) return;
  const set = (id, v) => { if (v != null) $(id).value = v; };
  set('auto_start', p.start); set('auto_stopf', p.stop); set('auto_step', p.step);
  set('auto_level', p.level_dbm); set('auto_window', p.window);
  set('auto_astep', p.analyzer_step); set('auto_loss', p.cable_loss); set('auto_tol', p.tolerance);
  if (p.conditions != null){ $('auto_conditions').value = p.conditions; condEdited = true; }
  refreshExpected();
}

function showPng(){
  const img = $('auto_img');
  if (last && last.png_url && ['done', 'stopped', 'error'].includes(last.state)){
    img.src = last.png_url + '?t=' + Date.now();
    img.style.display = 'block';
  } else {
    img.style.display = 'none';
  }
}

function schedule(){
  clearTimeout(pollTimer);
  if (isRunning()) pollTimer = setTimeout(() => poll(), 1000);
}

async function poll(first){
  if (polling) return;
  polling = true;
  try { await pollOnce(first); } finally { polling = false; }
}

async function pollOnce(first){
  let since = nextIdx;
  for (let attempt = 0; attempt < 2; attempt++){
    let j;
    try {
      const r = await fetch('/api/auto/status?since=' + since);
      if (!r.ok) throw new Error(await errorText(r));
      j = await r.json();
    } catch(e) {
      if (isRunning()) schedule();   // transient: keep trying
      else $('auto_status').textContent = 'ERROR: ' + e.message;
      return;
    }
    // A new run replaced ours: start over from the first point.
    if (since > 0 && (j.run_id !== runId || j.next < points.length || (j.n && points.length > j.n))){
      points = []; nextIdx = 0; since = 0;
      continue;
    }
    if (first) restoreParams(j.params);
    runId = j.run_id;
    for (const pt of (j.points || [])) points.push(pt);
    nextIdx = j.next;
    const prevRunning = wasRunning;
    last = j;
    wasRunning = j.state === 'running';
    showStatus(); applyLock(); redraw();
    if (prevRunning && !wasRunning) showPng();
    else if (first) showPng();
    schedule();
    return;
  }
}

$('auto_run').addEventListener('click', async () => {
  $('auto_run').disabled = true;
  $('auto_status').textContent = 'Starting...';
  try {
    const body = {
      start: num('auto_start'), stop: num('auto_stopf'), step: num('auto_step'),
      level_dbm: parseFloat($('auto_level').value), window: num('auto_window'),
      analyzer_step: num('auto_astep'), cable_loss: num('auto_loss'), tolerance: num('auto_tol'),
      conditions: $('auto_conditions').value,
    };
    const r = await fetch('/api/auto/start', {method:'POST', body: JSON.stringify(body)});
    if (!r.ok) throw new Error(await errorText(r));
    const j = await r.json();
    points = []; nextIdx = 0; runId = j.run_id;
    $('auto_img').style.display = 'none';
    $('report_status').textContent = '';
    last = j; wasRunning = j.state === 'running';
    restoreParams(j.params);
    showStatus(); applyLock(); redraw();
    // pick up points from index 0 right away
    await poll();
  } catch(e) {
    $('auto_status').textContent = 'ERROR: ' + e.message;
    applyLock();
  }
});

$('auto_stop').addEventListener('click', async () => {
  $('auto_stop').disabled = true;
  try {
    const r = await fetch('/api/auto/stop', {method:'POST', body: '{}'});
    if (!r.ok) throw new Error(await errorText(r));
    await poll();
  } catch(e) {
    $('auto_status').textContent = 'ERROR: ' + e.message;
  }
});

$('report_btn').addEventListener('click', async () => {
  const statusEl = $('report_status');
  const btn = $('report_btn');
  btn.disabled = true;
  statusEl.textContent = 'Generating PDF...';
  try {
    const r = await fetch(REPORT_URL, {method:'POST'});
    if (!r.ok) throw new Error(await errorText(r));
    const j = await r.json();
    statusEl.innerHTML = `Report ready: <a href="${j.pdf_url}" target=_blank>open PDF</a> ` +
      `(journal entry #${j.comparison_id}, see <a href="/journal">Test Journal</a>)`;
  } catch(e) {
    statusEl.textContent = 'ERROR: ' + e.message;
  } finally {
    applyLock();
  }
});

poll(true);
"""


def auto_page(cfg) -> str:
    script = ("<script>" + _COMMON_JS + _AUTO_PRE_JS + _HARDWARE_JS + _AUTO_JS + "</script>")
    script = script.replace("__DEFAULT_STOP__", f"{min(cfg.default_stop_mhz, 12000):g}")
    block = (_AUTO_HTML.replace("{start:g}", f"{cfg.default_start_mhz:g}")
             .replace("{stop:g}", f"{cfg.default_stop_mhz:g}")
             .replace("{astep:g}", f"{cfg.default_step_mhz:g}"))
    return f"""<!doctype html><html><head><meta charset=utf-8>
<title>Frontera — Auto Test</title>
<style>{_STYLE}</style></head><body>
{_NAV}
<h1>Auto Test</h1>
{_HARDWARE_HTML}
{block}
<button class="report" id="report_btn" disabled>Generate PDF report</button>
<div class="status" id="report_status"></div>
{script}
</body></html>"""



_JOURNAL_SCRIPT = """
<script>
const MODE_LABELS = __MODE_LABELS__;
""" + _COMMON_JS + """
function updateDeleteButton(){
  const checked = document.querySelectorAll('.rowchk:checked').length;
  document.getElementById('delete_btn').disabled = checked === 0;
}

async function poll(){
  try{
    const r = await fetch('/api/journal');
    const rows = await r.json();
    const b = document.querySelector('#t tbody');
    b.innerHTML = '';
    for (const c of rows){
      const tr = document.createElement('tr');
      const pdfLink = c.pdf_path ? `<a href="/report/${c.pdf_path}" target=_blank>PDF</a>` : '';
      const aLink = c.png_a ? `<a href="/plot/${c.png_a}" target=_blank>A png</a>` : '';
      const bLink = c.png_b ? `<a href="/plot/${c.png_b}" target=_blank>B png</a>` : '';
      const aData = c.npy_a ? `<a href="/data/${c.npy_a}">A bin</a>` : '';
      const bData = c.npy_b ? `<a href="/data/${c.npy_b}">B bin</a>` : '';
      const setup = (m) => escapeHtml(MODE_LABELS[m] || m || '—');
      const auto = c.kind === 'auto';
      const single = c.kind === 'single' || auto;
      const timeNote = auto ? '<div class=cond>auto</div>' : (single ? '<div class=cond>single</div>' : '');
      const blockA = auto
        ? `${c.start_a}-${c.stop_a}/${c.step_a}` +
          `<div class=cond>@ ${c.gen_level_dbm} dBm, window ${c.window_mhz} MHz, \u00b1${c.tolerance_db} dB</div>` +
          `<div class=cond>${setup(c.device_mode_a)}</div>` +
          `<div class=cond>${escapeHtml(c.conditions_a)}</div>`
        : `${c.start_a}-${c.stop_a}/${c.step_a}<div class=cond>${setup(c.device_mode_a)}</div>` +
          `<div class=cond>${escapeHtml(c.conditions_a)}</div>`;
      // A stopped/aborted run is never shown green: it says nothing about the
      // part of the band it did not reach.
      const incomplete = auto && c.run_state && c.run_state !== 'done';
      const blockB = auto
        ? `<span class="${c.n_fail == 0 && !incomplete ? 'ok' : 'bad'}">pass ${c.n_pass} / fail ${c.n_fail}</span>` +
          (incomplete ? `<div class=cond>${escapeHtml(c.run_state)} after ${c.n_pass + c.n_fail} points</div>` : '')
        : '';
      const peakA = `${c.peak_dbm_a?.toFixed?.(1) ?? ''} @ ${c.peak_freq_a?.toFixed?.(1) ?? ''}` +
        (auto ? '<div class=cond>worst</div>' : '');
      tr.innerHTML = `<td class=chk><input type=checkbox class=rowchk value="${c.id}"></td>` +
        `<td>${c.id}</td><td>${c.ts_utc}${timeNote}</td>` +
        `<td>${blockA}</td>` +
        (single ? `<td>${blockB}</td>` :
        `<td>${c.start_b}-${c.stop_b}/${c.step_b}<div class=cond>${setup(c.device_mode_b)}</div>` +
        `<div class=cond>${escapeHtml(c.conditions_b)}</div></td>`) +
        `<td>${peakA}</td>` +
        `<td>${single ? '' : (c.peak_dbm_b?.toFixed?.(1) ?? '') + ' @ ' + (c.peak_freq_b?.toFixed?.(1) ?? '')}</td>` +
        `<td>${aLink} ${bLink}</td><td>${aData} ${bData}</td><td>${pdfLink}</td>`;
      b.appendChild(tr);
    }
    for (const cb of document.querySelectorAll('.rowchk')){
      cb.addEventListener('change', updateDeleteButton);
    }
    document.getElementById('select_all').checked = false;
    updateDeleteButton();
  } catch(e) {}
}
poll();

document.getElementById('select_all').addEventListener('change', (e) => {
  for (const cb of document.querySelectorAll('.rowchk')) cb.checked = e.target.checked;
  updateDeleteButton();
});

document.getElementById('delete_btn').addEventListener('click', async () => {
  const ids = Array.from(document.querySelectorAll('.rowchk:checked')).map(cb => parseInt(cb.value, 10));
  if (ids.length === 0) return;
  if (!confirm(`Delete ${ids.length} comparison(s)? This also removes the stored plots, binaries and PDF.`)) return;
  const btn = document.getElementById('delete_btn');
  btn.disabled = true;
  try {
    const r = await fetch('/api/journal/delete', {method:'POST', body: JSON.stringify({ids})});
    if (!r.ok) throw new Error(await errorText(r));
    await poll();
  } catch(e) {
    alert('Delete failed: ' + e.message);
  }
});
</script>
"""


def journal_page() -> str:
    return f"""<!doctype html><html><head><meta charset=utf-8>
<title>Frontera — Test Journal</title>
<style>{_STYLE}</style></head><body>
{_NAV}
<h1>Test Journal</h1>
<table id=t><thead><tr>
<th class=chk><input type=checkbox id=select_all></th>
<th>#</th><th>time (UTC)</th><th>Block A (start-stop/step, bench)</th>
<th>Block B (start-stop/step, bench)</th>
<th>peak A</th><th>peak B</th><th>plots</th><th>data</th><th>report</th>
</tr></thead><tbody></tbody></table>
<button class="danger" id="delete_btn" disabled>Delete selected</button>
{_JOURNAL_SCRIPT.replace('__MODE_LABELS__', _MODE_LABELS_JSON)}
</body></html>"""
