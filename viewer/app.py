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
  VIEWER_SIMULATE_SECURITY=1    Module 5: run simulated traffic through the
                                Zero Trust engine (live trust + events)
  VIEWER_SECURITY_ATTACKS=1     ...and include synthetic attacks
  VIEWER_DECISIONS=1            Module 6: run the decision engine on live signals
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
_SIMULATE_SECURITY = os.environ.get("VIEWER_SIMULATE_SECURITY", "") == "1"
_SECURITY_ATTACKS = os.environ.get("VIEWER_SECURITY_ATTACKS", "") == "1"
_CONFIG_DIR = Path(_CONFIG_PATH).parent
_identities = None
_policy = None
_security = None
_security_error = None
_DECISIONS = os.environ.get("VIEWER_DECISIONS", "") == "1"
_decisions = None
_decision_error = None
_prediction_error = None

# High-frequency events that would flood the dashboard event log.
_UNLOGGED_EVENTS = {"TELEMETRY_COLLECTED"}


def _ensure_singletons() -> None:
    global _controller, _manager, _collector, _repository, _recorder, _predictor, _prediction_error
    global _identities, _policy, _security, _security_error, _decisions, _decision_error
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
    try:
        if _OFFLINE_MODE:
            from viewer._offline import load_security
            IdentityRegistry, ZeroTrustPolicy, LiveSecuritySimulation = load_security()
        else:
            from network.security.identity import IdentityRegistry
            from network.security.live import LiveSecuritySimulation
            from network.security.policy import ZeroTrustPolicy
        _identities = IdentityRegistry.from_config(_controller.config, _CONFIG_DIR / "identities.yaml")
        _policy = ZeroTrustPolicy.from_file(_CONFIG_DIR / "zero_trust_policy.yaml")
        if _SIMULATE_SECURITY:
            _security = LiveSecuritySimulation(
                _identities, _policy, _controller.event_bus,
                interval_s=_TELEMETRY_INTERVAL, attacks=_SECURITY_ATTACKS,
            )
    except Exception as exc:  # noqa: BLE001 - reported by the security endpoints
        _security_error = f"{type(exc).__name__}: {exc}"
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
    if _DECISIONS:
        try:
            if _OFFLINE_MODE:
                from viewer._offline import load_decision
                DecisionPolicy, LiveDecisions = load_decision()
            else:
                from network.decision.live import LiveDecisions
                from network.decision.policy import DecisionPolicy
            _decisions = LiveDecisions(
                _controller.config, DecisionPolicy.from_file(_CONFIG_DIR / "decision_policy.yaml"),
                _collector, _controller.event_bus, predictor=_predictor,
                security=_security.engine if _security is not None else None,
            ).attach()
        except Exception as exc:  # noqa: BLE001 - reported by /api/decisions
            _decision_error = f"{type(exc).__name__}: {exc}"


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
    if _security is not None:
        _security.start()
    try:
        yield
    finally:
        _collector.stop()
        if _security is not None:
            _security.stop()
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


# ---------------------------------------------------------------------------
# Zero Trust API (Module 5)
# ---------------------------------------------------------------------------

@app.get("/api/policy")
async def zero_trust_policy():
    _ensure_singletons()
    if _policy is None:
        raise HTTPException(status_code=503, detail=f"Zero Trust config unavailable: {_security_error}")
    return {
        "policy": _policy.to_dict(),
        "identities": [i.to_dict() for i in _identities.all()],
    }


def _require_security():
    _ensure_singletons()
    if _security is None:
        raise HTTPException(
            status_code=404,
            detail="Zero Trust simulation is off; start the viewer with VIEWER_SIMULATE_SECURITY=1"
            + (f" ({_security_error})" if _security_error else ""),
        )
    return _security


@app.get("/api/trust")
async def trust_status():
    return _require_security().status()


@app.get("/api/security/events")
async def security_events(limit: int = 100):
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    sim = _require_security()
    return {"simulated": True, "synthetic_attacks": sim.attacks, "events": sim.engine.recent_events(limit)}


# ---------------------------------------------------------------------------
# Decision API (Module 6) — read-only; needs VIEWER_DECISIONS=1
# ---------------------------------------------------------------------------

def _require_decisions():
    _ensure_singletons()
    if _decisions is None:
        raise HTTPException(
            status_code=404 if not _decision_error else 503,
            detail=_decision_error or "decision engine is off; start the viewer with VIEWER_DECISIONS=1",
        )
    return _decisions


@app.get("/api/risk")
async def risk(top: int = 20):
    if top < 1 or top > 200:
        raise HTTPException(status_code=400, detail="top must be between 1 and 200")
    d = _require_decisions()
    return {"weights": d.engine.policy.weights.__dict__, "act_above": d.engine.policy.act_above,
            "assessments": d.engine.risk_table(top)}


@app.get("/api/decisions")
async def decisions(limit: int = 50):
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 1000")
    d = _require_decisions()
    return {"status": d.status(), "active": d.engine.active_intents(), "pending_approval": d.engine.pending(),
            "log": list(d.engine.log)[:limit]}


# ---------------------------------------------------------------------------
# Self-healing API (Module 7)
# ---------------------------------------------------------------------------

@app.get("/api/healing")
async def healing():
    """Self-healing needs a network to act on. The offline preview has none,
    and the live executor (NetworkController on Mininet + Ryu REST) is not
    built yet, so the viewer reports that plainly instead of pretending."""
    raise HTTPException(
        status_code=501,
        detail=("self-healing is not available in the viewer yet: the offline preview has no network to act "
                "on and the live executor needs the Linux lab. Run the simulated closed loop with "
                "'PYTHONPATH=network/src python3 -m network.healing.evaluation --out data/module7'."),
    )
