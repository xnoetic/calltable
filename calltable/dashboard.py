"""单页看板：内联 CSS/JS，无任何外部依赖，每 3 秒轮询 hub。"""

from __future__ import annotations

import json

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>calltable</title>
<style>
:root {
  --bg:#0f1216; --panel:#171d24; --panel2:#1e262f; --line:#2b3540;
  --text:#e6edf3; --dim:#8b98a5; --accent:#4aa8ff; --ok:#3fb950;
  --bad:#f85149; --warn:#d29922; --run:#58a6ff;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font:13px/1.5 ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace; }
header { padding:14px 20px; border-bottom:1px solid var(--line);
  display:flex; gap:16px; align-items:baseline; flex-wrap:wrap; }
h1 { font-size:16px; margin:0; font-weight:600; letter-spacing:.5px; }
h2 { font-size:13px; margin:0 0 8px; color:var(--dim); font-weight:600;
  text-transform:uppercase; letter-spacing:.08em; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
main { padding:16px 20px 48px; max-width:1500px; margin:0 auto; }
.hosts { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr));
  gap:12px; margin-bottom:20px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:8px;
  padding:12px 14px; }
.card .name { font-size:14px; font-weight:600; display:flex; gap:8px;
  align-items:center; justify-content:space-between; }
.dot { width:8px; height:8px; border-radius:50%; display:inline-block; }
.online { background:var(--ok); } .offline { background:var(--bad); }
.slots { display:flex; gap:6px; flex-wrap:wrap; margin:8px 0; }
.pill { border:1px solid var(--line); border-radius:999px; padding:1px 9px;
  color:var(--dim); font-size:11px; }
.pill.full { color:var(--warn); border-color:var(--warn); }
.pill.lock { color:var(--accent); border-color:var(--accent); }
.sub { color:var(--dim); font-size:11px; margin-top:2px; }
table { width:100%; border-collapse:collapse; background:var(--panel);
  border:1px solid var(--line); border-radius:8px; overflow:hidden; }
th,td { text-align:left; padding:6px 9px; border-bottom:1px solid var(--line);
  white-space:nowrap; }
th { color:var(--dim); font-weight:600; font-size:11px; text-transform:uppercase;
  letter-spacing:.06em; background:var(--panel2); }
tr:last-child td { border-bottom:none; }
td.wrap { white-space:normal; }
tr.clickable { cursor:pointer; } tr.clickable:hover { background:var(--panel2); }
.status-succeeded { color:var(--ok); } .status-failed { color:var(--bad); }
.status-cancelled,.status-timeout,.status-error { color:var(--warn); }
.status-running { color:var(--run); } .status-queued { color:var(--dim); }
section { margin-bottom:22px; }
.empty { color:var(--dim); padding:10px 0; }
#detail { display:none; }
#overview.hidden { display:none; }
.back { margin-bottom:12px; display:inline-block; }
.meta { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
  gap:4px 18px; margin:10px 0 14px; color:var(--dim); }
.meta b { color:var(--text); font-weight:500; }
pre { background:#0b0e12; border:1px solid var(--line); border-radius:8px;
  padding:10px; overflow:auto; max-height:420px; margin:0;
  font-size:12px; line-height:1.45; white-space:pre-wrap; word-break:break-all; }
svg { width:100%; height:150px; background:#0b0e12; border:1px solid var(--line);
  border-radius:8px; display:block; }
.legend { color:var(--dim); font-size:11px; margin:6px 0 0; }
.toolbar { display:flex; gap:10px; align-items:center; margin:8px 0; }
button { background:var(--panel2); color:var(--text); border:1px solid var(--line);
  border-radius:6px; padding:3px 10px; font:inherit; cursor:pointer; }
button:hover { border-color:var(--accent); }
button.danger:hover { border-color:var(--bad); color:var(--bad); }
.artifact { display:flex; gap:10px; align-items:center; padding:3px 0; }
code { color:var(--accent); }
</style>
</head>
<body>
<header>
  <h1>calltable</h1>
  <span class="sub" id="summary">connecting…</span>
  <span style="flex:1"></span>
  <a id="beszel" href="#" target="_blank" rel="noreferrer">Beszel 监控 ↗</a>
