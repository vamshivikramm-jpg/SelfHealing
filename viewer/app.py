"""Viewer v1.0 — Read-only enterprise network dashboard.

FastAPI backend that serves a read-only Cytoscape.js dashboard.
Never modifies the network.

Live mode  — Mininet available (Linux): uses NetworkController + FailureManager.
Offline mode — Mininet unavailable (macOS): reads YAML config directly,
               uses NetworkState + FailureRegistry, no Mininet dependency.
"""

from __future__ import annotations

import sys
import threading
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_NETWORK_SRC = str(_REPO_ROOT / "network" / "src")
_CONFIG_PATH = str(_REPO_ROOT / "network" / "config" / "enterprise.yaml")

if _NETWORK_SRC not in sys.path:
    sys.path.insert(0, _NETWORK_SRC)

# ---------------------------------------------------------------------------
# Import strategy: live first, offline fallback
# ---------------------------------------------------------------------------

_OFFLINE_MODE = False

try:
    from network.control import NetworkController      # noqa: E402
    from network.failure import FailureManager         # noqa: E402
    from network.events.event import Event as NetEvent # noqa: E402
except Exception:
    _OFFLINE_MODE = True
    from viewer._offline import OfflineController      # noqa: E402
    from viewer._offline import OfflineFailureManager  # noqa: E402
    from viewer._offline import Event as NetEvent      # noqa: E402

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_controller = None
_manager = None
_event_log: deque = deque(maxlen=200)
_lock = threading.Lock()


def _ensure_singletons() -> None:
    global _controller, _manager
    if _controller is not None:
        return

    if not _OFFLINE_MODE:
        _controller = NetworkController(config_path=_CONFIG_PATH)
        _manager = FailureManager(_controller)
        _controller.event_bus.subscribe(_collect_event)
    else:
        _controller = OfflineController(config_path=_CONFIG_PATH)
        _manager = OfflineFailureManager(_controller._registry)
        _controller.event_bus.subscribe(_collect_event)


def _collect_event(event) -> None:
    with _lock:
        _event_log.appendleft(event.to_dict())


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Enterprise Network Viewer", version="1.0.0")

_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe(fn, fallback=None):
    try:
        return fn()
    except Exception:
        return fallback


def _failure_dicts():
    if _manager is None:
        return []
    return [f.to_dict() for f in _manager.active_failures()]


def _recovered_dicts():
    if _manager is None:
        return []
    return [f.to_dict() for f in _manager._registry.recovered_failures()]


def _history_dicts():
    if _manager is None:
        return []
    return [f.to_dict() for f in _manager._registry.history()]


def _device_counts():
    try:
        counts = Counter(d.kind for d in _controller.config.devices)
    except Exception:
        counts = Counter()
    return {
        "routers":  counts.get("router", 0),
        "switches": counts.get("switch", 0),
        "hosts":    counts.get("host", 0),
        "servers":  counts.get("server", 0),
    }


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((_TEMPLATE_DIR / "index.html").read_text())


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    _ensure_singletons()
    counts = _device_counts()
    return {
        "status": "running",
        "module": "viewer v1.0",
        "offline_mode": _OFFLINE_MODE,
        "network_version": _safe(lambda: _controller.config.version, "unknown"),
        "network_name": _safe(lambda: _controller.config.name, "unknown"),
        "network_running": _safe(lambda: _controller.state.is_running, False),
        "controller_ip": _safe(lambda: _controller.config.controller_ip, "unknown"),
        "controller_port": _safe(lambda: _controller.config.controller_port, 6653),
        "active_failures": len(_failure_dicts()),
        "total_events": len(_event_log),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **counts,
    }


@app.get("/api/topology")
async def topology():
    _ensure_singletons()
    return {
        "topology": _safe(lambda: _controller.topology_snapshot(), {}),
        "impairments": _safe(lambda: _controller.impairments(), {}),
        "active_failures": _failure_dicts(),
    }


@app.get("/api/state")
async def state():
    _ensure_singletons()
    ns = _safe(lambda: _controller.network_status(), {})
    counts = _device_counts()
    return {
        "status": _safe(lambda: _controller.state.status, "stopped"),
        "timestamp": ns.get("timestamp", datetime.now(timezone.utc).isoformat()),
        "nodes": ns.get("nodes", []),
        "links": ns.get("links", []),
        **counts,
    }


@app.get("/api/failures")
async def failures():
    _ensure_singletons()
    return {
        "active": _failure_dicts(),
        "recovered": _recovered_dicts(),
        "history": _history_dicts(),
    }


@app.get("/api/events")
async def events():
    with _lock:
        return {"events": list(_event_log)}
