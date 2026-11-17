/* Self-Healing Network Dashboard (Module 8). Plain JS, no build step.
 * Polls /api/overview every POLL_MS; other views load their data when shown.
 *
 * Rendering rules that keep the UI calm while it refreshes every 2 s:
 *  - setHTML() only touches the DOM when the markup really changed, so hover
 *    states, entrance animations and scroll positions survive polling;
 *  - entrance animations (stagger) run the first time a container fills;
 *  - bars animate from their previous width, KPI numbers count up/down;
 *  - everything animated respects prefers-reduced-motion. */

const POLL_MS = 2000;
const $ = (id) => document.getElementById(id);
const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)');
const COLORS = { HEALTHY: '#34d399', DEGRADED: '#fbbf24', DOWN: '#f87171', UNKNOWN: '#5d6684' };
const LEVEL_COLORS = { TRUSTED: '#34d399', RESTRICTED: '#fbbf24', QUARANTINED: '#f87171' };
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
const TAB_TITLES = { overview: 'Overview', telemetry: 'Telemetry', prediction: 'Failure prediction', security: 'Zero Trust',
  healing: 'Decisions & healing', timeline: 'Timeline', analytics: 'Analytics', control: 'Control' };

let state = null;
let cy = null;
let activeTab = 'overview';
let timelineFilter = new Set(Object.keys(CATEGORIES));
let timelineEvents = [];

/* ── icons (stroke icons, 24×24, lucide-style paths) ─────────────────────── */
const ICONS = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  sparkles: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
  shield: '<path d="M12 2l8 4v6c0 5-3.4 8.6-8 10-4.6-1.4-8-5-8-10V6l8-4z"/>',
  'shield-check': '<path d="M12 2l8 4v6c0 5-3.4 8.6-8 10-4.6-1.4-8-5-8-10V6l8-4z"/><path d="M8.5 12.5l2.5 2.5 4.5-5"/>',
  'shield-alert': '<path d="M12 2l8 4v6c0 5-3.4 8.6-8 10-4.6-1.4-8-5-8-10V6l8-4z"/><path d="M12 8v5M12 16.5v.01"/>',
  wrench: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18v3h3l6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4z"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  chart: '<path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 6-7"/>',
  sliders: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
  'chevrons-left': '<path d="M11 17l-5-5 5-5M18 17l-5-5 5-5"/>',
  menu: '<path d="M3 6h18M3 12h18M3 18h18"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/>',
  network: '<rect x="9" y="2" width="6" height="6" rx="1.5"/><rect x="2" y="16" width="6" height="6" rx="1.5"/><rect x="16" y="16" width="6" height="6" rx="1.5"/><path d="M12 8v4M5 16v-2h14v2"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  gauge: '<path d="M12 14l4-4"/><path d="M3.3 19a10 10 0 1 1 17.4 0"/>',
  alert: '<path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
  hand: '<path d="M18 11V6a2 2 0 0 0-4 0v5M14 10V4a2 2 0 0 0-4 0v6M10 10.5V6a2 2 0 0 0-4 0v8"/><path d="M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.9-6-2.4l-3.6-3.6a2 2 0 0 1 2.8-2.8L7 15"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
  zap: '<path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>',
  crosshair: '<circle cx="12" cy="12" r="9"/><path d="M22 12h-4M6 12H2M12 6V2M12 22v-4"/>',
  cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/>',
  play: '<path d="M6 4l14 8-14 8z"/>',
  history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l4 2"/>',
  check: '<path d="M20 6L9 17l-5-5"/>',
  'check-circle': '<circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/>',
  x: '<path d="M18 6L6 18M6 6l12 12"/>',
  'x-circle': '<circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/>',
  route: '<circle cx="6" cy="19" r="3"/><circle cx="18" cy="5" r="3"/><path d="M12 19h4.5a3.5 3.5 0 0 0 0-7h-9a3.5 3.5 0 0 1 0-7H12"/>',
  heart: '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8z"/>',
  laptop: '<rect x="4" y="5" width="16" height="11" rx="1.5"/><path d="M2 19h20"/>',
  server: '<rect x="3" y="3" width="18" height="7" rx="1.5"/><rect x="3" y="14" width="18" height="7" rx="1.5"/><path d="M7 6.5h.01M7 17.5h.01"/>',
  inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5.1L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1z"/>',
  wifi: '<path d="M5 12.6a10 10 0 0 1 14 0M8.5 16.1a5 5 0 0 1 7 0M12 19.5h.01M1.4 9a16 16 0 0 1 21.2 0"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
};
function icon(name, cls = 'i') {
  return `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ICONS.info}</svg>`;
}
function hydrateIcons(root = document) {
  root.querySelectorAll('[data-icon]').forEach((el) => {
    if (el.dataset.iconDone) return;
    el.insertAdjacentHTML('afterbegin', icon(el.dataset.icon));
    el.dataset.iconDone = '1';
  });
}

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
const STATE_CLASS = { VERIFIED: 'ok', FAILED_BACK: 'ok', VERIFYING: 'info', EXECUTING: 'info', RETRY: 'warn',
  ROLLED_BACK: 'warn', ESCALATED: 'bad', FAILED: 'bad', APPROVED: 'ok', PENDING_APPROVAL: 'warn' };