</header>
<main>
  <div id="tokenbox" style="display:none">
    <p>需要 hub token：<input id="tokeninput" size="48"> <button onclick="saveToken()">保存</button></p>
  </div>
  <div id="overview">
    <section><h2>主机</h2><div class="hosts" id="hosts"></div></section>
    <section><h2>排队</h2><div id="queue"></div></section>
    <section><h2>最近任务</h2><div id="history"></div></section>
  </div>
  <div id="detail">
    <a class="back" href="#">← 返回</a>
    <section>
      <h2>任务 <span id="d-id"></span></h2>
      <div class="meta" id="d-meta"></div>
      <div class="toolbar">
        <button onclick="toggleFollow()" id="follow">自动刷新：开</button>
        <button class="danger" onclick="cancelJob()">取消任务</button>
      </div>
      <h2>日志（末尾）</h2>
      <pre id="d-log">…</pre>
    </section>
    <section>
      <h2>资源</h2>
      <svg id="d-chart" viewBox="0 0 600 150" preserveAspectRatio="none"></svg>
      <p class="legend" id="d-legend"></p>
    </section>
    <section>
      <h2>产物</h2>
      <div id="d-artifacts" class="sub"></div>
    </section>
    <section>
      <h2>叫号通知</h2>
      <div id="d-webhooks" class="sub"></div>
    </section>
  </div>
</main>
<script>
const TOKEN = __TOKEN__;
const BESZEL = __BESZEL__;
let detailId = null, follow = true, logOffset = 0, logText = "", timer = null;

if (BESZEL) { document.getElementById('beszel').href = BESZEL; }
else { document.getElementById('beszel').style.display = 'none'; }

