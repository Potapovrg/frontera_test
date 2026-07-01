"""Inline HTML/CSS/JS for the two pages this app serves.

Same approach as frontera_ml/persistence/log_server.py: no template engine,
no static assets folder — pages are Python string constants, dark themed,
vanilla JS + fetch().
"""
from __future__ import annotations

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
th,td{border:1px solid #30363d;padding:4px 8px;text-align:left;font-size:12px}
th{background:#161b22;color:#8b949e}
tr:nth-child(even){background:#161b22}
nav{margin-bottom:14px;font-size:13px}
nav a{margin-right:14px}
"""

_NAV = """<nav><a href="/">Comparison Test</a><a href="/journal">Test Journal</a></nav>"""


def _block_html(block: str, title: str) -> str:
    return f"""
<div class="block {block.lower()}" id="block_{block}">
  <h2>Block {block} — {title}</h2>
  <div class="row">
    <label>Start (MHz)</label><input type=number step=any id="{block}_start" value=100 class=param>
    <label>Stop (MHz)</label><input type=number step=any id="{block}_stop" value=6000 class=param>
    <label>Step (MHz)</label><input type=number step=any id="{block}_step" value=1 class=param>
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


_SCRIPT = """
<script>
const state = {A: {edited:false, done:false}, B: {edited:false, done:false}};

function todayStr(){ return new Date().toISOString().slice(0,19).replace('T',' ') + ' UTC'; }

function defaultConditions(block){
  const start = document.getElementById(block+'_start').value;
  const stop  = document.getElementById(block+'_stop').value;
  const step  = document.getElementById(block+'_step').value;
  return `${todayStr()}; Sweep ${start}-${stop} MHz, step ${step} MHz`;
}

function refreshDefault(block){
  if (state[block].edited) return;
  document.getElementById(block+'_conditions').value = defaultConditions(block);
}

for (const block of ['A','B']){
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
      };
      const r = await fetch('/api/sweep', {method:'POST', body: JSON.stringify(body)});
      if (!r.ok) throw new Error(await r.text());
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
  document.getElementById('report_btn').disabled = !(state.A.done && state.B.done);
}

document.getElementById('report_btn').addEventListener('click', async () => {
  const statusEl = document.getElementById('report_status');
  const btn = document.getElementById('report_btn');
  btn.disabled = true;
  statusEl.textContent = 'Generating PDF...';
  try {
    const r = await fetch('/api/report', {method:'POST'});
    if (!r.ok) throw new Error(await r.text());
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


def comparison_page() -> str:
    return f"""<!doctype html><html><head><meta charset=utf-8>
<title>Frontera — Comparison Test</title>
<style>{_STYLE}</style></head><body>
{_NAV}
<h1>Comparison Test</h1>
{_block_html('A', 'before hardware change')}
{_block_html('B', 'after hardware change')}
<button class="report" id="report_btn" disabled>Generate PDF report</button>
<div class="status" id="report_status"></div>
{_SCRIPT}
</body></html>"""


_JOURNAL_SCRIPT = """
<script>
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
      tr.innerHTML = `<td>${c.id}</td><td>${c.ts_utc}</td>` +
        `<td>${c.start_a}-${c.stop_a}/${c.step_a}</td>` +
        `<td>${c.start_b}-${c.stop_b}/${c.step_b}</td>` +
        `<td>${c.peak_dbm_a?.toFixed?.(1) ?? ''} @ ${c.peak_freq_a?.toFixed?.(1) ?? ''}</td>` +
        `<td>${c.peak_dbm_b?.toFixed?.(1) ?? ''} @ ${c.peak_freq_b?.toFixed?.(1) ?? ''}</td>` +
        `<td>${aLink} ${bLink}</td><td>${aData} ${bData}</td><td>${pdfLink}</td>`;
      b.appendChild(tr);
    }
  } catch(e) {}
}
poll();
</script>
"""


def journal_page() -> str:
    return f"""<!doctype html><html><head><meta charset=utf-8>
<title>Frontera — Test Journal</title>
<style>{_STYLE}</style></head><body>
{_NAV}
<h1>Test Journal</h1>
<table id=t><thead><tr>
<th>#</th><th>time (UTC)</th><th>Block A (start-stop/step)</th><th>Block B (start-stop/step)</th>
<th>peak A</th><th>peak B</th><th>plots</th><th>data</th><th>report</th>
</tr></thead><tbody></tbody></table>
{_JOURNAL_SCRIPT}
</body></html>"""
