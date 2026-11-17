/* Self-Healing Network Dashboard (Module 8). Plain JS, no build step.
 * Polls /api/overview every POLL_MS; other views load their data when shown. */

const POLL_MS = 2000;
const $ = (id) => document.getElementById(id);
const COLORS = { HEALTHY: '#3fb950', DEGRADED: '#d29922', DOWN: '#f85149', UNKNOWN: '#6e7681' };
const LEVEL_COLORS = { TRUSTED: '#3fb950', RESTRICTED: '#d29922', QUARANTINED: '#f85149' };
const CATEGORIES = {
  failure: ['FAILURE_INJECTED', 'FAILURE_RECOVERED', 'LINK_DISCONNECTED', 'LINK_CONNECTED', 'NODE_DISABLED',
            'NODE_ENABLED', 'LATENCY_INJECTED', 'PACKET_LOSS_INJECTED', 'BANDWIDTH_LIMITED', 'NETWORK_RESTORED'],
  health: ['HEALTH_CHANGED', 'TELEMETRY_SOURCE_ERROR'],
  prediction: ['FAILURE_PREDICTED', 'PREDICTION_CLEARED'],
  security: ['SECURITY_ANOMALY', 'ACCESS_DENIED', 'TRUST_CHANGED', 'DEVICE_QUARANTINED', 'DEVICE_RELEASED'],
  decision: ['DECISION_MADE', 'APPROVAL_REQUIRED', 'INTENT_RESOLVED'],
  healing: ['HEALING_STARTED', 'HEALING_VERIFIED', 'HEALING_FAILED', 'HEALING_ROLLED_BACK', 'ESCALATED'],
};
const CHART_METRICS = {
  link: [['link.latency_ms', 'Latency', 'ms', 'latency_ms'], ['link.packet_loss_pct', 'Packet loss', '%', 'packet_loss_pct'],
         ['link.utilization_pct', 'Utilization', '%', 'utilization_pct'], ['link.throughput_mbps', 'Throughput', 'Mbps', null],
         ['link.status', 'Link up', '', null]],
  node: [['node.cpu_pct', 'CPU', '%', 'cpu_pct'], ['node.memory_pct', 'Memory', '%', 'memory_pct'], ['node.status', 'Node up', '', null]],
};

let state = null;
let cy = null;
let activeTab = 'overview';
let timelineFilter = new Set(Object.keys(CATEGORIES));

/* ── helpers ─────────────────────────────────────────────────────────────── */
function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function fmt(v, digits = 1, unit = '') {
  return v === null || v === undefined || Number.isNaN(v) ? '—' : `${Number(v).toFixed(digits)}${unit}`;
}
function hhmmss(t) {
  if (t === null || t === undefined) return '—';
  const d = typeof t === 'number' ? new Date(t * 1000) : new Date(t);
  return d.toLocaleTimeString([], { hour12: false });
}
function chip(text, cls = '') { return `<span class="chip ${cls}">${esc(text)}</span>`; }
function statusChip(s) { return chip(s, { HEALTHY: 'ok', DEGRADED: 'warn', DOWN: 'bad' }[s] || ''); }
function stateChip(s) {
  return chip(s, { VERIFIED: 'ok', FAILED_BACK: 'ok', VERIFYING: 'info', EXECUTING: 'info', RETRY: 'warn',
    ROLLED_BACK: 'warn', ESCALATED: 'bad', FAILED: 'bad', APPROVED: 'ok', PENDING_APPROVAL: 'warn' }[s] || '');
}
async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) { const e = new Error((await r.json().catch(() => ({}))).detail || r.statusText); e.status = r.status; throw e; }
  return r.json();
}
async function postJSON(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}
function empty(text) { return `<div class="empty">${esc(text)}</div>`; }