function stateChip(s) { return chip(s, STATE_CLASS[s] || ''); }
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
function empty(text, kind = '', iconName = 'inbox') {
  return `<div class="empty ${kind}"><span class="e-icon">${icon(iconName)}</span><span>${esc(text)}</span></div>`;
}
function skeleton(lines = 4) {
  return Array.from({ length: lines }, (_, i) => `<div class="skel skel-line" style="width:${92 - (i % 3) * 14}%"></div>`).join('');
}
/* Replace markup only when it changed; first fill of a container staggers in. */
const filled = new WeakSet();
function setHTML(el, html, stagger = true) {
  if (!el || el._html === html) return false;
  el._html = html;
  el.innerHTML = html;
  if (stagger && !filled.has(el) && !html.includes('class="skel')) {
    filled.add(el);
    el.classList.add('stagger');
    [...el.children].forEach((c, i) => c.style.setProperty('--i', Math.min(i, 12)));
    setTimeout(() => el.classList.remove('stagger'), 1200);
  }
  return true;
}
/* Bars: render at their previous width, then grow/shrink to the new one. */
const barWidths = new Map();
function bar(key, pct, color) {
  const prev = barWidths.has(key) ? barWidths.get(key) : 0;
  const w = Math.max(0, Math.min(100, pct));
  return `<div class="bar"><i data-bar="${esc(key)}" data-w="${w}" style="width:${prev}%;background:${color}"></i></div>`;
}
function settleBars(root) {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    root.querySelectorAll('[data-bar]').forEach((b) => { b.style.width = `${b.dataset.w}%`; barWidths.set(b.dataset.bar, Number(b.dataset.w)); });
  }));
}
/* Count-up for numbers. */
function countTo(el, to, digits = 0, suffix = '') {
  if (to === null || to === undefined || Number.isNaN(to)) { el.textContent = '—'; el._v = null; return; }
  const from = el._v ?? 0;
  el._v = to;
  if (REDUCED.matches || from === to) { el.textContent = `${Number(to).toFixed(digits)}${suffix}`; return; }
  const t0 = performance.now();
  const dur = 650;
  const step = (now) => {
    const p = Math.min(1, (now - t0) / dur);
    const e = 1 - Math.pow(1 - p, 3);
    el.textContent = `${(from + (to - from) * e).toFixed(digits)}${suffix}`;
    if (p < 1 && el._v === to) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/* ── dialogs & toasts (replace window.confirm / alert) ──────────────────── */
function confirmDialog({ title, body, ok = 'Confirm', tone = 'primary', iconName = 'info' }) {
  return new Promise((resolve) => {
    const root = $('modal');
    const okBtn = $('modalOk');
    const cancelBtn = $('modalCancel');
    const prevFocus = document.activeElement;
    $('modalTitle').textContent = title;
    $('modalBody').textContent = body;
    $('modalIcon').className = `modal-icon ${tone === 'danger' ? 'danger' : tone === 'warn' ? 'warn' : ''}`;
    $('modalIcon').innerHTML = icon(iconName);
    okBtn.textContent = ok;
    okBtn.className = `btn ${tone === 'danger' ? 'danger' : 'primary'}`;
    root.hidden = false;
    root.classList.remove('closing');
    okBtn.focus();
    const close = (result) => {
      root.classList.add('closing');
      document.removeEventListener('keydown', onKey, true);
      setTimeout(() => { root.hidden = true; root.classList.remove('closing'); if (prevFocus) prevFocus.focus(); }, REDUCED.matches ? 0 : 150);
      resolve(result);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); close(false); }
      if (e.key === 'Tab') { e.preventDefault(); (document.activeElement === okBtn ? cancelBtn : okBtn).focus(); }
    };
    document.addEventListener('keydown', onKey, true);
    okBtn.onclick = () => close(true);
    cancelBtn.onclick = () => close(false);
    root.querySelector('[data-close]').onclick = () => close(false);
  });
}
function toast(title, text, ok = true) {
  const t = document.createElement('div');
  t.className = `toast ${ok ? 'ok' : 'err'}`;
  t.setAttribute('role', ok ? 'status' : 'alert');
  t.innerHTML = `<span class="t-icon">${icon(ok ? 'check' : 'x')}</span><div><b>${esc(title)}</b><span>${esc(text || '')}</span></div>`;
  $('toasts').appendChild(t);
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 260); }, ok ? 3800 : 6500);
}
async function withBusy(btn, fn) {
  btn.disabled = true;
  try { return await fn(); } finally { btn.disabled = false; }
}

/* ── shell: sidebar, tabs, notifications ─────────────────────────────────── */
const app = $('app');
function storeGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function storeSet(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } }
if (storeGet('sh.sidebar') === 'collapsed') app.classList.add('collapsed');
$('collapseBtn').addEventListener('click', () => {
  app.classList.toggle('collapsed');
  storeSet('sh.sidebar', app.classList.contains('collapsed') ? 'collapsed' : 'open');
  setTimeout(refitTopology, 260);
});
function setNav(open) {
  app.classList.toggle('nav-open', open);
  $('scrim').hidden = !open;
  $('menuBtn').setAttribute('aria-expanded', String(open));
}
$('menuBtn').addEventListener('click', () => setNav(true));
$('scrim').addEventListener('click', () => setNav(false));

