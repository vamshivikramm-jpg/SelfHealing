"""Viewer v1.0 — Read-only enterprise network dashboard.

FastAPI backend that serves a read-only Cytoscape.js dashboard.
Never modifies the network.

Live mode  — Mininet available (Linux): uses NetworkController + FailureManager.
Offline mode — Mininet unavailable (macOS): reads YAML config directly,
               uses NetworkState + FailureRegistry, no Mininet dependency.

Module 2 adds a TelemetryCollector running in the background with the
SimulatedSource. All telemetry it serves is SIMULATED (flagged in every
response). In offline mode the network is "stopped", so every target
reads DOWN unless VIEWER_SIMULATE_NETWORK=1 marks the offline topology
as running for simulation purposes.

Module 3: with VIEWER_DB_PATH set, telemetry and events are also
recorded to a SQLite database and /api/analytics/summary reports
availability, incidents and MTTR over a recent window.

Environment:
  VIEWER_SIMULATE_NETWORK=1     offline only: simulate a running network
  VIEWER_TELEMETRY_INTERVAL=5   telemetry collection interval (seconds)
  VIEWER_DB_PATH=path.db        record telemetry history (off when unset)
  VIEWER_MODEL_DIR=dir          Module 4: run the registry's champion model
                                for live failure prediction (needs numpy +
                                scikit-learn in the viewer's environment)
"""

from __future__ import annotations

import os
import sys
import threading
from collections import Counter, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
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
    from network.telemetry import (                    # noqa: E402
        METRIC_SPECS, Metric, SimulatedSource, TelemetryCollector,
    )
    from network.analytics.recorder import TelemetryRecorder      # noqa: E402
    from network.analytics.repository import TelemetryRepository  # noqa: E402
    from network.analytics.summary import network_summary         # noqa: E402
except Exception:
    _OFFLINE_MODE = True
    from viewer._offline import OfflineController      # noqa: E402
    from viewer._offline import OfflineFailureManager  # noqa: E402
    from viewer._offline import Event as NetEvent      # noqa: E402
    from viewer._offline import SimulatedSource        # noqa: E402
    from viewer._offline import TelemetryCollector     # noqa: E402
    from viewer._offline import Metric, METRIC_SPECS   # noqa: E402
    from viewer._offline import TelemetryRepository    # noqa: E402
    from viewer._offline import TelemetryRecorder      # noqa: E402
    from viewer._offline import network_summary        # noqa: E402

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_controller = None
_manager = None
_collector = None
_repository = None
_recorder = None
_event_log: deque = deque(maxlen=200)
_lock = threading.Lock()


_SIMULATE_NETWORK = os.environ.get("VIEWER_SIMULATE_NETWORK", "") == "1"
_TELEMETRY_INTERVAL = float(os.environ.get("VIEWER_TELEMETRY_INTERVAL", "5"))
_DB_PATH = os.environ.get("VIEWER_DB_PATH", "")
_MODEL_DIR = os.environ.get("VIEWER_MODEL_DIR", "")
_predictor = None
_prediction_error = None

# High-frequency events that would flood the dashboard event log.
_UNLOGGED_EVENTS = {"TELEMETRY_COLLECTED"}


def _ensure_singletons() -> None:
    global _controller, _manager, _collector, _repository, _recorder, _predictor, _prediction_error
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
        if _SIMULATE_NETWORK:
            _controller.simulate_running()

    _collector = TelemetryCollector(
        [SimulatedSource(_controller)],
        event_bus=_controller.event_bus,
        interval_s=_TELEMETRY_INTERVAL,
    )
    if _DB_PATH:
        _repository = TelemetryRepository(_DB_PATH)
        _recorder = TelemetryRecorder(
            _repository, collector=_collector, event_bus=_controller.event_bus
        ).attach()
    if _MODEL_DIR:
        try:
            if _OFFLINE_MODE:
                from viewer._offline import load_prediction
                ModelRegistry, OnlinePredictor = load_prediction()
            else:
                from network.prediction.online import OnlinePredictor
                from network.prediction.registry import ModelRegistry
            _predictor = OnlinePredictor.from_registry(
                ModelRegistry(_MODEL_DIR, create=False), _collector
            ).attach()
        except Exception as exc:  # noqa: BLE001 - reported by /api/predictions
            _prediction_error = f"{type(exc).__name__}: {exc}"