/* ── clock + tabs ────────────────────────────────────────────────────────── */
setInterval(() => { $('clock').textContent = new Date().toLocaleTimeString([], { hour12: false }); }, 1000);
document.querySelectorAll('.tab').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
function showTab(name) {
  activeTab = name;
  document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${name}`));
  if (name === 'overview' && cy) { cy.resize(); cy.fit(undefined, 30); }
  refreshTab();
}

/* ── topology ────────────────────────────────────────────────────────────── */
function layout(nodes, links) {
  const kind = Object.fromEntries(nodes.map((n) => [n.name, n.kind]));
  const switches = nodes.filter((n) => n.kind === 'switch').map((n) => n.name).sort();
  const routers = nodes.filter((n) => n.kind === 'router').map((n) => n.name).sort();
  const pos = {};
  const W = 900;
  routers.forEach((r, i) => { pos[r] = { x: W / 2 + (i - (routers.length - 1) / 2) * 160, y: 60 }; });
  switches.forEach((s, i) => { pos[s] = { x: (W / (switches.length + 1)) * (i + 1), y: 230 }; });
  const under = {};
  links.forEach((l) => {
    const [a, b] = [l.src, l.dst];
    if (kind[a] === 'switch' && !['switch', 'router'].includes(kind[b])) (under[a] ||= []).push(b);
    if (kind[b] === 'switch' && !['switch', 'router'].includes(kind[a])) (under[b] ||= []).push(a);
  });
  Object.entries(under).forEach(([sw, kids]) => {
    kids.sort().forEach((k, i) => { pos[k] = { x: pos[sw].x + (i - (kids.length - 1) / 2) * 85, y: 400 + (i % 2) * 34 }; });
  });
  nodes.forEach((n, i) => { pos[n.name] ||= { x: 80 + i * 70, y: 520 }; });
  return pos;
}

function nodeStyle(n) {
  const size = { router: 36, switch: 30, server: 24, host: 20 }[n.kind] || 20;
  let fill = { router: '#2f81f7', switch: '#58a6ff', server: '#d29922', host: '#8b949e' }[n.kind] || '#8b949e';
  if (n.status === 'DOWN') fill = '#484f58';
  let border = COLORS[n.status] || COLORS.UNKNOWN;
  if (n.level === 'QUARANTINED' || n.blocked) { border = '#f85149'; fill = '#5a1e1e'; }
  else if (n.level === 'RESTRICTED') border = '#d29922';
  const label = n.blocked || n.level === 'QUARANTINED' ? `${n.name} ⊘` : n.name;
  return { size, fill, border, label };
}

function edgeStyle(l) {
  let color = COLORS[l.status] || COLORS.UNKNOWN;
  if (l.rerouted && l.status === 'HEALTHY') color = '#39c5cf';
  const width = 2 + Math.min(8, (l.utilization_pct || 0) / 12);
  return { color, width, style: l.status === 'DOWN' ? 'dashed' : 'solid', glow: l.predicted ? 6 : 0 };
}

function renderTopology(topo) {
  if (!window.cytoscape) { $('cy').innerHTML = empty('Topology view needs internet access to load Cytoscape.'); return; }
  const pos = layout(topo.nodes, topo.links);
  const els = [];
  topo.nodes.forEach((n) => {
    const s = nodeStyle(n);
    els.push({ group: 'nodes', data: { id: n.name, label: s.label, size: s.size, fill: s.fill, border: s.border, info: n },
               position: pos[n.name] });
  });
  topo.links.forEach((l) => {
    const s = edgeStyle(l);
    els.push({ group: 'edges', data: { id: l.name, source: l.src, target: l.dst, color: s.color, width: s.width,
               lstyle: s.style, glow: s.glow, info: l } });
  });
  if (!cy) {
    cy = cytoscape({
      container: $('cy'), elements: els, layout: { name: 'preset' }, minZoom: 0.4, maxZoom: 2.5,
      // fixed view: the mouse wheel scrolls the page instead of zooming; the graph auto-fits
      boxSelectionEnabled: false, autoungrabify: true, userZoomingEnabled: false, userPanningEnabled: false,
      style: [
        { selector: 'node', style: { label: 'data(label)', 'font-size': 11, color: '#e6edf3', 'text-valign': 'bottom',
            'text-margin-y': 6, 'text-outline-color': '#0f1117', 'text-outline-width': 2, 'background-color': 'data(fill)',
            'border-width': 3, 'border-color': 'data(border)', width: 'data(size)', height: 'data(size)' } },
        { selector: 'edge', style: { 'curve-style': 'bezier', width: 'data(width)', 'line-color': 'data(color)',
            'line-style': 'data(lstyle)', 'underlay-color': '#bc8cff', 'underlay-padding': 'data(glow)',
            'underlay-opacity': 0.45 } },
      ],
    });
    // the container's final size is only known after layout: fit then, and on resize
    const refit = () => { cy.resize(); cy.fit(undefined, 30); };
    requestAnimationFrame(refit);
    setTimeout(refit, 300);
    window.addEventListener('resize', refit);
    const tip = $('tooltip');
    cy.on('mouseover', 'node, edge', (e) => { tip.innerHTML = tooltipHtml(e.target); tip.hidden = false; });
    cy.on('mousemove', (e) => { tip.style.left = `${e.originalEvent.clientX + 14}px`; tip.style.top = `${e.originalEvent.clientY + 10}px`; });
    cy.on('mouseout', 'node, edge', () => { tip.hidden = true; });
    cy.on('tap', 'node, edge', (e) => { $('seriesTarget').value = e.target.id(); showTab('telemetry'); });
  } else {
    els.forEach((el) => {
      const ele = cy.getElementById(el.data.id);
      if (ele.nonempty()) ele.data(el.data); else cy.add(el);
    });
  }
}

function tooltipHtml(t) {
  const d = t.data('info');
  if (t.isNode()) {
    return `<b>${esc(d.name)}</b>${esc(d.kind)}${d.role ? ` · ${esc(d.role)}` : ''} · ${statusChip(d.status)}<br>
      CPU ${fmt(d.cpu, 0, '%')} · memory ${fmt(d.memory, 0, '%')}
      ${d.trust !== null && d.trust !== undefined ? `<br>trust ${fmt(d.trust, 2)} · ${esc(d.level)}${d.blocked ? ' · blocked' : ''}` : ''}
      ${d.reasons.length ? `<br>${esc(d.reasons.join('; '))}` : ''}`;
  }
  return `<b>${esc(d.name)}</b>${statusChip(d.status)} ${d.rerouted ? chip('carries rerouted traffic', 'reroute') : ''}<br>
    latency ${fmt(d.latency_ms, 2, ' ms')} · loss ${fmt(d.loss_pct, 1, '%')}<br>
    utilization ${fmt(d.utilization_pct, 1, '%')} of ${fmt(d.capacity_mbps, 0, ' Mbps')}
    ${d.prediction !== null && d.prediction !== undefined ? `<br>failure score ${fmt(d.prediction, 2)}${d.predicted ? ' — predicted' : ''}` : ''}
    ${d.reasons.length ? `<br>${esc(d.reasons.join('; '))}` : ''}<br><span class="muted">click for charts</span>`;
}

/* ── overview panels ─────────────────────────────────────────────────────── */
function renderHeader(s) {
  const badge = $('modeBadge');
  badge.textContent = s.mode === 'simulation' ? 'Simulation' : s.mode === 'offline' ? 'Offline preview' : 'Live lab';
  badge.className = `badge ${s.mode === 'simulation' ? 'sim' : s.mode === 'offline' ? 'offline' : 'live'}`;
  const c = s.health_counts;
  $('healthPills').innerHTML = `<span class="pill ok">${c.HEALTHY || 0} healthy</span>
    <span class="pill warn">${c.DEGRADED || 0} degraded</span><span class="pill bad">${c.DOWN || 0} down</span>`;
  $('autonomyBadge').textContent = s.status.autonomy ? `Autonomy ${s.status.autonomy}` : '';
  const banner = $('banner');
  banner.hidden = false;
  if (s.mode === 'simulation') {
    banner.className = 'banner';
    banner.textContent = 'SIMULATION — every value is simulated; failures and attacks are synthetic. Use the Control tab to inject them.';
  } else if (s.mode === 'live') {
    banner.className = 'banner live';
    const learning = s.status && s.status.learning
      ? ` Learning normal traffic for prediction — failures and attacks open in ~${Math.ceil(s.status.warmup_remaining_s || 0)} s.` : '';
    banner.textContent = 'LIVE LAB — Mininet + Open vSwitch + Ryu. Telemetry is measured, attacks are real programs in the hosts, failures change the emulated network.' + learning;
  } else {
    banner.className = 'banner warn';
    banner.textContent = 'OFFLINE PREVIEW — no network is running; telemetry is simulated. Start with VIEWER_MODE=simulation for the full closed loop.';
  }
  document.querySelectorAll('.control-only').forEach((el) => { el.hidden = !s.controls_enabled; });
  const pending = s.decisions ? s.decisions.pending.length : 0;
  $('pendingCount').hidden = pending === 0;
  $('pendingCount').textContent = pending;
}

function renderKpis(s) {
  const nodes = s.topology.nodes.length + s.topology.links.length;
  const healthy = s.health_counts.HEALTHY || 0;
  const heal = s.healing ? s.healing.stats : null;
  const threat = s.security && s.security.threat ? s.security.threat.value : null;
  const alerts = s.prediction ? s.prediction.alerts.length : null;
  const kpi = (label, value, sub, color) => `<div class="kpi"><div class="label">${label}</div>
    <div class="value" style="color:${color || 'inherit'}">${value}</div><div class="sub">${sub}</div></div>`;
  $('kpis').innerHTML = [
    kpi('Healthy elements', `${healthy}/${nodes}`, 'links + devices', healthy === nodes ? COLORS.HEALTHY : COLORS.DEGRADED),
    kpi('Rerouted demands', s.rerouted.length, 'client/server pairs', s.rerouted.length ? '#39c5cf' : null),
    kpi('Healing success', heal && heal.success_rate !== null ? fmt(heal.success_rate * 100, 0, '%') : '—',
        heal ? `${heal.verified}/${heal.executed} verified · ${heal.escalated} escalated` : 'simulation mode only'),
    kpi('Threat index', threat === null ? '—' : fmt(threat, 2), s.security ? `${s.security.quarantined.length} quarantined · ${s.security.blocked.length} blocked` : 'Zero Trust off',
        threat > 0.2 ? COLORS.DOWN : null),
    kpi('Failure alerts', alerts === null ? '—' : alerts, s.prediction ? s.prediction.status.model : 'predictor off',
        alerts ? '#bc8cff' : null),
    kpi('Pending approvals', s.decisions ? s.decisions.pending.length : '—', s.decisions ? `autonomy ${s.status.autonomy || s.decisions.status.autonomy}` : 'decisions off',
        s.decisions && s.decisions.pending.length ? COLORS.DEGRADED : null),
  ].join('');
}

function intentRow(i, buttons = false) {
  return `<div class="row"><span class="time">${hhmmss(i.ts)}</span><span class="what">${chip(i.type, i.type === 'MONITOR' ? '' : 'info')}
    <b>${esc(i.target)}</b> ${stateChip(i.status)}<div class="detail">${esc(i.reason)}${i.approval_reason ? ` — ${esc(i.approval_reason)}` : ''}</div>
    ${buttons ? `<div class="mt"><button class="btn small" data-approve="${esc(i.id)}">Approve</button>
      <button class="btn small reject" data-reject="${esc(i.id)}">Reject</button></div>` : ''}</span></div>`;
}

function renderOverviewLists(s) {
  $('ovDecisions').innerHTML = s.decisions
    ? (s.decisions.active.length ? s.decisions.active.slice(0, 8).map((i) => intentRow(i)).join('') : empty('No active decisions — network is fine.'))
    : empty('Decision engine is off.');
}

/* ── event formatting ───────────────────────────────────────────────────── */
function eventText(e) {
  const d = e.data || {};
  switch (e.type) {
    case 'HEALTH_CHANGED': return `${d.key}: ${d.previous} → ${d.status}${d.reasons && d.reasons.length ? ` (${d.reasons.join('; ')})` : ''}`;
    case 'FAILURE_PREDICTED': return `failure predicted on ${d.target} (score ${fmt(d.score, 2)})`;
    case 'PREDICTION_CLEARED': return `prediction cleared on ${d.target}`;
    case 'DECISION_MADE': case 'APPROVAL_REQUIRED': case 'INTENT_RESOLVED': return `${d.type} ${d.target} — ${d.reason || ''}`;
    case 'HEALING_STARTED': case 'HEALING_VERIFIED': case 'HEALING_FAILED': case 'HEALING_ROLLED_BACK': case 'ESCALATED':
      return `${d.type} ${d.target} (attempt ${d.attempts}) — ${d.detail || ''}`;
    case 'SECURITY_ANOMALY': return `${d.subject}: ${d.kind} — ${d.detail || ''}`;
    case 'ACCESS_DENIED': return `${d.subject}: ${d.count} request(s) denied ${JSON.stringify(d.codes || {})}`;
    case 'TRUST_CHANGED': case 'DEVICE_QUARANTINED': case 'DEVICE_RELEASED':
      return `${d.subject}: ${d.previous} → ${d.level} (trust ${fmt(d.trust, 2)})`;
    default: {
      const target = d.node || d.link || d.target || (d.src ? `${d.src}-${d.dst}` : '') || d.name || '';
      return `${target}`;
    }
  }
}
function categoryOf(type) { return Object.keys(CATEGORIES).find((c) => CATEGORIES[c].includes(type)) || 'other'; }
const CAT_CLASS = { failure: 'bad', health: 'warn', prediction: 'predict', security: 'bad', decision: 'info', healing: 'ok', other: '' };
function eventRow(e) {
  const cat = categoryOf(e.type);
  const times = e.count > 1 ? `<span class="muted small"> ×${e.count} since ${hhmmss(e.firstTime)}</span>` : '';
  return `<div class="row"><span class="time">${hhmmss(e.time)}</span><span class="what">${chip(e.type, CAT_CLASS[cat])}
    ${esc(eventText(e))}${times}</span></div>`;
}
/* Collapse runs of the same event type about the same subject (e.g. an
 * attacker denied every step) into one row with a count. Newest first. */
function eventKey(e) {
  const d = e.data || {};
  return `${e.type}|${d.subject || d.key || d.target || d.node || ''}|${d.kind || ''}`;
}
function collapse(events) {
  const out = [];
  const seen = new Map();
  events.forEach((e) => {
    const k = eventKey(e);
    const prev = seen.get(k);
    if (prev && ['ACCESS_DENIED', 'SECURITY_ANOMALY'].includes(e.type)) {
      prev.count += 1; prev.firstTime = e.time;
    } else {
      const row = { ...e, count: 1, firstTime: e.time };
      seen.set(k, row); out.push(row);
    }
  });
  return out;
}

async function refreshOverviewEvents() {
  try {
    const t = await getJSON('/api/timeline?limit=40');
    $('ovEvents').innerHTML = t.events.length ? collapse(t.events).map(eventRow).join('') : empty('No events yet.');
  } catch (e) { $('ovEvents').innerHTML = empty(e.message); }
}

/* ── telemetry charts (hand-made SVG) ───────────────────────────────────── */
function fillTargets(s) {
  const sel = $('seriesTarget');
  if (sel.options.length) return;
  const opt = (v, label) => `<option value="${esc(v)}">${esc(label)}</option>`;
  sel.innerHTML = `<optgroup label="Links">${s.topology.links.map((l) => opt(l.name, l.name)).join('')}</optgroup>
    <optgroup label="Devices">${s.topology.nodes.map((n) => opt(n.name, `${n.name} (${n.kind})`)).join('')}</optgroup>`;
  sel.value = 'sw-a-sw-b';
  if (!sel.value) sel.selectedIndex = 0;
  sel.addEventListener('change', refreshTab);
}

function lineChart(points, opts) {
  const W = 360, H = 150, P = { l: 40, r: 8, t: 8, b: 20 };
  if (!points.length) return `<svg viewBox="0 0 ${W} ${H}"><text x="${W / 2}" y="${H / 2}" fill="#6e7681" text-anchor="middle">no data yet</text></svg>`;
  const xs = points.map((p) => p[0]); const ys = points.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs, x0 + 1);
  let y0 = Math.min(0, ...ys), y1 = Math.max(...ys, opts.threshold ?? -Infinity, opts.binary ? 1 : 1e-9);
  if (y1 === y0) y1 = y0 + 1;
  y1 *= 1.1;
  const X = (v) => P.l + ((v - x0) / (x1 - x0)) * (W - P.l - P.r);
  const Y = (v) => H - P.b - ((v - y0) / (y1 - y0)) * (H - P.t - P.b);
  const path = points.map((p, i) => `${i ? 'L' : 'M'}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join('');
  const area = `${path}L${X(xs[xs.length - 1]).toFixed(1)},${Y(y0)}L${X(xs[0]).toFixed(1)},${Y(y0)}Z`;
  const ticks = [y0, (y0 + y1) / 2, y1].map((v) => `<text x="${P.l - 5}" y="${Y(v) + 4}" fill="#6e7681" font-size="10" text-anchor="end">${fmt(v, v < 10 ? 1 : 0)}</text>
      <line x1="${P.l}" x2="${W - P.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="#21262d"/>`).join('');
  const thr = opts.threshold !== null && opts.threshold !== undefined && opts.threshold < y1
    ? `<line x1="${P.l}" x2="${W - P.r}" y1="${Y(opts.threshold)}" y2="${Y(opts.threshold)}" stroke="#f85149" stroke-dasharray="4 3"/>
       <text x="${W - P.r - 2}" y="${Y(opts.threshold) - 3}" fill="#f85149" font-size="10" text-anchor="end">threshold ${opts.threshold}</text>` : '';
  const span = Math.round(x1 - x0);
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${ticks}
    <path d="${area}" fill="${opts.color}22"/><path d="${path}" fill="none" stroke="${opts.color}" stroke-width="1.8"/>${thr}
    <text x="${P.l}" y="${H - 5}" fill="#6e7681" font-size="10">−${span}s</text>
    <text x="${W - P.r}" y="${H - 5}" fill="#6e7681" font-size="10" text-anchor="end">now</text></svg>`;
}

async function refreshTelemetry() {
  const target = $('seriesTarget').value;
  if (!target) return;
  const kind = state && state.topology.links.some((l) => l.name === target) ? 'link' : 'node';
  const metrics = CHART_METRICS[kind];
  try {
    const r = await getJSON(`/api/series?target=${encodeURIComponent(target)}&metrics=${metrics.map((m) => m[0]).join(',')}&limit=180`);
    $('seriesNote').textContent = 'Simulated telemetry · last ~180 collections · red dashed line = health threshold';
    $('charts').innerHTML = metrics.map(([m, label, unit, thrKey]) => {
      const pts = r.series[m] || [];
      const last = pts.length ? pts[pts.length - 1][1] : null;
      const color = m.endsWith('status') ? '#3fb950' : m.includes('loss') ? '#f85149' : m.includes('latency') ? '#bc8cff' : '#58a6ff';
      return `<div class="card chart"><h3>${esc(label)} <span class="now">${fmt(last, m.endsWith('status') ? 0 : 2, unit ? ` ${unit}` : '')}</span></h3>
        ${lineChart(pts, { threshold: thrKey ? r.thresholds[thrKey] : null, color, binary: m.endsWith('status') })}</div>`;
    }).join('');
  } catch (e) { $('charts').innerHTML = empty(e.message); }
}

/* ── predictions ─────────────────────────────────────────────────────────── */
function renderPredictions(s) {
  if (!s.prediction) {
    $('predInfo').textContent = s.mode === 'live'
      ? 'The predictor is learning what normal looks like on the live network; it starts after the warm-up.'
      : 'No predictor is running (simulation mode trains one at start-up; otherwise set VIEWER_MODEL_DIR).';
    $('predTable').innerHTML = '';
    return;
  }
  const st = s.prediction.status;
  $('predInfo').textContent = `Model ${st.model} · alarm threshold ${fmt(st.threshold, 3)} · ` +
    (s.mode === 'live' ? 'trained on this live network\'s normal traffic' : 'trained on simulated data with synthetic warning patterns');
  const rows = [...s.topology.links].sort((a, b) => (b.prediction || 0) - (a.prediction || 0));
  $('predTable').innerHTML = `<table><tr><th>Link</th><th>Health</th><th>Failure score</th><th></th><th>Status</th></tr>
    ${rows.map((l) => `<tr><td>${esc(l.name)}</td><td>${statusChip(l.status)}</td>
      <td style="width:45%"><div class="bar"><i style="width:${Math.round((l.prediction || 0) * 100)}%;background:${l.predicted ? '#bc8cff' : '#58a6ff'}"></i></div></td>
      <td class="num">${l.prediction === null ? '—' : fmt(l.prediction, 2)}</td>
      <td>${l.status === 'DOWN' ? chip('down — not scored') : l.predicted ? chip('FAILURE PREDICTED', 'predict') : chip('normal', 'ok')}</td></tr>`).join('')}</table>`;
}

/* ── zero trust ──────────────────────────────────────────────────────────── */
function renderSecurity(s) {
  if (!s.security) {
    $('trustBars').innerHTML = empty('Zero Trust is off (simulation mode, or VIEWER_SIMULATE_SECURITY=1).');
    $('threat').innerHTML = ''; $('secEvents').innerHTML = '';
    return;
  }
  $('trustBars').innerHTML = s.security.trust.map((t) => `<div class="trust-row"><b>${esc(t.device)}</b>
    <div class="bar"><i style="width:${Math.round(t.trust * 100)}%;background:${LEVEL_COLORS[t.level]}"></i></div>
    <span class="num">${fmt(t.trust, 2)}</span><span>${chip(t.level, { TRUSTED: 'ok', RESTRICTED: 'warn', QUARANTINED: 'bad' }[t.level])}
    ${s.security.blocked.includes(t.device) ? chip('blocked', 'bad') : ''}</span></div>`).join('') || empty('No trust data yet.');
  const th = s.security.threat;
  $('threat').innerHTML = th ? `<div class="stats-row">
      <div class="stat"><b>${fmt(th.value, 2)}</b><span>threat index</span></div>
      <div class="stat"><b>${fmt(th.h_flow, 2)}</b><span>H_flow (entropy drop)</span></div>
      <div class="stat"><b>${fmt(th.r_auth, 2)}</b><span>R_auth (failed logins)</span></div>
      <div class="stat"><b>${fmt(th.f_anom, 2)}</b><span>F_anom (anomalous devices)</span></div></div>` : empty('No data yet.');
  $('secEvents').innerHTML = s.security.events.length
    ? collapse(s.security.events.map((e) => ({ type: e.type, time: e.ts, data: e.data }))).map(eventRow).join('')
    : empty('No security events.');
}

/* ── decisions & healing ─────────────────────────────────────────────────── */
function renderHealing(s) {
  if (!s.decisions) {
    $('pending').innerHTML = empty('Decision engine is off.');
    $('activeIntents').innerHTML = ''; $('healStats').innerHTML = ''; $('healActions').innerHTML = '';
    return;
  }
  $('pendingCard').hidden = false;
  $('pending').innerHTML = s.decisions.pending.length
    ? s.decisions.pending.map((i) => intentRow(i, s.controls_enabled)).join('') : empty('Nothing is waiting for approval.');
  $('activeIntents').innerHTML = s.decisions.active.length
    ? `<table><tr><th>Action</th><th>Target</th><th>Risk</th><th>Status</th></tr>${s.decisions.active.map((i) => `<tr>
      <td>${chip(i.type, i.type === 'MONITOR' ? '' : 'info')}</td><td>${esc(i.target)}<div class="detail">${esc(i.reason)}</div></td>
      <td class="num">${fmt(i.risk, 2)}</td><td>${stateChip(i.status)}</td></tr>`).join('')}</table>` : empty('No active decisions.');
  if (!s.healing) {
    $('healStats').innerHTML = ''; $('healActions').innerHTML = empty('Self-healing runs in simulation and live lab mode.');
    return;
  }
  const st = s.healing.stats;
  $('healStats').innerHTML = `<div class="stat"><b>${st.executed}</b><span>executed</span></div>
    <div class="stat"><b>${st.verified}</b><span>verified</span></div>
    <div class="stat"><b>${st.success_rate === null ? '—' : fmt(st.success_rate * 100, 0, '%')}</b><span>success</span></div>
    <div class="stat"><b>${st.rollbacks}</b><span>rolled back</span></div>
    <div class="stat"><b>${st.escalated}</b><span>escalated</span></div>`;
  $('healActions').innerHTML = s.healing.actions.length
    ? `<table><tr><th>Started</th><th>Action</th><th>State</th></tr>${s.healing.actions.map((a) => `<tr>
      <td class="num">${hhmmss(a.started)}</td><td>${chip(a.type, 'info')} ${esc(a.target)}
      <div class="detail">${esc(a.detail)}</div>${a.checks.length ? `<div class="detail">last check: ${esc(a.checks[a.checks.length - 1])}</div>` : ''}</td>
      <td>${stateChip(a.state)}${a.attempts > 1 ? ` <span class="muted small">×${a.attempts}</span>` : ''}</td></tr>`).join('')}</table>`
    : empty('No healing actions yet.');
}

document.addEventListener('click', async (e) => {
  const approve = e.target.dataset && e.target.dataset.approve;
  const reject = e.target.dataset && e.target.dataset.reject;
  if (!approve && !reject) return;
  const id = approve || reject;
  if (!confirm(`${approve ? 'Approve' : 'Reject'} ${id}?`)) return;
  try { await postJSON(`/api/decisions/${encodeURIComponent(id)}/${approve ? 'approve' : 'reject'}`); poll(); }
  catch (err) { alert(err.message); }
});

/* ── timeline ────────────────────────────────────────────────────────────── */
function renderTimelineFilters() {
  $('timelineFilters').innerHTML = Object.keys(CATEGORIES).map((c) => `<label class="check"><input type="checkbox" data-cat="${c}"
    ${timelineFilter.has(c) ? 'checked' : ''}> ${chip(c, CAT_CLASS[c])}</label>`).join('');
  $('timelineFilters').querySelectorAll('input').forEach((i) => i.addEventListener('change', () => {
    if (i.checked) timelineFilter.add(i.dataset.cat); else timelineFilter.delete(i.dataset.cat);
    refreshTimeline();
  }));
}
async function refreshTimeline() {
  const types = [...timelineFilter].flatMap((c) => CATEGORIES[c]).join(',');
  try {
    const t = await getJSON(`/api/timeline?limit=400&types=${types}`);
    $('timeline').innerHTML = t.events.length ? collapse(t.events).map(eventRow).join('') : empty('No events for these filters.');
  } catch (e) { $('timeline').innerHTML = empty(e.message); }
}

/* ── analytics ───────────────────────────────────────────────────────────── */
async function refreshAnalytics() {
  try {
    const r = await getJSON(`/api/analytics/summary?window_s=${$('anaWindow').value}`);
    const s = r.summary;
    if (!s) { $('analytics').innerHTML = empty('No data recorded yet.'); return; }
    const inc = s.incidents;
    const stat = (v, label) => `<div class="stat"><b>${v}</b><span>${label}</span></div>`;
    const avail = (rows) => rows.targets.map((t) => `<tr><td>${esc(t.target)}</td><td class="num">${fmt(t.availability_pct, 1, '%')}</td>
      <td style="width:40%"><div class="bar"><i style="width:${t.availability_pct || 0}%;background:${(t.availability_pct || 0) >= 99 ? '#3fb950' : '#d29922'}"></i></div></td></tr>`).join('');
    $('analytics').innerHTML = `<div class="card"><h3>Window summary (${esc(s.simulated)} data)</h3><div class="stats-row">
        ${stat(fmt(s.links.availability_pct, 2, '%'), 'link availability')}${stat(fmt(s.nodes.availability_pct, 2, '%'), 'device availability')}
        ${stat(inc.down.count, 'outages')}${stat(inc.down.mttr_s === null ? '—' : fmt(inc.down.mttr_s, 0, ' s'), 'MTTR (outages)')}
        ${stat(inc.degraded.count, 'degradations')}${stat(fmt(inc.down.total_duration_s, 0, ' s'), 'total downtime')}</div></div>
      <div class="two-col"><div class="card"><h3>Link availability</h3><table>${avail(s.links)}</table></div>
      <div class="card"><h3>Device availability</h3><table>${avail(s.nodes)}</table></div></div>`;
  } catch (e) {
    $('analytics').innerHTML = empty(e.status === 404 ? 'History recording is off (simulation mode records automatically; otherwise set VIEWER_DB_PATH).' : e.message);
  }
}
$('anaWindow').addEventListener('change', refreshAnalytics);

/* ── control (simulation only) ───────────────────────────────────────────── */
function fillControlForms(s) {
  const form = $('failForm');
  const kindSel = form.elements.kind;
  const targetSel = form.elements.target;
  const fill = () => {
    const kind = kindSel.value;
    let opts;
    if (kind === 'switch' || kind === 'router') opts = s.topology.nodes.filter((n) => n.kind === kind).map((n) => [n.name, n.name]);
    else opts = s.topology.links.map((l) => [`${l.src},${l.dst}`, l.name]);
    targetSel.innerHTML = opts.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('');
    form.elements.physical.disabled = !['link', 'switch', 'router'].includes(kind);
    if (form.elements.physical.disabled) form.elements.physical.checked = false;
  };
  if (!form.dataset.ready) { kindSel.addEventListener('change', fill); form.dataset.ready = '1'; fill(); }
  const attacker = $('attackForm').elements.attacker;
  if (!attacker.options.length) {
    attacker.innerHTML = s.topology.nodes.filter((n) => n.kind === 'host').map((n) => `<option>${esc(n.name)}</option>`).join('');
    attacker.value = 'h5';
  }
  document.querySelectorAll('#autonomy button').forEach((b) => b.classList.toggle('active', b.dataset.level === s.status.autonomy));
  $('injections').innerHTML = s.injections.length
    ? `<table><tr><th>When</th><th>What</th><th>Target</th><th>State</th></tr>${s.injections.map((i) => `<tr>
      <td class="num">${hhmmss(i.start)}</td><td>${chip(i.type, i.type === 'attack' ? 'bad' : 'warn')} ${esc(i.kind)}
      ${i.physical ? chip('physical', 'bad') : ''}${i.warning_s ? chip(`${i.warning_s}s warning`, 'predict') : ''}</td>
      <td>${esc(i.target.join('-'))}</td><td>${chip(i.state, i.state === 'active' ? 'bad' : i.state === 'warning' ? 'predict' : '')}</td></tr>`).join('')}</table>`
    : empty('Nothing injected yet.');
}
function controlMsg(text, ok) { const m = $('controlMsg'); m.textContent = text; m.className = `msg ${ok ? 'ok' : 'err'}`; }
$('failForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target.elements;
  const body = { kind: f.kind.value, target: f.target.value.split(','), duration_s: Number(f.duration_s.value),
    warning_s: Number(f.warning_s.value), physical: f.physical.checked };
  if (!confirm(`Inject ${body.kind} failure on ${body.target.join('-')} for ${body.duration_s}s?`)) return;
  try { const r = await postJSON('/api/sim/failure', body); controlMsg(`Scheduled ${r.id}`, true); poll(); }
  catch (err) { controlMsg(err.message, false); }
});
$('attackForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target.elements;
  const body = { kind: f.kind.value, attacker: f.attacker.value, duration_s: Number(f.duration_s.value) };
  if (!confirm(`Launch ${body.kind} from ${body.attacker} for ${body.duration_s}s?`)) return;
  try { const r = await postJSON('/api/sim/attack', body); controlMsg(`Launched ${r.id}`, true); poll(); }
  catch (err) { controlMsg(err.message, false); }
});
document.querySelectorAll('#autonomy button').forEach((b) => b.addEventListener('click', async () => {
  if (!confirm(`Set autonomy to ${b.dataset.level}?`)) return;
  try { await postJSON('/api/sim/autonomy', { level: b.dataset.level }); controlMsg(`Autonomy ${b.dataset.level}`, true); poll(); }
  catch (err) { controlMsg(err.message, false); }
}));
$('demoBtn').addEventListener('click', async () => {
  if (!confirm('Run the scripted demo (about 7 minutes)?')) return;
  try { const r = await postJSON('/api/sim/demo'); controlMsg(`Demo scheduled: ${r.scheduled.length} events`, true); showTab('overview'); }
  catch (err) { controlMsg(err.message, false); }
});

/* ── polling ─────────────────────────────────────────────────────────────── */
function refreshTab() {
  if (activeTab === 'telemetry') refreshTelemetry();
  if (activeTab === 'timeline') refreshTimeline();
  if (activeTab === 'analytics') refreshAnalytics();
}

async function poll() {
  try {
    state = await getJSON('/api/overview');
  } catch (e) {
    const banner = $('banner'); banner.hidden = false; banner.className = 'banner warn';
    banner.textContent = `Cannot reach the dashboard API: ${e.message}`;
    return;
  }
  renderHeader(state);
  renderTopology(state.topology);
  renderKpis(state);
  renderOverviewLists(state);
  fillTargets(state);
  renderPredictions(state);
  renderSecurity(state);
  renderHealing(state);
  if (state.controls_enabled) fillControlForms(state);
  if (activeTab === 'overview') refreshOverviewEvents();
  else refreshTab();
}

renderTimelineFilters();
poll();
setInterval(poll, POLL_MS);
