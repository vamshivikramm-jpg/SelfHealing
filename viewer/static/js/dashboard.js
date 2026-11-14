/* ── Enterprise Network Viewer — Dashboard JS ── */

const POLL_INTERVAL = 2000;
const cyEl = document.getElementById('cy');
const tooltip = document.createElement('div');
tooltip.className = 'cy-tooltip';
document.body.appendChild(tooltip);

let cy = null;
let lastEventCount = 0;
let bannerShown = false;

/* ── Clock ── */
function tickClock() {
  document.getElementById('clock').textContent = new Date().toLocaleTimeString();
}
tickClock();
setInterval(tickClock, 1000);

/* ── Node / Link Styles ── */
const NODE_COLORS = {
  router:   { online: '#3fb950', offline: '#484f58' },
  switch:   { online: '#58a6ff', offline: '#484f58' },
  host:     { online: '#3fb950', offline: '#484f58' },
  server:   { online: '#d29922', offline: '#484f58' },
};
const LINK_COLORS = {
  online:   '#3fb950',
  offline:  '#f85149',
};

/* ── Cytoscape Init ── */
function initCytoscape() {
  cy = cytoscape({
    container: cyEl,
    style: [
      {
        selector: 'node',
        style: {
          'label': 'data(label)',
          'font-size': '11px',
          'font-family': 'Segoe UI, system-ui, sans-serif',
          'color': '#e0e0e0',
          'text-valign': 'bottom',
          'text-margin-y': 6,
          'text-outline-color': '#0d1117',
          'text-outline-width': 2,
          'background-color': 'data(color)',
          'border-width': 2,
          'border-color': 'data(border)',
          'width': 'data(w)',
          'height': 'data(h)',
        },
      },
      {
        selector: 'edge',
        style: {
          'curve-style': 'bezier',
          'target-arrow-shape': 'none',
          'width': 2,
          'line-color': 'data(color)',
          'line-opacity': 0.8,
        },
      },
    ],
    layout: { name: 'preset' },
    minZoom: 0.3,
    maxZoom: 3,
    boxSelectionEnabled: false,
  });

  /* Tooltip on hover */
  cy.on('mouseover', 'node, edge', (evt) => {
    const d = evt.target.data();
    let html = '';
    if (evt.target.isNode()) {
      html = `<div class="tt-name">${d.label || d.id}</div>
              <div class="tt-kind">${(d.kind || d.type || '').toUpperCase()}</div>`;
      if (d.state) html += `<div class="tt-state">State: ${d.state}</div>`;
    } else {
      html = `<div class="tt-name">${d.id}</div>
              <div class="tt-kind">Link</div>
              <div class="tt-state">State: ${d.state || 'online'}</div>`;
    }
    tooltip.innerHTML = html;
    tooltip.style.display = 'block';
    tooltip.style.left = evt.originalEvent.pageX + 14 + 'px';
    tooltip.style.top  = evt.originalEvent.pageY - 10 + 'px';
  });

  cy.on('mouseout', 'node, edge', () => { tooltip.style.display = 'none'; });
}

/* ── Node size helper ── */
function nodeSize(kind) {
  switch (kind) {
    case 'router':  return 30;
    case 'switch':  return 24;
    case 'server':  return 20;
    default:        return 16; // host
  }
}

/* ── Build Cytoscape elements from topology snapshot ── */
function buildElements(snap, activeFailures) {
  const nodes = snap.devices || [];
  const links = snap.links || [];
  const elements = [];
  const impairedSet = new Set();
  (activeFailures || []).forEach((f) => {
    (f.affected_nodes || []).forEach((n) => impairedSet.add(n));
  });

  const positions = {};
  const nodeCount = nodes.length;
  nodes.forEach((n, i) => {
    const state = n.state || 'online';
    const kind  = n.kind || n.type || 'host';
    const imp = impairedSet.has(n.name) || impairedSet.has(n.id);
    let fill = '#3fb950';
    if (state === 'offline') fill = '#484f58';
    else if (imp) fill = '#d29922';
    else fill = (NODE_COLORS[kind] || NODE_COLORS.host)[state === 'offline' ? 'offline' : 'online'];
    const border = imp ? '#d29922' : fill;

    /* radial layout */
    const angle = (2 * Math.PI * i) / nodeCount;
    const r = 180 + (kind === 'router' ? 60 : kind === 'switch' ? 0 : -60);
    positions[n.id || n.name] = {
      x: 450 + r * Math.cos(angle),
      y: 300 + r * Math.sin(angle),
    };

    elements.push({
      group: 'nodes',
      data: {
        id: n.id || n.name,
        label: n.name || n.id,
        kind,
        type: kind,
        color: fill,
        border,
        w: nodeSize(kind),
        h: nodeSize(kind),
        state,
      },
      position: positions[n.id || n.name],
    });
  });

  links.forEach((lnk) => {
    const src = lnk.src || lnk.source;
    const dst = lnk.dst || lnk.target;
    const ok = lnk.state === 'online' || lnk.state === 'up';
    elements.push({
      group: 'edges',
      data: {
        id: `${src}--${dst}`,
        source: src,
        target: dst,
        color: ok ? LINK_COLORS.online : LINK_COLORS.offline,
        state: lnk.state || 'online',
      },
    });
  });

  return elements;
}

