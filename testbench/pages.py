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
.ports .unit{display:inline-block;min-width:92px;color:#c9d1d9}
.ports .src{color:#6e7681}
.ok{color:#3fb950}
.bad{color:#f85149}
.warn{color:#d29922;font-size:12px;margin-top:6px;white-space:pre-wrap}
"""

_NAV = """<nav><a href="/">Comparison Test</a><a href="/single">Single Test</a><a href="/journal">Test Journal</a></nav>"""


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

_SCRIPT = """
<script>
""" + _COMMON_JS + """
const BLOCKS = __BLOCKS__;
const SINGLE = __SINGLE__;
const REPORT_URL = '__REPORT_URL__';
const state = {};
for (const b of BLOCKS) state[b] = {edited:false, done:false};
let HW = null;   // latest /api/hardware payload

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

// -- hardware panel ---------------------------------------------------------
// Seeded with the value the stop inputs are rendered with, so the very first
// render can already retune it if the bench started in a 12 GHz mode.
let lastMax = __DEFAULT_STOP__;
const UNIT_NAMES = {frontera1:'Frontera 1', frontera2:'Frontera 2', switcher:'Shifter'};
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
  for (const key of ['frontera1','frontera2','switcher']){
    const p = HW.ports[key] || {};
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

  // Retune the frequency inputs to what this topology can actually reach.
  // The stop value follows the ceiling only while it still sits on the
  // previous ceiling — i.e. the operator hasn't typed their own span — so
  // switching to a 12 GHz setup gives a 12 GHz sweep without clobbering a
  // deliberately narrowed range. Above the new ceiling it is always clamped.
  for (const block of BLOCKS){
    const stopEl = document.getElementById(block+'_stop');
    const value = parseFloat(stopEl.value);
    stopEl.max = HW.max_stop_mhz;
    if (value === lastMax || value > HW.max_stop_mhz) stopEl.value = HW.max_stop_mhz;
    refreshDefault(block);
  }
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
      const single = c.kind === 'single';
      tr.innerHTML = `<td class=chk><input type=checkbox class=rowchk value="${c.id}"></td>` +
        `<td>${c.id}</td><td>${c.ts_utc}${single ? '<div class=cond>single</div>' : ''}</td>` +
        `<td>${c.start_a}-${c.stop_a}/${c.step_a}<div class=cond>${setup(c.device_mode_a)}</div>` +
        `<div class=cond>${escapeHtml(c.conditions_a)}</div></td>` +
        (single ? `<td></td>` :
        `<td>${c.start_b}-${c.stop_b}/${c.step_b}<div class=cond>${setup(c.device_mode_b)}</div>` +
        `<div class=cond>${escapeHtml(c.conditions_b)}</div></td>`) +
        `<td>${c.peak_dbm_a?.toFixed?.(1) ?? ''} @ ${c.peak_freq_a?.toFixed?.(1) ?? ''}</td>` +
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