const tabs = [...document.querySelectorAll('.tab')];
tabs.forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
$('tabs').addEventListener('keydown', (e) => {
  if (!['ArrowDown', 'ArrowUp'].includes(e.key)) return;
  const visible = tabs.filter((t) => !t.hidden);
  const i = visible.indexOf(document.activeElement);
  if (i < 0) return;
  e.preventDefault();
  visible[(i + (e.key === 'ArrowDown' ? 1 : -1) + visible.length) % visible.length].focus();
});
document.addEventListener('click', (e) => {
  const go = e.target.closest('[data-goto]');
  if (go) showTab(go.dataset.goto);
});
function showTab(name) {
  if (!TAB_TITLES[name]) name = 'overview';
  activeTab = name;
  tabs.forEach((b) => { const on = b.dataset.tab === name; b.classList.toggle('active', on); b.setAttribute('aria-selected', String(on)); });
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${name}`));
  $('pageTitle').textContent = TAB_TITLES[name];
  if (location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
  setNav(false);
  if (name === 'overview') refitTopology();
  if (name === 'control') positionSegment();
  refreshTab();
}

let notifSeen = '';
function toggleNotif(open) {
  const panel = $('notifPanel');
  const btn = $('notifBtn');
  if (open) {
    panel.hidden = false; panel.classList.remove('closing'); btn.setAttribute('aria-expanded', 'true');
    notifSeen = notifSignature(); $('notifDot').hidden = true;
  } else if (!panel.hidden) {
    panel.classList.add('closing'); btn.setAttribute('aria-expanded', 'false');
    setTimeout(() => { panel.hidden = true; panel.classList.remove('closing'); }, REDUCED.matches ? 0 : 140);
  }
}
$('notifBtn').addEventListener('click', (e) => { e.stopPropagation(); toggleNotif($('notifPanel').hidden); });
document.addEventListener('click', (e) => { if (!e.target.closest('.notif')) toggleNotif(false); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { toggleNotif(false); setNav(false); } });
$('notifList').addEventListener('click', (e) => {
  const item = e.target.closest('[data-goto]');
  if (item) toggleNotif(false);
});
function notifItems(s) {
  const items = [];
  (s.decisions ? s.decisions.pending : []).forEach((i) => items.push({ tone: 'warn', ic: 'hand', goto: 'healing',
    title: `Approve ${i.type} on ${i.target}?`, text: i.reason || 'waiting for an operator' }));
  (s.security ? s.security.quarantined : []).forEach((d) => items.push({ tone: 'bad', ic: 'shield-alert', goto: 'security',
    title: `${d} is quarantined`, text: 'all traffic blocked except its gateway' }));
  (s.prediction ? s.prediction.alerts : []).forEach((p) => items.push({ tone: 'predict', ic: 'sparkles', goto: 'prediction',
    title: `Failure predicted on ${p.target}`, text: `score ${fmt(p.score, 2)}` }));
  [...s.topology.links, ...s.topology.nodes].filter((x) => x.status === 'DOWN').forEach((x) => items.push({ tone: 'bad', ic: 'x-circle',
    goto: 'overview', title: `${x.name} is down`, text: (x.reasons || []).join('; ') || 'not carrying traffic' }));
  return items;
}
function notifSignature() { return state ? notifItems(state).map((i) => i.title).join('|') : ''; }
const TONE_BG = { warn: 'rgba(251,191,36,.14);color:#fbbf24', bad: 'rgba(248,113,113,.14);color:#f87171',
  predict: 'rgba(192,132,252,.14);color:#c084fc', ok: 'rgba(52,211,153,.14);color:#34d399' };
function renderNotifications(s) {
  const items = notifItems(s);
  $('notifSub').textContent = items.length ? `${items.length} item${items.length > 1 ? 's' : ''}` : '';
  setHTML($('notifList'), items.length ? items.map((i) => `<div class="notif-item" data-goto="${i.goto}" role="button" tabindex="0">
      <span class="ni-icon" style="background:${TONE_BG[i.tone]}">${icon(i.ic)}</span>
      <div><b>${esc(i.title)}</b><span>${esc(i.text)}</span></div></div>`).join('')
    : empty('All clear — nothing needs your attention.', 'ok', 'check-circle'), false);
  $('notifDot').hidden = !items.length || notifSignature() === notifSeen || !$('notifPanel').hidden;
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

const KIND_FILL = { router: '#6d7cff', switch: '#3b82f6', server: '#f59e0b', host: '#64748b' };
function nodeStyle(n) {
  const size = { router: 40, switch: 32, server: 26, host: 22 }[n.kind] || 22;
  let fill = KIND_FILL[n.kind] || '#64748b';
  let border = n.status === 'HEALTHY' ? '#c7d2fe' : (COLORS[n.status] || COLORS.UNKNOWN);
  let bw = n.status === 'HEALTHY' ? 2 : 4;
  if (n.status === 'DOWN') fill = '#2a3150';
  if (n.level === 'QUARANTINED' || n.blocked) { border = '#f87171'; fill = '#4c1d24'; bw = 4; }
  else if (n.level === 'RESTRICTED') { border = '#fbbf24'; bw = 3; }
  const label = n.blocked || n.level === 'QUARANTINED' ? `${n.name} ⊘` : n.name;
  return { size, fill, border, bw, label };
}
function edgeStyle(l) {
  let color = l.status === 'HEALTHY' ? '#5b7bb4' : (COLORS[l.status] || COLORS.UNKNOWN);
  if (l.rerouted && l.status === 'HEALTHY') color = '#22d3ee';
  const width = 2.5 + Math.min(8, (l.utilization_pct || 0) / 12);
  return { color, width, style: l.status === 'DOWN' || l.rerouted ? 'dashed' : 'solid', glow: l.predicted ? 7 : 0 };
}

function refitTopology() {
  if (!cy) return;
  requestAnimationFrame(() => { cy.resize(); cy.fit(undefined, 34); });
}
let dashOffset = 0;
function animateReroutes() {
  if (cy && activeTab === 'overview' && !REDUCED.matches && !document.hidden) {
    dashOffset = (dashOffset + 1) % 1000;
    cy.edges('[?moving]').style('line-dash-offset', -dashOffset);
  }
  setTimeout(() => requestAnimationFrame(animateReroutes), 40);
}

function renderTopology(topo) {
  if (!window.cytoscape) {
    setHTML($('cy'), empty('The topology map needs internet access to load Cytoscape.', 'err', 'wifi'));
    return;
  }
  const pos = layout(topo.nodes, topo.links);
  const els = [];
  topo.nodes.forEach((n) => {
    const s = nodeStyle(n);
    els.push({ group: 'nodes', data: { id: n.name, label: s.label, size: s.size, fill: s.fill, border: s.border, bw: s.bw, info: n },
               position: pos[n.name] });
  });
  topo.links.forEach((l) => {
    const s = edgeStyle(l);
    els.push({ group: 'edges', data: { id: l.name, source: l.src, target: l.dst, color: s.color, width: s.width,
               lstyle: s.style, glow: s.glow, moving: Boolean(l.rerouted && l.status !== 'DOWN'), info: l } });
  });
  if (!cy) {
    cy = cytoscape({
      container: $('cy'), elements: els, layout: { name: 'preset' }, minZoom: 0.4, maxZoom: 2.5,
      // fixed view: the mouse wheel scrolls the page instead of zooming; the graph auto-fits
      boxSelectionEnabled: false, autoungrabify: true, userZoomingEnabled: false, userPanningEnabled: false,
      style: [
        { selector: 'node', style: { label: 'data(label)', 'font-size': 11.5, 'font-weight': 600, color: '#c9d1ec',
            'font-family': 'Inter, -apple-system, Segoe UI, system-ui, sans-serif', 'text-valign': 'bottom', 'text-margin-y': 8,
            'text-background-color': '#0a0e1a', 'text-background-opacity': 0.75, 'text-background-padding': 3,
            'text-background-shape': 'roundrectangle', 'background-color': 'data(fill)', 'border-width': 'data(bw)',
            'border-color': 'data(border)', 'border-opacity': 0.95, width: 'data(size)', height: 'data(size)',
            'transition-property': 'background-color, border-color, border-width', 'transition-duration': '400ms' } },
        { selector: 'node:active', style: { 'overlay-opacity': 0 } },
        { selector: 'edge', style: { 'curve-style': 'bezier', width: 'data(width)', 'line-color': 'data(color)', 'line-opacity': 0.85,
            'line-style': 'data(lstyle)', 'line-dash-pattern': [9, 7], 'underlay-color': '#c084fc', 'underlay-padding': 'data(glow)',
            'underlay-opacity': 0.4,
            'transition-property': 'line-color, width', 'transition-duration': '400ms' } },
        { selector: 'node.hover', style: { 'underlay-color': '#6d7cff', 'underlay-padding': 9, 'underlay-opacity': 0.25 } },
        { selector: 'edge.hover', style: { 'line-opacity': 1, width: (e) => e.data('width') + 2 } },
      ],
    });
    // the container's final size is only known after layout: fit then, and on resize
    refitTopology();
    setTimeout(refitTopology, 300);
    window.addEventListener('resize', refitTopology);
    const tip = $('tooltip');
    cy.on('mouseover', 'node, edge', (e) => { e.target.addClass('hover'); tip.innerHTML = tooltipHtml(e.target); tip.hidden = false; $('cy').style.cursor = 'pointer'; });
    cy.on('mousemove', (e) => {
      const x = e.originalEvent.clientX; const y = e.originalEvent.clientY;
      tip.style.left = `${Math.min(x + 16, window.innerWidth - 340)}px`; tip.style.top = `${y + 14}px`;
    });
    cy.on('mouseout', 'node, edge', (e) => { e.target.removeClass('hover'); tip.hidden = true; $('cy').style.cursor = ''; });
    cy.on('tap', 'node, edge', (e) => { tip.hidden = true; $('seriesTarget').value = e.target.id(); lastChartTarget = null; showTab('telemetry'); });
    animateReroutes();
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

/* ── header, banner, environment ─────────────────────────────────────────── */
let warmupMax = 0;
function renderHeader(s) {
  const badge = $('modeBadge');
  badge.textContent = s.mode === 'simulation' ? 'Simulation' : s.mode === 'offline' ? 'Offline preview' : 'Live lab';
  badge.className = `badge ${s.mode === 'simulation' ? 'sim' : s.mode === 'offline' ? 'offline' : 'live'}`;
  const c = s.health_counts;
  setHTML($('healthPills'), `<span class="pill ok"><i></i>${c.HEALTHY || 0}<span class="pl">healthy</span></span>
    <span class="pill warn ${c.DEGRADED ? 'on' : ''}"><i></i>${c.DEGRADED || 0}<span class="pl">degraded</span></span>
    <span class="pill bad ${c.DOWN ? 'on' : ''}"><i></i>${c.DOWN || 0}<span class="pl">down</span></span>`, false);
  $('autonomyBadge').textContent = s.status.autonomy ? `Autonomy ${s.status.autonomy}` : '';

  const banner = $('banner');
  banner.hidden = false;
  let cls = 'banner'; let ic = 'info'; let text; let progress = '';
  if (s.mode === 'simulation') {
    text = 'Simulation — every value is simulated; failures and attacks are synthetic. Use the Control page to inject them.';
  } else if (s.mode === 'live') {
    cls = 'banner live'; ic = 'wifi';
    text = 'Live lab — Mininet + Open vSwitch + Ryu. Telemetry is measured, attacks are real programs in the hosts, failures change the emulated network.';
    if (s.status && s.status.learning) {
      const left = Math.ceil(s.status.warmup_remaining_s || 0);
      warmupMax = Math.max(warmupMax, left);
      text += ` Learning normal traffic — failures and attacks open in ~${left} s.`;
      progress = `<span class="progress" aria-hidden="true"><i style="width:${warmupMax ? Math.round(100 * (1 - left / warmupMax)) : 0}%"></i></span>`;
    }
  } else {
    cls = 'banner warn'; ic = 'alert';
    text = 'Offline preview — no network is running; telemetry is simulated. Start with VIEWER_MODE=simulation for the full closed loop.';
  }
  banner.className = cls;
  setHTML(banner, `${icon(ic)}<span>${esc(text)}</span>${progress}`, false);

  const env = { simulation: ['Simulation', 'sim'], live: ['Live lab', 'on'], offline: ['Offline preview', ''] }[s.mode] || ['Dashboard', ''];
  $('envTitle').textContent = env[0];
  $('envSub').textContent = s.topology.name || 'network';
  $('envDot').className = `env-dot ${env[1]}`;

  document.querySelectorAll('.control-only').forEach((el) => { el.hidden = !s.controls_enabled; });
  const pending = s.decisions ? s.decisions.pending.length : 0;
  $('pendingCount').hidden = pending === 0; $('pendingCount').textContent = pending;
  const quar = s.security ? s.security.quarantined.length : 0;
  $('quarCount').hidden = quar === 0; $('quarCount').textContent = quar;
  const alerts = s.prediction ? s.prediction.alerts.length : 0;
  $('predCount').hidden = alerts === 0; $('predCount').textContent = alerts;
}
function markSync(ok) {
  const sync = $('sync');
  sync.classList.toggle('err', !ok);
  if (ok) {
    $('clock').textContent = new Date().toLocaleTimeString([], { hour12: false });
    sync.classList.remove('tick'); void sync.offsetWidth; sync.classList.add('tick');
  }
}

/* ── KPIs ────────────────────────────────────────────────────────────────── */
const KPI_DEFS = [
  { key: 'healthy', label: 'Healthy elements', ic: 'heart' },
  { key: 'rerouted', label: 'Rerouted demands', ic: 'route' },
  { key: 'healing', label: 'Healing success', ic: 'wrench' },
  { key: 'threat', label: 'Threat index', ic: 'shield' },
  { key: 'alerts', label: 'Failure alerts', ic: 'sparkles' },
  { key: 'pending', label: 'Pending approvals', ic: 'hand' },
];
function kpiValues(s) {
  const total = s.topology.nodes.length + s.topology.links.length;
  const healthy = s.health_counts.HEALTHY || 0;
  const heal = s.healing ? s.healing.stats : null;
  const threat = s.security && s.security.threat ? s.security.threat.value : null;
  const alerts = s.prediction ? s.prediction.alerts.length : null;
  const pending = s.decisions ? s.decisions.pending.length : null;
  return {
    healthy: { v: healthy, suffix: `/${total}`, sub: 'links + devices', tone: healthy === total ? 't-ok' : 't-warn' },
    rerouted: { v: s.rerouted.length, sub: 'client/server pairs', tone: s.rerouted.length ? 't-reroute' : '' },
    healing: { v: heal && heal.success_rate !== null ? heal.success_rate * 100 : null, suffix: '%',
      sub: heal ? `${heal.verified}/${heal.executed} verified · ${heal.escalated} escalated` : 'simulation and live lab only',
      tone: heal && heal.escalated ? 't-warn' : 't-ok' },
    threat: { v: threat, digits: 2, sub: s.security ? `${s.security.quarantined.length} quarantined · ${s.security.blocked.length} blocked` : 'Zero Trust off',
      tone: threat > 0.2 ? 't-bad' : '' },
    alerts: { v: alerts, sub: s.prediction ? s.prediction.status.model : (s.status && s.status.learning ? 'learning normal traffic' : 'predictor off'),
      tone: alerts ? 't-predict' : '' },
    pending: { v: pending, sub: s.decisions ? `autonomy ${s.status.autonomy || s.decisions.status.autonomy}` : 'decisions off',
      tone: pending ? 't-warn' : '' },
  };
}
function renderKpis(s) {
  const box = $('kpis');
  if (!box.dataset.ready) {
    box.innerHTML = KPI_DEFS.map((k, i) => `<div class="kpi" id="kpi-${k.key}" style="--i:${i}">
      <div class="kpi-top"><span class="label">${k.label}</span><span class="kpi-icon">${icon(k.ic)}</span></div>
      <div class="value"><span class="v">—</span><span class="suffix"></span></div><div class="sub"></div></div>`).join('');
    box.dataset.ready = '1';
    box.classList.add('stagger');
    setTimeout(() => box.classList.remove('stagger'), 1200);
  }
  const vals = kpiValues(s);
  KPI_DEFS.forEach((k) => {
    const el = $(`kpi-${k.key}`); const d = vals[k.key];
    el.className = `kpi ${d.tone || ''}`;
    countTo(el.querySelector('.v'), d.v, d.digits || 0, d.v === null || d.v === undefined ? '' : (d.suffix === '%' ? '%' : ''));
    el.querySelector('.suffix').textContent = d.suffix && d.suffix !== '%' ? d.suffix : '';
    el.querySelector('.sub').textContent = d.sub;
  });
}

/* ── lists ───────────────────────────────────────────────────────────────── */
function intentRow(i, buttons = false) {
  return `<div class="row"><span class="marker ${STATE_CLASS[i.status] || 'info'}"></span><span class="time">${hhmmss(i.ts)}</span>
    <span class="what">${chip(i.type, i.type === 'MONITOR' ? '' : 'info')} <b>${esc(i.target)}</b> ${stateChip(i.status)}
    <div class="detail">${esc(i.reason)}${i.approval_reason ? ` — ${esc(i.approval_reason)}` : ''}</div>
    ${buttons ? `<div class="actions"><button class="btn ok small" data-approve="${esc(i.id)}">${icon('check')}Approve</button>
      <button class="btn ghost small" data-reject="${esc(i.id)}">${icon('x')}Reject</button></div>` : ''}</span></div>`;
}
function renderOverviewLists(s) {
  setHTML($('ovDecisions'), s.decisions
    ? (s.decisions.active.length ? s.decisions.active.slice(0, 8).map((i) => intentRow(i)).join('')
      : empty('No active decisions — the network is fine.', 'ok', 'check-circle'))
    : empty('The decision engine is off.'));
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
  return `<div class="row"><span class="marker ${CAT_CLASS[cat]}"></span><span class="time">${hhmmss(e.time)}</span>
    <span class="what">${chip(e.type, CAT_CLASS[cat])} ${esc(eventText(e))}${times}</span></div>`;
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
    setHTML($('ovEvents'), t.events.length ? collapse(t.events).slice(0, 25).map(eventRow).join('') : empty('No events yet.'));
  } catch (e) { setHTML($('ovEvents'), empty(e.message, 'err', 'alert')); }
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
  sel.addEventListener('change', () => { lastChartTarget = null; refreshTab(); });
}

let chartSeq = 0;
const W = 380, H = 170, P = { l: 40, r: 10, t: 10, b: 22 };
function lineChart(points, opts) {
  if (!points.length) return `<svg viewBox="0 0 ${W} ${H}"><text x="${W / 2}" y="${H / 2}" class="axis" text-anchor="middle">no data yet</text></svg>`;
  const id = `g${++chartSeq}`;
  const xs = points.map((p) => p[0]); const ys = points.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs, x0 + 1);
  let y0 = Math.min(0, ...ys), y1 = Math.max(...ys, opts.threshold ?? -Infinity, opts.binary ? 1 : 1e-9);
  if (y1 === y0) y1 = y0 + 1;
  y1 *= 1.12;
  const X = (v) => P.l + ((v - x0) / (x1 - x0)) * (W - P.l - P.r);
  const Y = (v) => H - P.b - ((v - y0) / (y1 - y0)) * (H - P.t - P.b);
  const path = points.map((p, i) => `${i ? 'L' : 'M'}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join('');
  const area = `${path}L${X(xs[xs.length - 1]).toFixed(1)},${Y(y0)}L${X(xs[0]).toFixed(1)},${Y(y0)}Z`;
  const ticks = [y0, (y0 + y1) / 2, y1].map((v) => `<text x="${P.l - 8}" y="${Y(v) + 3.5}" class="axis" text-anchor="end">${fmt(v, Math.abs(v) < 10 ? 1 : 0)}</text>
      <line class="grid" x1="${P.l}" x2="${W - P.r}" y1="${Y(v)}" y2="${Y(v)}"/>`).join('');
  const thr = opts.threshold !== null && opts.threshold !== undefined && opts.threshold < y1
    ? `<line class="thr" x1="${P.l}" x2="${W - P.r}" y1="${Y(opts.threshold)}" y2="${Y(opts.threshold)}"/>
       <text class="thr-label" x="${W - P.r - 2}" y="${Y(opts.threshold) - 4}" text-anchor="end">threshold ${opts.threshold}</text>` : '';
  const span = Math.round(x1 - x0);
  const draw = opts.animate && !REDUCED.matches;
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" data-x0="${x0}" data-x1="${x1}">
    <defs><linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0%" stop-color="${opts.color}" stop-opacity="0.32"/><stop offset="100%" stop-color="${opts.color}" stop-opacity="0"/></linearGradient></defs>
    ${ticks}<path class="area ${draw ? '' : 'static'}" d="${area}" fill="url(#${id})"/>
    <path class="line ${draw ? 'draw' : ''}" pathLength="1" d="${path}" stroke="${opts.color}" vector-effect="non-scaling-stroke"/>${thr}
    <text x="${P.l}" y="${H - 5}" class="axis">−${span}s</text>
    <text x="${W - P.r}" y="${H - 5}" class="axis" text-anchor="end">now</text></svg>`;
}
const chartData = new WeakMap();
function attachChartHover(box, pts, unit, color, digits) {
  chartData.set(box, { pts, unit, color, digits });
  if (box.dataset.hover) return;
  box.dataset.hover = '1';
  box.insertAdjacentHTML('beforeend', '<div class="chart-cursor"></div><div class="chart-dot"></div><div class="chart-tip"></div>');
  const [cur, dot, tip] = ['.chart-cursor', '.chart-dot', '.chart-tip'].map((s) => box.querySelector(s));
  const hide = () => { cur.style.display = dot.style.display = tip.style.display = 'none'; };
  box.addEventListener('mouseleave', hide);
  box.addEventListener('mousemove', (e) => {
    const d = chartData.get(box); const svg = box.querySelector('svg');
    if (!d || !d.pts.length || !svg) return hide();
    const r = svg.getBoundingClientRect();
    const sx = r.width / W; const sy = r.height / H;
    const x0 = Number(svg.dataset.x0); const x1 = Number(svg.dataset.x1);
    const fx = ((e.clientX - r.left) / sx - P.l) / (W - P.l - P.r);
    const t = x0 + Math.max(0, Math.min(1, fx)) * (x1 - x0);
    let best = d.pts[0];
    d.pts.forEach((p) => { if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p; });
    const y0 = d.yMin ?? 0; const y1 = d.yMax ?? 1;
    const px = (P.l + ((best[0] - x0) / (x1 - x0)) * (W - P.l - P.r)) * sx;
    const py = (H - P.b - ((best[1] - y0) / (y1 - y0)) * (H - P.t - P.b)) * sy;
    cur.style.display = dot.style.display = tip.style.display = 'block';
    cur.style.left = `${px}px`; dot.style.left = `${px}px`; dot.style.top = `${py}px`; dot.style.background = d.color;
    tip.style.left = `${Math.max(50, Math.min(r.width - 50, px))}px`;
    tip.innerHTML = `<b>${fmt(best[1], d.digits, d.unit ? ` ${d.unit}` : '')}</b> <span class="muted">${hhmmss(best[0])}</span>`;
  });
}

let lastChartTarget = null;
async function refreshTelemetry() {
  const target = $('seriesTarget').value;
  if (!target) return;
  const kind = state && state.topology.links.some((l) => l.name === target) ? 'link' : 'node';
  const metrics = CHART_METRICS[kind];
  const animate = lastChartTarget !== target;
  if (animate && !$('charts').children.length) setHTML($('charts'), metrics.map(() => '<div class="skel skel-chart"></div>').join(''), false);
  try {
    const r = await getJSON(`/api/series?target=${encodeURIComponent(target)}&metrics=${metrics.map((m) => m[0]).join(',')}&limit=180`);
    if ($('seriesTarget').value !== target) return;
    $('seriesNote').textContent = `${state && state.mode === 'live' ? 'Measured' : 'Simulated'} telemetry · last ~180 collections · dashed red line = health threshold`;
    const box = $('charts');
    const cards = metrics.map(([m, label, unit, thrKey], i) => {
      const pts = r.series[m] || [];
      const last = pts.length ? pts[pts.length - 1][1] : null;
      const color = m.endsWith('status') ? '#34d399' : m.includes('loss') ? '#f87171' : m.includes('latency') ? '#c084fc' : '#60a5fa';
      const digits = m.endsWith('status') ? 0 : 2;
      const thr = thrKey ? r.thresholds[thrKey] : null;
      return { m, html: `<div class="card chart" data-metric="${esc(m)}" style="--i:${i}"><div class="chart-head"><h3>${esc(label)}</h3>
          <span class="now">${fmt(last, digits)}<small>${unit ? esc(unit) : ''}</small></span></div>
          <div class="chart-box">${lineChart(pts, { threshold: thr, color, binary: m.endsWith('status'), animate })}</div></div>`,
        pts, unit, color, digits, thr };
    });
    if (animate || box.children.length !== cards.length || box.querySelector('.skel')) {
      box._html = null; box.innerHTML = cards.map((c) => c.html).join('');
      if (animate) { box.classList.add('stagger'); setTimeout(() => box.classList.remove('stagger'), 1200); }
    } else {
      cards.forEach((c, i) => {
        const card = box.children[i];
        const tmp = document.createElement('div'); tmp.innerHTML = c.html;
        const fresh = tmp.firstElementChild;
        card.querySelector('.now').innerHTML = fresh.querySelector('.now').innerHTML;
        const oldSvg = card.querySelector('svg'); const newSvg = fresh.querySelector('svg');
        oldSvg.replaceWith(newSvg);
      });
    }
    cards.forEach((c, i) => {
      const ys = c.pts.map((p) => p[1]);
      const y0 = Math.min(0, ...(ys.length ? ys : [0]));
      let y1 = Math.max(...(ys.length ? ys : [0]), c.thr ?? -Infinity, c.m.endsWith('status') ? 1 : 1e-9);
      if (y1 === y0) y1 = y0 + 1;
      const cb = box.children[i].querySelector('.chart-box');
      attachChartHover(cb, c.pts, c.unit, c.color, c.digits);
      Object.assign(chartData.get(cb), { yMin: y0, yMax: y1 * 1.12, thr: c.thr });
    });
    lastChartTarget = target;
  } catch (e) { setHTML($('charts'), empty(e.message, 'err', 'alert')); }
}

/* ── predictions ─────────────────────────────────────────────────────────── */
function renderPredictions(s) {
  if (!s.prediction) {
    $('predInfo').textContent = s.mode === 'live'
      ? 'The predictor is learning what normal looks like on the live network; it starts after the warm-up.'
      : 'No predictor is running (simulation mode trains one at start-up; otherwise set VIEWER_MODEL_DIR).';
    setHTML($('predTable'), empty(s.mode === 'live' ? 'Learning normal traffic…' : 'Prediction is off.', '', 'sparkles'));
    return;
  }
  const st = s.prediction.status;
  $('predInfo').textContent = `Model ${st.model} · alarm threshold ${fmt(st.threshold, 3)} · ` +
    (s.mode === 'live' ? 'trained on this live network\'s normal traffic' : 'trained on simulated data with synthetic warning patterns');
  const rows = [...s.topology.links].sort((a, b) => (b.prediction || 0) - (a.prediction || 0));
  const html = `<table><thead><tr><th>Link</th><th>Health</th><th>Failure score</th><th class="num">Score</th><th>Status</th></tr></thead><tbody>
    ${rows.map((l) => `<tr><td><b>${esc(l.name)}</b></td><td>${statusChip(l.status)}</td>
      <td style="width:42%">${bar(`pred:${l.name}`, Math.round((l.prediction || 0) * 100), l.predicted ? 'linear-gradient(90deg,#8b5cf6,#c084fc)' : 'linear-gradient(90deg,#3b82f6,#60a5fa)')}</td>
      <td class="num">${l.prediction === null ? '—' : fmt(l.prediction, 2)}</td>
      <td>${l.status === 'DOWN' ? chip('down — not scored') : l.predicted ? chip('failure predicted', 'predict') : chip('normal', 'ok')}</td></tr>`).join('')}</tbody></table>`;
  if (setHTML($('predTable'), html, false)) settleBars($('predTable'));
}

/* ── zero trust ──────────────────────────────────────────────────────────── */
function gauge(value) {
  const v = Math.max(0, Math.min(1, value || 0));
  const color = v > 0.4 ? '#f87171' : v > 0.2 ? '#fbbf24' : '#34d399';
  const len = Math.PI * 50;
  return `<svg viewBox="0 0 120 70" aria-hidden="true"><path class="g-track" d="M10 62a50 50 0 0 1 100 0" fill="none" stroke-width="10" stroke-linecap="round"/>
    <path class="g-val" d="M10 62a50 50 0 0 1 100 0" fill="none" stroke="${color}" stroke-width="10" stroke-linecap="round"
      stroke-dasharray="${len}" stroke-dashoffset="${len * (1 - v)}"/></svg>`;
}
function renderSecurity(s) {
  if (!s.security) {
    setHTML($('trustBars'), empty('Zero Trust is off (simulation mode, or VIEWER_SIMULATE_SECURITY=1).', '', 'shield'));
    setHTML($('threat'), ''); setHTML($('secEvents'), '');
    return;
  }
  const kinds = Object.fromEntries(s.topology.nodes.map((n) => [n.name, n.kind]));
  const html = s.security.trust.map((t) => `<div class="trust-row">
    <span class="avatar ${t.level === 'QUARANTINED' ? 'bad' : ''}">${icon(kinds[t.device] === 'server' ? 'server' : 'laptop')}</span>
    <b>${esc(t.device)}</b>
    ${bar(`trust:${t.device}`, Math.round(t.trust * 100), LEVEL_COLORS[t.level])}
    <span class="num">${fmt(t.trust, 2)}</span>
    <span class="chips">${chip(t.level, { TRUSTED: 'ok', RESTRICTED: 'warn', QUARANTINED: 'bad' }[t.level])}
    ${s.security.blocked.includes(t.device) ? chip('blocked', 'bad') : ''}</span></div>`).join('');
  if (setHTML($('trustBars'), html || empty('No trust data yet.'))) settleBars($('trustBars'));
  const th = s.security.threat;
  setHTML($('threat'), th ? `<div class="gauge">${gauge(th.value)}<div class="g-text"><b>${fmt(th.value, 2)}</b>
      <span>${th.value > 0.4 ? 'High threat' : th.value > 0.2 ? 'Elevated' : 'Normal'} · ${s.security.quarantined.length} quarantined</span></div></div>
    <div class="stats-row">
      <div class="stat"><b>${fmt(th.h_flow, 2)}</b><span>H_flow · entropy drop</span></div>
      <div class="stat"><b>${fmt(th.r_auth, 2)}</b><span>R_auth · failed logins</span></div>
      <div class="stat"><b>${fmt(th.f_anom, 2)}</b><span>F_anom · anomalous</span></div></div>` : empty('No data yet.'), false);
  setHTML($('secEvents'), s.security.events.length
    ? collapse(s.security.events.map((e) => ({ type: e.type, time: e.ts, data: e.data }))).map(eventRow).join('')
    : empty('No security events.', 'ok', 'shield-check'));
}

/* ── decisions & healing ─────────────────────────────────────────────────── */
function renderHealing(s) {
  if (!s.decisions) {
    setHTML($('pending'), empty('The decision engine is off.'));
    setHTML($('activeIntents'), ''); setHTML($('healStats'), ''); setHTML($('healActions'), '');
    return;
  }
  $('pendingCard').hidden = false;
  setHTML($('pending'), s.decisions.pending.length
    ? s.decisions.pending.map((i) => intentRow(i, s.controls_enabled)).join('') : empty('Nothing is waiting for approval.', 'ok', 'check-circle'));
  setHTML($('activeIntents'), s.decisions.active.length
    ? `<table><thead><tr><th>Action</th><th>Target</th><th class="num">Risk</th><th>Status</th></tr></thead><tbody>${s.decisions.active.map((i) => `<tr>
      <td>${chip(i.type, i.type === 'MONITOR' ? '' : 'info')}</td><td><b>${esc(i.target)}</b><div class="detail">${esc(i.reason)}</div></td>
      <td class="num">${fmt(i.risk, 2)}</td><td>${stateChip(i.status)}</td></tr>`).join('')}</tbody></table>` : empty('No active decisions.', 'ok', 'check-circle'));
  if (!s.healing) {
    setHTML($('healStats'), ''); setHTML($('healActions'), empty('Self-healing runs in simulation and live lab mode.'));
    return;
  }
  const st = s.healing.stats;
  setHTML($('healStats'), `<div class="stat"><b>${st.executed}</b><span>executed</span></div>
    <div class="stat"><b style="color:var(--ok)">${st.verified}</b><span>verified</span></div>
    <div class="stat"><b>${st.success_rate === null ? '—' : fmt(st.success_rate * 100, 0, '%')}</b><span>success</span></div>
    <div class="stat"><b>${st.rollbacks}</b><span>rolled back</span></div>
    <div class="stat"><b style="color:${st.escalated ? 'var(--warn)' : 'inherit'}">${st.escalated}</b><span>escalated</span></div>`, false);
  setHTML($('healActions'), s.healing.actions.length
    ? `<table><thead><tr><th>Started</th><th>Action</th><th>State</th></tr></thead><tbody>${s.healing.actions.map((a) => `<tr>
      <td class="tnum">${hhmmss(a.started)}</td><td>${chip(a.type, 'info')} <b>${esc(a.target)}</b>
      <div class="detail">${esc(a.detail)}</div>${a.checks.length ? `<div class="detail">last check: ${esc(a.checks[a.checks.length - 1])}</div>` : ''}</td>
      <td>${stateChip(a.state)}${a.attempts > 1 ? ` <span class="muted small">×${a.attempts}</span>` : ''}</td></tr>`).join('')}</tbody></table>`
    : empty('No healing actions yet.', '', 'wrench'));
}

document.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-approve], [data-reject]');
  if (!btn) return;
  const approve = btn.dataset.approve;
  const id = approve || btn.dataset.reject;
  const ok = await confirmDialog({ title: approve ? 'Approve this action?' : 'Reject this action?',
    body: `${approve ? 'The system will execute' : 'The system will not execute'} decision ${id}.`,
    ok: approve ? 'Approve' : 'Reject', tone: approve ? 'primary' : 'danger', iconName: approve ? 'check-circle' : 'x-circle' });
  if (!ok) return;
  try {
    await withBusy(btn, () => postJSON(`/api/decisions/${encodeURIComponent(id)}/${approve ? 'approve' : 'reject'}`));
    toast(approve ? 'Approved' : 'Rejected', id, true); poll();
  } catch (err) { toast('Could not update the decision', err.message, false); }
});

/* ── timeline ────────────────────────────────────────────────────────────── */
const FILTER_CLASS = { failure: 'c-bad', health: 'c-warn', prediction: 'c-predict', security: 'c-bad', decision: 'c-info', healing: 'c-ok' };
function renderTimelineFilters() {
  $('timelineFilters').innerHTML = Object.keys(CATEGORIES).map((c) => `<button type="button" class="fpill ${FILTER_CLASS[c]}"
    data-cat="${c}" aria-pressed="${timelineFilter.has(c)}"><i></i>${c}</button>`).join('');
  $('timelineFilters').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
    const c = b.dataset.cat;
    if (timelineFilter.has(c)) timelineFilter.delete(c); else timelineFilter.add(c);
    b.setAttribute('aria-pressed', String(timelineFilter.has(c)));
    refreshTimeline();
  }));
}
function renderTimeline() {
  const q = $('timelineSearch').value.trim().toLowerCase();
  const rows = collapse(timelineEvents).filter((e) => !q || `${e.type} ${eventText(e)}`.toLowerCase().includes(q));
  setHTML($('timeline'), rows.length ? rows.map(eventRow).join('')
    : empty(q ? `No events match “${q}”.` : 'No events for these filters.', '', q ? 'search' : 'inbox'));
}
let searchTimer = null;
$('timelineSearch').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(renderTimeline, 120); });
async function refreshTimeline() {
  const types = [...timelineFilter].flatMap((c) => CATEGORIES[c]).join(',');
  if (!types) { timelineEvents = []; renderTimeline(); return; }
  try {
    const t = await getJSON(`/api/timeline?limit=400&types=${types}`);
    timelineEvents = t.events;
    renderTimeline();
  } catch (e) { setHTML($('timeline'), empty(e.message, 'err', 'alert')); }
}

/* ── analytics ───────────────────────────────────────────────────────────── */
async function refreshAnalytics() {
  try {
    const r = await getJSON(`/api/analytics/summary?window_s=${$('anaWindow').value}`);
    const s = r.summary;
    if (!s) { setHTML($('analytics'), empty('No data recorded yet.', '', 'chart')); return; }
    const inc = s.incidents;
    const stat = (v, label, color) => `<div class="stat"><b${color ? ` style="color:${color}"` : ''}>${v}</b><span>${label}</span></div>`;
    const avail = (prefix, rows) => `<table><thead><tr><th>Element</th><th class="num">Availability</th><th></th></tr></thead><tbody>${rows.targets.map((t) => `<tr><td><b>${esc(t.target)}</b></td>
      <td class="num">${fmt(t.availability_pct, 1, '%')}</td>
      <td style="width:42%">${bar(`${prefix}:${t.target}`, t.availability_pct || 0, (t.availability_pct || 0) >= 99 ? 'linear-gradient(90deg,#10b981,#34d399)' : 'linear-gradient(90deg,#f59e0b,#fbbf24)')}</td></tr>`).join('')}</tbody></table>`;
    const html = `<div class="card"><div class="card-head"><h3><span class="h-icon">${icon('chart')}</span>Window summary <span class="muted small">${esc(s.simulated)} data</span></h3></div>
      <div class="stats-row">
        ${stat(fmt(s.links.availability_pct, 2, '%'), 'link availability', 'var(--ok)')}${stat(fmt(s.nodes.availability_pct, 2, '%'), 'device availability', 'var(--ok)')}
        ${stat(inc.down.count, 'outages', inc.down.count ? 'var(--bad)' : null)}${stat(inc.down.mttr_s === null ? '—' : fmt(inc.down.mttr_s, 0, ' s'), 'MTTR (outages)')}
        ${stat(inc.degraded.count, 'degradations', inc.degraded.count ? 'var(--warn)' : null)}${stat(fmt(inc.down.total_duration_s, 0, ' s'), 'total downtime')}</div></div>
      <div class="two-col"><div class="card"><div class="card-head"><h3><span class="h-icon">${icon('link')}</span>Link availability</h3></div><div class="table-wrap">${avail('al', s.links)}</div></div>
      <div class="card"><div class="card-head"><h3><span class="h-icon">${icon('server')}</span>Device availability</h3></div><div class="table-wrap">${avail('an', s.nodes)}</div></div></div>`;
    if (setHTML($('analytics'), html)) settleBars($('analytics'));
  } catch (e) {
    setHTML($('analytics'), empty(e.status === 404 ? 'History recording is off (simulation mode records automatically; otherwise set VIEWER_DB_PATH).' : e.message, e.status === 404 ? '' : 'err', 'chart'));
  }
}
$('anaWindow').addEventListener('change', refreshAnalytics);

/* ── control ─────────────────────────────────────────────────────────────── */
function positionSegment() {
  const seg = $('autonomy'); const active = seg.querySelector('button.active'); const ind = seg.querySelector('.seg-indicator');
  if (!active) { ind.style.opacity = 0; return; }
  const i = [...seg.querySelectorAll('button')].indexOf(active);
  ind.style.opacity = 1;
  ind.style.transform = `translateX(${i * 100}%)`;
}
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
  document.querySelectorAll('#autonomy button').forEach((b) => {
    const on = b.dataset.level === s.status.autonomy;
    b.classList.toggle('active', on); b.setAttribute('aria-checked', String(on));
  });
  positionSegment();
  setHTML($('injections'), s.injections.length
    ? `<table><thead><tr><th>When</th><th>What</th><th>Target</th><th>State</th></tr></thead><tbody>${s.injections.map((i) => `<tr>
      <td class="tnum">${hhmmss(i.start)}</td><td>${chip(i.type, i.type === 'attack' ? 'bad' : 'warn')} <b>${esc(i.kind)}</b>
      ${i.physical ? chip('physical', 'bad') : ''}${i.warning_s ? chip(`${i.warning_s}s warning`, 'predict') : ''}</td>
      <td>${esc(i.target.join('-'))}</td><td>${chip(i.state, i.state === 'active' ? 'bad' : i.state === 'warning' ? 'predict' : '')}</td></tr>`).join('')}</tbody></table>`
    : empty('Nothing injected yet — use the forms above.', '', 'history'));
}
function controlMsg(text, ok) { const m = $('controlMsg'); m.textContent = text; m.className = `msg ${ok ? 'ok' : 'err'}`; }
$('failForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target.elements;
  const body = { kind: f.kind.value, target: f.target.value.split(','), duration_s: Number(f.duration_s.value),
    warning_s: Number(f.warning_s.value), physical: f.physical.checked };
  const ok = await confirmDialog({ title: `Inject ${body.kind.replace('_', ' ')} failure?`,
    body: `${body.target.join('-')} for ${body.duration_s} s${body.physical ? ', physical damage (software cannot fix it)' : ''}${body.warning_s ? `, after a ${body.warning_s} s warning ramp` : ''}.`,
    ok: 'Inject', tone: 'warn', iconName: 'zap' });
  if (!ok) return;
  try {
    const r = await withBusy(e.target.querySelector('button[type=submit]'), () => postJSON('/api/sim/failure', body));
    controlMsg(`Scheduled ${r.id}`, true); toast('Failure scheduled', `${r.id} · ${body.target.join('-')}`, true); poll();
  } catch (err) { controlMsg(err.message, false); toast('Could not inject the failure', err.message, false); }
});
$('attackForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target.elements;
  const body = { kind: f.kind.value, attacker: f.attacker.value, duration_s: Number(f.duration_s.value) };
  const ok = await confirmDialog({ title: `Launch ${body.kind.replace('_', ' ')}?`,
    body: `From ${body.attacker} for ${body.duration_s} s. Watch the Zero Trust page.`, ok: 'Launch', tone: 'danger', iconName: 'crosshair' });
  if (!ok) return;
  try {
    const r = await withBusy(e.target.querySelector('button[type=submit]'), () => postJSON('/api/sim/attack', body));
    controlMsg(`Launched ${r.id}`, true); toast('Attack launched', `${r.id} from ${body.attacker}`, true); poll();
  } catch (err) { controlMsg(err.message, false); toast('Could not launch the attack', err.message, false); }
});
document.querySelectorAll('#autonomy button').forEach((b) => b.addEventListener('click', async () => {
  if (b.classList.contains('active')) return;
  const ok = await confirmDialog({ title: `Set autonomy to ${b.dataset.level}?`,
    body: { L1: 'Every action will wait for your approval.', L2: 'Reversible actions run automatically; others wait for approval.',
      L3: 'Every action runs automatically.' }[b.dataset.level], ok: `Switch to ${b.dataset.level}`, iconName: 'cpu' });
  if (!ok) return;
  try { await postJSON('/api/sim/autonomy', { level: b.dataset.level }); controlMsg(`Autonomy ${b.dataset.level}`, true); toast('Autonomy changed', b.dataset.level, true); poll(); }
  catch (err) { controlMsg(err.message, false); toast('Could not change autonomy', err.message, false); }
}));
$('demoBtn').addEventListener('click', async () => {
  const ok = await confirmDialog({ title: 'Run the scripted demo?', body: 'A cable cut, a switch failure, a predicted latency rise, a flood and a spoofing attack over about 7 minutes.',
    ok: 'Run demo', iconName: 'play' });
  if (!ok) return;
  try {
    const r = await withBusy($('demoBtn'), () => postJSON('/api/sim/demo'));
    controlMsg(`Demo scheduled: ${r.scheduled.length} events`, true); toast('Demo started', `${r.scheduled.length} events over ~7 minutes`, true); showTab('overview');
  } catch (err) { controlMsg(err.message, false); toast('Could not start the demo', err.message, false); }
});

/* ── polling ─────────────────────────────────────────────────────────────── */
function refreshTab() {
  if (activeTab === 'telemetry') refreshTelemetry();
  if (activeTab === 'timeline') refreshTimeline();
  if (activeTab === 'analytics') refreshAnalytics();
}

let failures = 0;
async function poll() {
  try {
    state = await getJSON('/api/overview');
    failures = 0;
  } catch (e) {
    failures += 1;
    markSync(false);
    const banner = $('banner'); banner.hidden = false; banner.className = 'banner err';
    setHTML(banner, `${icon('alert')}<span>Cannot reach the dashboard API (${esc(e.message)}). Retrying every ${POLL_MS / 1000} s…</span>`, false);
    $('envDot').className = 'env-dot err'; $('envTitle').textContent = 'Disconnected'; $('envSub').textContent = 'API unreachable';
    return;
  }
  markSync(true);
  renderHeader(state);
  renderTopology(state.topology);
  renderKpis(state);
  renderOverviewLists(state);
  fillTargets(state);
  renderPredictions(state);
  renderSecurity(state);
  renderHealing(state);
  renderNotifications(state);
  if (state.controls_enabled) fillControlForms(state);
  if (activeTab === 'overview') refreshOverviewEvents();
  else refreshTab();
}

/* ── boot ────────────────────────────────────────────────────────────────── */
hydrateIcons();
$('kpis').innerHTML = KPI_DEFS.map(() => '<div class="skel skel-kpi"></div>').join('');
['ovDecisions', 'ovEvents', 'trustBars', 'secEvents', 'pending', 'activeIntents', 'healActions', 'timeline', 'predTable']
  .forEach((id) => { $(id).innerHTML = skeleton(4); });
renderTimelineFilters();
showTab((location.hash || '#overview').slice(1));
poll();
setInterval(poll, POLL_MS);