def _collect_event(event) -> None:
    if event.type.value in _UNLOGGED_EVENTS:
        return
    with _lock:
        _event_log.appendleft(event.to_dict())


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(_app):
    _ensure_singletons()
    _collector.start()
    try:
        yield
    finally:
        _collector.stop()
        if _recorder is not None:
            _recorder.detach()
        if _repository is not None:
            _repository.close()


app = FastAPI(title="Enterprise Network Viewer", version="1.1.0", lifespan=_lifespan)

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
        "module": "viewer v1.1",
        "offline_mode": _OFFLINE_MODE,
        "network_simulated": _safe(lambda: _controller.simulated_running, False),
        "telemetry_simulated": _collector.simulated,
        "telemetry_running": _collector.is_running,
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


# ---------------------------------------------------------------------------
# Telemetry API (Module 2) — all data is SIMULATED (see "simulated" flags)
# ---------------------------------------------------------------------------

def _parse_metric(metric: Optional[str]):
    if metric is None:
        return None
    try:
        return Metric(metric)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"unknown metric: {metric!r}")


@app.get("/api/telemetry/status")
async def telemetry_status():
    _ensure_singletons()
    return _collector.status()


@app.get("/api/telemetry/metrics")
async def telemetry_metrics():
    return {
        "metrics": [
            {
                "metric": spec.metric.value,
                "kind": spec.kind.value,
                "unit": spec.unit,
                "target_kind": spec.target_kind.value,
                "description": spec.description,
            }
            for spec in METRIC_SPECS.values()
        ]
    }


@app.get("/api/telemetry/health")
async def telemetry_health():
    _ensure_singletons()
    reports = [r.to_dict() for r in _collector.health().values()]
    return {
        "simulated": _collector.simulated,
        "thresholds": _collector.evaluator.thresholds.to_dict(),
        "summary": _collector.status()["health_counts"],
        "reports": sorted(reports, key=lambda r: (r["target_kind"], r["target"])),
    }


@app.get("/api/telemetry/latest")
async def telemetry_latest(metric: Optional[str] = None, target: Optional[str] = None):
    _ensure_singletons()
    samples = _collector.store.latest(metric=_parse_metric(metric), target=target)
    return {"simulated": _collector.simulated, "samples": [x.to_dict() for x in samples]}


@app.get("/api/telemetry/series")
async def telemetry_series(metric: str, target: str, limit: int = 120):
    _ensure_singletons()
    if limit < 1 or limit > 10_000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 10000")
    samples = _collector.store.series(_parse_metric(metric), target, limit=limit)
    return {
        "metric": metric,
        "target": target,
        "simulated": _collector.simulated,
        "points": [{"timestamp": x.timestamp, "value": x.value} for x in samples],
    }


# ---------------------------------------------------------------------------
# Analytics API (Module 3) — needs VIEWER_DB_PATH
# ---------------------------------------------------------------------------

@app.get("/api/analytics/summary")
async def analytics_summary(window_s: float = 3600.0):
    _ensure_singletons()
    if _repository is None:
        raise HTTPException(
            status_code=404,
            detail="history recording is off; start the viewer with VIEWER_DB_PATH=<file.db>",
        )
    if window_s <= 0:
        raise HTTPException(status_code=400, detail="window_s must be > 0")
    rng = _repository.time_range()
    if rng is None:
        return {"recording": _recorder.stats(), "summary": None}
    summary = network_summary(_repository, start=max(rng[0], rng[1] - window_s), end=rng[1] + 1e-6)
    return {"recording": _recorder.stats(), "database": _repository.stats(), "summary": summary}


# ---------------------------------------------------------------------------
# Prediction API (Module 4) — needs VIEWER_MODEL_DIR
# ---------------------------------------------------------------------------

@app.get("/api/predictions")
async def predictions():
    _ensure_singletons()
    if not _MODEL_DIR:
        raise HTTPException(
            status_code=404,
            detail="live prediction is off; start the viewer with VIEWER_MODEL_DIR=<model registry>",
        )
    if _predictor is None:
        raise HTTPException(status_code=503, detail=f"prediction unavailable: {_prediction_error}")
    return {"status": _predictor.status(), "predictions": _predictor.predictions()}