/* ── Update Cytoscape graph ── */
function updateGraph(snap, activeFailures) {
  const newEls = buildElements(snap, activeFailures);
  if (!cy) return;
  /* merge / update */
  const existing = cy.elements();
  /* rebuild if topology changed */
  if (existing.length === 0 || existing.nodes().length !== (snap.devices || []).length) {
    cy.elements().remove();
    cy.add(newEls);
    cy.layout({ name: 'preset', animate: false }).run();
  } else {
    /* just update colours */
    newEls.forEach((el) => {
      if (el.group === 'nodes') {
        const node = cy.getElementById(el.data.id);
        if (node.length) {
          node.data('color', el.data.color);
          node.data('border', el.data.border);
          node.data('state', el.data.state);
        }
      } else {
        const edge = cy.getElementById(el.data.id);
        if (edge.length) {
          edge.data('color', el.data.color);
          edge.data('state', el.data.state);
        }
      }
    });
  }
  cy.fit(undefined, 40);
}

/* ── DOM helpers ── */
const $  = (id) => document.getElementById(id);
const $t = (id, v) => { $(id).textContent = v; };

function renderSummary(status) {
  $t('statusLabel', status.status || 'stopped');
  const dot = $('statusDot');
  dot.className = 'status-dot' + (status.status === 'running' ? ' running' : '');
  $t('controllerInfo', `${status.controller_ip || '—'}:${status.controller_port || '—'}`);
  $t('cntRouters',  status.routers  || 0);
  $t('cntSwitches', status.switches || 0);
  $t('cntHosts',    status.hosts    || 0);
  $t('cntServers',  status.servers  || 0);
}

function renderFailures(active) {
  const box = $('failureList');
  $('cntFailures').textContent = active.length;
  if (active.length === 0) {
    box.innerHTML = '<p class="empty-msg">No active failures</p>';
    return;
  }
  box.innerHTML = active.map((f) => `
    <div class="failure-item">
      <div class="fi-type">${(f.failure_type || 'unknown').replace(/_/g,' ').toUpperCase()}</div>
      <div class="fi-nodes">Affected: ${(f.affected_nodes || []).join(', ')}</div>
      <div class="fi-meta">Severity: ${f.severity || '—'} | State: ${f.state || '—'}</div>
    </div>`).join('');
}

function renderEvents(events) {
  const box = $('eventLog');
  box.innerHTML = events.slice(0, 100).map((e) => {
    const ts = e.timestamp ? e.timestamp.replace('T',' ').substring(0,19) : '—';
    const typ = e.type || 'UNKNOWN';
    let tgt = e.data ? (e.data.node || e.data.switch || e.data.link || e.data.router || e.data.target || e.data.source || '') : '';
    if (typ === 'HEALTH_CHANGED' && e.data) tgt = `${tgt}: ${e.data.previous} → ${e.data.status}`;
    return `<div class="event-row">
      <span class="ev-time">${ts}</span>
      <span class="ev-type ${typ}">${typ}</span>
      <span class="ev-target">${tgt}</span>
      <span class="ev-result">${e.result || ''}</span>
    </div>`;
  }).join('');
}

/* ── Poll loop ── */
async function poll() {
  try {
    const [healthRes, topoRes, stateRes, failRes, evtRes] = await Promise.all([
      fetch('/api/health').then((r) => r.json()),
      fetch('/api/topology').then((r) => r.json()),
      fetch('/api/state').then((r) => r.json()),
      fetch('/api/failures').then((r) => r.json()),
      fetch('/api/events').then((r) => r.json()),
    ]);

    /* Show offline banner once */
    if (healthRes.offline_mode && !bannerShown) {
      $('offlineBanner').style.display = 'block';
      $('layout').classList.add('with-banner');
      bannerShown = true;
    }

    const topo  = topoRes.topology || {};
    const state = stateRes || {};
    const active = failRes.active || [];
    const events = evtRes.events || [];

    renderSummary(state);
    renderFailures(active);
    $('cntRecovered').textContent = (failRes.recovered || []).length;

    // Key on the newest event too: once the server's 200-entry log is full,
    // its length stops changing while new events keep arriving.
    const eventKey = events.length + '|' + (events[0] ? events[0].timestamp + events[0].type : '');
    if (eventKey !== lastEventCount) {
      renderEvents(events);
      lastEventCount = eventKey;
    }

    updateGraph(topo, active);
  } catch (err) {
    console.error('poll error', err);
  }
}

/* ── Init ── */
initCytoscape();
poll();
setInterval(poll, POLL_INTERVAL);