function saveToken() {
  localStorage.setItem('calltable_token', document.getElementById('tokeninput').value.trim());
  location.reload();
}
function tok() {
  return TOKEN || localStorage.getItem('calltable_token') || '';
}
function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({'Authorization': 'Bearer ' + tok()}, opts.headers || {});
  return fetch(path, opts).then(r => {
    if (r.status === 401) { document.getElementById('tokenbox').style.display = 'block'; throw new Error('unauthorized'); }
    if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
    return r;
  });
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtDur(sec) {
  if (sec == null) return '-';
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + 's';
  const m = Math.floor(sec/60), s = sec%60;
  if (m < 60) return m + 'm' + String(s).padStart(2,'0') + 's';
  const h = Math.floor(m/60);
  return h + 'h' + String(m%60).padStart(2,'0') + 'm';
}
function fmtBytes(n) {
  if (!n) return '0';
  const u = ['B','KB','MB','GB','TB']; let i = 0;
  while (n >= 1024 && i < u.length-1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + u[i];
}
function fmtTime(ts) {
  if (!ts) return '-';
  return new Date(ts*1000).toLocaleString();
}
function statusClass(s) { return 'status-' + s; }

function renderHosts(hosts, groups) {
  const box = document.getElementById('hosts');
  if (!hosts.length) { box.innerHTML = '<div class="empty">没有主机上报过心跳。</div>'; return; }
  const membership = {};
  for (const [name, members] of Object.entries(groups || {})) {
    for (const member of members) membership[member] = name;
  }
  box.innerHTML = hosts.map(h => {
    const group = membership[h.name];
    const sys = h.system || {};
    const slots = Object.entries(h.slots || {}).map(([name, limit]) => {
      const used = (h.used || {})[name] || 0;
      return `<span class="pill ${used >= limit ? 'full' : ''}">${esc(name)} ${used}/${limit}</span>`;
    }).join('');
    const locks = (h.locks || []).map(l => `<span class="pill lock">🔒 ${esc(l)}</span>`).join('');
    const res = [];
    if (sys.disk_free_gb != null) res.push(`盘余 ${sys.disk_free_gb}G`);
    if (sys.disk_runs_gb != null) res.push(`runs ${sys.disk_runs_partial ? '≥' : ''}${sys.disk_runs_gb}G`);
    if (sys.mem_free_gb) res.push(`内存 ${sys.mem_free_gb}/${sys.mem_total_gb || '?'}G`);
    if (sys.load1) res.push(`load ${sys.load1}`);
    if (h.queue) res.push(`排队 ${h.queue}`);
    const running = (h.running || []).map(j =>
      `<div class="sub">▶ <a href="#/job/${esc(j.id)}">${esc(j.id)}</a> ${esc(j.project)} ${esc(j.label||'')}</div>`).join('');
    return `<div class="card">
      <div class="name"><span>${esc(h.name)}
        <span class="dot ${h.online ? 'online' : 'offline'}"></span></span>
        <span class="sub">${group ? 'group:' + esc(group) + ' · ' : ''}${h.online ? 'online' : 'offline'}</span></div>
      ${h.disk_low ? `<div class="pill full">⚠ disk_low ${esc(sys.disk_reason || '')} · 暂停领取</div>` : ''}
      <div class="slots">${slots || '<span class="sub">未配置 slot</span>'}</div>
      <div class="slots">${locks}</div>
      <div class="sub">${res.map(esc).join(' · ')}</div>
      <div class="sub">${esc(h.os||'')} · 最后心跳 ${fmtTime(h.last_seen)}${h.note ? ' · ' + esc(h.note) : ''}</div>
      ${running}
    </div>`;
  }).join('');
}

function renderQueue(jobs) {
  const box = document.getElementById('queue');
  if (!jobs.length) { box.innerHTML = '<div class="empty">队列为空。</div>'; return; }
  const est = v => v == null ? '未知' : fmtDur(v);
  box.innerHTML = `<table><thead><tr>
    <th>id</th><th>项目</th><th>主机</th><th>slot</th><th>锁</th>
    <th>已等</th><th>前面</th><th>预计等待</th><th>预计耗时</th>
    <th>提交人</th><th>label</th><th>创建</th></tr></thead><tbody>` +
    jobs.map(j => {
      const q = j.queue || {};
      return `<tr class="clickable" onclick="location.hash='#/job/${esc(j.id)}'">
      <td><code>${esc(j.id)}</code></td><td>${esc(j.project)}</td><td>${esc(j.host)}</td>
      <td>${esc(j.slot)}</td><td>${esc((j.locks_display||j.locks||[]).join(', '))}</td>
      <td>${fmtDur(j.queued_seconds)}</td><td>${j.ahead == null ? '-' : j.ahead}</td>
      <td>${est(q.wait_seconds)}</td><td>${est(q.runtime_seconds)}</td>
      <td>${esc(j.submitter)}</td><td class="wrap">${esc(j.label)}</td>
      <td>${fmtTime(j.created)}</td></tr>`;
    }).join('') + '</tbody></table>';
}

function renderHistory(jobs) {
  const box = document.getElementById('history');
  if (!jobs.length) { box.innerHTML = '<div class="empty">还没有任务。</div>'; return; }
  box.innerHTML = `<table><thead><tr>
    <th>id</th><th>项目</th><th>主机</th><th>提交人</th><th>label</th>
    <th>SHA</th><th>dirty</th><th>开始</th><th>耗时</th><th>状态</th>
    <th>CPU 总秒 / 峰值核数</th><th>内存峰值</th></tr></thead><tbody>` +
    jobs.map(j => {
      const dur = j.started ? ((j.finished || Date.now()/1000) - j.started) : null;
      const sha = j.sha ? j.sha.slice(0, 9) : '-';
      return `<tr class="clickable" onclick="location.hash='#/job/${esc(j.id)}'">
        <td><code>${esc(j.id)}</code></td><td>${esc(j.project)}</td><td>${esc(j.host)}</td>
        <td>${esc(j.submitter)}${j.agent ? ' <span class="pill">' + esc(j.agent) + '</span>' : ''}</td><td class="wrap">${esc(j.label)}</td>
        <td>${esc(sha)}</td><td>${j.dirty ? '是' : ''}</td>
        <td>${fmtTime(j.started || j.created)}</td><td>${fmtDur(dur)}</td>
        <td class="${statusClass(j.status)}">${esc(j.status)}${j.exit_code != null ? ' (' + j.exit_code + ')' : ''}</td>
        <td>${(j.cpu_total_sec || 0).toFixed(1)}s / ${(j.cpu_peak_cores || 0).toFixed(2)}c</td><td>${fmtBytes(j.mem_peak)}</td>
      </tr>`;
    }).join('') + '</tbody></table>';
}

function refreshOverview() {
  api('/api/state').then(r => r.json()).then(s => {
    document.getElementById('summary').textContent =
      `${(s.hosts||[]).length} 台主机 · ${(s.queued||[]).length} 排队 · ` +
      `${(s.running||[]).length} 运行中 · 更新于 ${new Date().toLocaleTimeString()}`;
    renderHosts(s.hosts || [], s.groups || {});
    renderQueue(s.queued || []);
    renderHistory(s.jobs || []);
  }).catch(() => {});
}

function drawChart(samples) {
  const svg = document.getElementById('d-chart');
  if (!samples || samples.length < 2) {
    svg.innerHTML = '<text x="10" y="80" fill="#8b98a5" font-size="12">还没有采样</text>';
    document.getElementById('d-legend').textContent = '';
    return;
  }
  const W = 600, H = 150, pad = 8;
  const memMax = Math.max(...samples.map(s => s.mem_peak || 0), 1);
  const cpuMax = Math.max(...samples.map(s => s.cpu_usec || 0), 1);
  const n = samples.length;
  const x = i => pad + (W - 2*pad) * i / (n - 1);
  const ym = v => H - pad - (H - 2*pad) * (v / memMax);
  const yc = v => H - pad - (H - 2*pad) * (v / cpuMax);
  const memPath = samples.map((s,i) => `${i?'L':'M'}${x(i).toFixed(1)},${ym(s.mem_peak||0).toFixed(1)}`).join(' ');
  const curPath = samples.map((s,i) => `${i?'L':'M'}${x(i).toFixed(1)},${ym(s.mem_current||0).toFixed(1)}`).join(' ');
  const cpuPath = samples.map((s,i) => `${i?'L':'M'}${x(i).toFixed(1)},${yc(s.cpu_usec||0).toFixed(1)}`).join(' ');
  svg.innerHTML = `
    <path d="${memPath}" fill="none" stroke="#4aa8ff" stroke-width="1.6"/>
    <path d="${curPath}" fill="none" stroke="#3fb950" stroke-width="1" stroke-dasharray="3 3"/>
    <path d="${cpuPath}" fill="none" stroke="#d29922" stroke-width="1.2" opacity="0.85"/>`;
  const last = samples[n-1];
  document.getElementById('d-legend').textContent =
    `蓝=内存峰值 ${fmtBytes(memMax)} · 绿虚线=内存当前 ${fmtBytes(last.mem_current)} · ` +
    `橙=累计 CPU ${(last.cpu_usec/1e6).toFixed(1)}s（纵轴各自归一化）· ${n} 个采样`;
}

function renderDetail(job) {
  document.getElementById('d-id').textContent = job.id;
  const dur = job.started ? ((job.finished || Date.now()/1000) - job.started) : null;
  document.getElementById('d-meta').innerHTML = `
    <div>项目 <b>${esc(job.project)}</b></div>
    <div>主机 <b>${esc(job.host)}</b> / ${esc(job.slot)}</div>
    <div>状态 <b class="${statusClass(job.status)}">${esc(job.status)}</b>${job.exit_code != null ? ' (' + job.exit_code + ')' : ''}</div>
    <div>提交人 <b>${esc(job.submitter)}</b></div>
    <div>agent <b>${esc(job.agent || '-')}</b></div>
    <div>label <b>${esc(job.label || '-')}</b></div>
    <div>SHA <b>${esc(job.sha || '-')}</b>${job.dirty ? ' <span class="pill">含未提交改动</span>' : ''}</div>
    <div>base <b>${esc(job.base_ref || '-')}</b> = ${esc((job.base_ref_sha || '-').slice(0,12))}
      ${job.dirty ? '<span class="pill">baseline ' + esc((job.base_sha || '-').slice(0,12)) + '</span>' : ''}</div>
    <div>锁 <b>${esc(((job.locks_display||job.locks||[]).join(', ')) || '-')}</b></div>
    ${job.status === 'queued' ? `<div>排队 <b>${fmtDur(job.queued_seconds)}</b> 前面 ${job.ahead == null ? '-' : job.ahead} 个 · 预计等待 <b>${(job.queue||{}).wait_seconds == null ? '未知' : fmtDur(job.queue.wait_seconds)}</b> · 预计耗时 <b>${(job.queue||{}).runtime_seconds == null ? '未知' : fmtDur(job.queue.runtime_seconds)}</b></div>` : ''}
    ${job.hold_seconds ? `<div>号码绑定 <b>${fmtDur(job.hold_seconds)}</b> 无心跳就自动取消</div>` : ''}
    <div>创建 <b>${fmtTime(job.created)}</b></div>
    <div>开始 <b>${fmtTime(job.started)}</b></div>
    <div>耗时 <b>${fmtDur(dur)}</b></div>
    <div>超时 <b>${fmtDur(job.timeout)}</b></div>
    <div>CPU 总秒 <b>${(job.cpu_total_sec||0).toFixed(3)}s</b></div>
    <div>峰值核数 <b>${(job.cpu_peak_cores||0).toFixed(2)}c</b></div>
    <div>内存峰值 <b>${fmtBytes(job.mem_peak)}</b></div>
    <div class="wrap">命令 <b>${esc((job.cmd||[]).join(' ') || '-')}</b></div>
    ${job.error ? `<div class="wrap">错误 <b>${esc(job.error)}</b></div>` : ''}`;
}

function renderArtifacts(list, jobId) {
  const box = document.getElementById('d-artifacts');
  if (!list || !list.length) { box.innerHTML = '<div class="empty">没有产物。</div>'; return; }
  box.innerHTML = list.map(a =>
    `<div class="artifact"><a href="#" data-job="${esc(jobId)}" data-name="${esc(a.name)}" class="artifact-link">${esc(a.name)}</a>
      <span class="sub">${fmtBytes(a.size)} · sha256 ${esc((a.sha256||'').slice(0,16))}…</span></div>`).join('');
  for (const link of box.querySelectorAll('.artifact-link')) {
    link.addEventListener('click', event => {
      event.preventDefault();
      downloadArtifact(link.dataset.job, link.dataset.name);
    });
  }
}

function loadLogs(jobId) {
  api(`/api/jobs/${encodeURIComponent(jobId)}/logs?offset=${logOffset}`).then(r => {
    const next = r.headers.get('X-Calltable-Next');
    return r.text().then(text => {
      if (text) {
        logText += text;
        if (logText.length > 200000) logText = logText.slice(-200000);
        const pre = document.getElementById('d-log');
        pre.textContent = logText;
        pre.scrollTop = pre.scrollHeight;
      }
      if (next) logOffset = parseInt(next, 10);
    });
  }).catch(() => {});
}

function renderWebhooks(deliveries) {
  const box = document.getElementById('d-webhooks');
  if (!box) return;
  if (!deliveries.length) { box.innerHTML = '<span class="sub">没有叫号通知。</span>'; return; }
  box.innerHTML = deliveries.map(d =>
    `<div>${esc(d.url)}</div>` + (d.events || []).map(e =>
      `<div class="sub">${esc(e.event)} · ${esc(e.status)} · 尝试 ${e.attempts || 0}` +
      (e.last_status ? ` · HTTP ${e.last_status}` : '') +
      (e.last_error ? ` · ${esc(e.last_error)}` : '') + '</div>').join('')
  ).join('');
}

function downloadArtifact(jobId, name) {
  // 用 fetch 带 Authorization header 下载；不再把 token 拼进 URL（A9）。
  api(`/api/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(name)}`)
    .then(r => r.blob())
    .then(blob => {
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = name.split('/').pop();
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    })
    .catch(() => alert('artifact download failed'));
}

function toggleFollow() {
  follow = !follow;
  document.getElementById('follow').textContent = '自动刷新：' + (follow ? '开' : '关');
}

function cancelJob() {
  if (!detailId) return;
  if (!confirm('确定取消 ' + detailId + '？')) return;
  api(`/api/jobs/${encodeURIComponent(detailId)}/cancel`, {method: 'POST'}).then(() => refreshDetail());
}

function refreshDetail() {
  if (!detailId) return;
  api(`/api/jobs/${encodeURIComponent(detailId)}`).then(r => r.json()).then(job => {
    renderDetail(job);
    renderArtifacts(job.artifacts || [], job.id);
    renderWebhooks(job.webhook_deliveries || []);
    if (follow) loadLogs(job.id);
    api(`/api/jobs/${encodeURIComponent(job.id)}/samples`).then(r => r.json())
      .then(s => drawChart(s.samples || s)).catch(() => {});
  }).catch(() => {});
}

function route() {
  const hash = location.hash || '';
  const m = hash.match(/^#\\/job\\/(.+)$/);
  const overview = document.getElementById('overview');
  const detail = document.getElementById('detail');
  if (m) {
    const id = decodeURIComponent(m[1]);
    if (id !== detailId) { detailId = id; logOffset = 0; logText = ''; }
    overview.classList.add('hidden'); detail.style.display = 'block';
    refreshDetail();
  } else {
    detailId = null; overview.classList.remove('hidden'); detail.style.display = 'none';
    refreshOverview();
  }
}

window.addEventListener('hashchange', route);
route();
timer = setInterval(() => { if (detailId) refreshDetail(); else refreshOverview(); }, 3000);
</script>
</body>
</html>
"""


def render_page(token: str, beszel_url: str = "") -> str:
    return (PAGE
            .replace("__TOKEN__", json.dumps(token))
            .replace("__BESZEL__", json.dumps(beszel_url or "")))
