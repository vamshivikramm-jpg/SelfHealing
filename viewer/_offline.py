"""Offline controller — no Mininet dependency.

Used when the viewer runs on macOS (or any platform where Mininet is
unavailable).  Reads topology from YAML, exposes the same read-only
API surface as the live NetworkController + FailureManager so the
dashboard works identically.

Import strategy: pre-populates sys.modules with lightweight stub packages
for `network`, `network.state`, `network.failure`, `network.events`, etc.
then loads the specific leaf modules via importlib.util — never triggering
network/__init__.py (which imports NetworkController → Mininet).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

_REPO_ROOT = Path(__file__).resolve().parent.parent
_NETWORK_SRC = _REPO_ROOT / "network" / "src"

# ------------------------------------------------------------------
# 1.  Create empty stub packages in sys.modules so that absolute
#     imports like ``from network.failure.exceptions import …``
#     resolve without triggering network/__init__.py.
# ------------------------------------------------------------------

def _stub_package(name: str) -> types.ModuleType:
    """Create (or return existing) empty package stub."""
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    mod.__path__ = []          # mark as package
    mod.__package__ = name
    sys.modules[name] = mod
    return mod

_stub_package("network")
_stub_package("network.config")
_stub_package("network.state")
_stub_package("network.failure")
_stub_package("network.events")
_stub_package("network.control")

# ------------------------------------------------------------------
# 2.  Load individual leaf modules via importlib.util
# ------------------------------------------------------------------

def _load(name: str, rel: str):
    fp = str(_NETWORK_SRC / rel)
    spec = importlib.util.spec_from_file_location(name, fp)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

# Load in dependency order — earlier modules don't trigger network/__init__.py
_exceptions_mod    = _load("network.failure.exceptions",
                           "network/failure/exceptions.py")
_config_mod        = _load("network.config._impl",
                           "network/config.py")
_state_mod         = _load("network.state.network_state",
                           "network/state/network_state.py")
_ft_mod            = _load("network.failure.failure_types",
                           "network/failure/failure_types.py")
_fr_mod            = _load("network.failure.failure_registry",
                           "network/failure/failure_registry.py")
_ev_mod            = _load("network.events.event",
                           "network/events/event.py")
_et_mod            = _load("network.events.event_types",
                           "network/events/event_types.py")
_eb_mod            = _load("network.events.event_bus",
                           "network/events/event_bus.py")

# Module 2 telemetry (standard library + events only; no Mininet)
_stub_package("network.telemetry")
_stub_package("network.telemetry.sources")
_load("network.logging", "network/logging.py")
_metrics_mod       = _load("network.telemetry.metrics",
                           "network/telemetry/metrics.py")
_load("network.telemetry.store", "network/telemetry/store.py")
_load("network.telemetry.derive", "network/telemetry/derive.py")
_load("network.telemetry.health", "network/telemetry/health.py")
_load("network.telemetry.sources.base", "network/telemetry/sources/base.py")
_sim_mod           = _load("network.telemetry.sources.simulated",
                           "network/telemetry/sources/simulated.py")
_collector_mod     = _load("network.telemetry.collector",
                           "network/telemetry/collector.py")

# Module 3 analytics (storage + summaries only; no Mininet)
_stub_package("network.analytics")
_repo_mod          = _load("network.analytics.repository",
                           "network/analytics/repository.py")
_recorder_mod      = _load("network.analytics.recorder",
                           "network/analytics/recorder.py")
_load("network.analytics.preprocess", "network/analytics/preprocess.py")
_summary_mod       = _load("network.analytics.summary",
                           "network/analytics/summary.py")


def load_security():
    """Load the Module 5 Zero Trust modules (standard library + PyYAML).

    Returns ``(IdentityRegistry, ZeroTrustPolicy, LiveSecuritySimulation)``.
    """
    _stub_package("network.security")
    identity = _load("network.security.identity", "network/security/identity.py")
    _load("network.security.records", "network/security/records.py")
    policy = _load("network.security.policy", "network/security/policy.py")
    _load("network.security.trust", "network/security/trust.py")
    _load("network.security.detectors", "network/security/detectors.py")
    _load("network.security.enforcement", "network/security/enforcement.py")
    _load("network.security.traffic", "network/security/traffic.py")
    _load("network.security.engine", "network/security/engine.py")
    live = _load("network.security.live", "network/security/live.py")
    return identity.IdentityRegistry, policy.ZeroTrustPolicy, live.LiveSecuritySimulation


def load_decision():
    """Load the Module 6 decision modules (standard library + PyYAML).

    Returns ``(DecisionPolicy, LiveDecisions)``.
    """
    _stub_package("network.decision")
    policy = _load("network.decision.policy", "network/decision/policy.py")
    for leaf in ("graph", "intents", "risk", "strategies", "safety", "engine"):
        _load(f"network.decision.{leaf}", f"network/decision/{leaf}.py")
    live = _load("network.decision.live", "network/decision/live.py")
    return policy.DecisionPolicy, live.LiveDecisions


def load_prediction():
    """Load the Module 4 live-prediction modules (needs numpy + scikit-learn).

    Returns ``(ModelRegistry, OnlinePredictor)``; raises ImportError when
    the viewer's environment lacks numpy/scikit-learn.
    """
    _stub_package("network.prediction")
    _load("network.analytics.timeseries", "network/analytics/timeseries.py")
    _load("network.analytics.features", "network/analytics/features.py")
    _load("network.prediction.models", "network/prediction/models.py")  # for unpickling
    registry = _load("network.prediction.registry", "network/prediction/registry.py")
    online = _load("network.prediction.online", "network/prediction/online.py")
    return registry.ModelRegistry, online.OnlinePredictor


# Re-export what we need
TopologyConfig      = _config_mod.TopologyConfig
load_topology_config = _config_mod.load_topology_config
LINK_DISCONNECTED   = _state_mod.LINK_DISCONNECTED
NODE_OFFLINE        = _state_mod.NODE_OFFLINE
LinkState           = _state_mod.LinkState
NetworkState        = _state_mod.NetworkState
_canonical_pair     = _state_mod._canonical_pair
FailureRegistry     = _fr_mod.FailureRegistry
Failure             = _ft_mod.Failure
FailureState        = _ft_mod.FailureState
Event               = _ev_mod.Event
EventBus            = _eb_mod.EventBus
EventType           = _et_mod.EventType
SimulatedSource     = _sim_mod.SimulatedSource
TelemetryCollector  = _collector_mod.TelemetryCollector
Metric              = _metrics_mod.Metric
METRIC_SPECS        = _metrics_mod.METRIC_SPECS
TelemetryRepository = _repo_mod.TelemetryRepository
TelemetryRecorder   = _recorder_mod.TelemetryRecorder
network_summary     = _summary_mod.network_summary

SOURCE = "network.controller.offline"


class OfflineFailureManager:
    """Read-only failure manager backed by FailureRegistry only."""

    def __init__(self, registry: FailureRegistry) -> None:
        self._registry = registry

    def active_failures(self) -> List[Failure]:
        return self._registry.active_failures()


class OfflineController:
    """Lightweight read-only stand-in for NetworkController.

    Loads the topology YAML, registers all devices and links into a
    fresh ``NetworkState``, and exposes the introspection methods that
    the viewer REST endpoints rely on.  No Mininet import anywhere.
    """

    def __init__(self, config_path: str) -> None:
        self._config: TopologyConfig = load_topology_config(config_path)
        self._state = NetworkState()
        self._event_bus = EventBus()
        self._registry = FailureRegistry()
        self._impairments: Dict[str, dict] = {}
        self._simulated_running = False
        self._register_configured_state()

    @property
    def config(self) -> TopologyConfig:
        return self._config

    @property
    def state(self) -> NetworkState:
        return self._state

    @property
    def event_bus(self) -> EventBus:
        return self._event_bus

    def topology_snapshot(self) -> dict:
        config = self._config
        link_index: Dict[tuple, LinkState] = {
            (link.src, link.dst): link for link in self._state.links.values()
        }
        return {
            "name": config.name,
            "version": config.version,
            "status": self._state.status,
            "segments": [
                {
                    "id": seg.id,
                    "label": seg.label,
                    "subnets": [
                        {"id": s.id, "cidr": s.cidr, "gateway": s.gateway}
                        for s in seg.subnets
                    ],
                }
                for seg in config.segments
            ],
            "devices": [
                {
                    "name": dev.name,
                    "kind": dev.kind,
                    "role": dev.role,
                    "subnet": dev.subnet,
                    "ip": dev.ip,
                    "gateway": dev.gateway,
                    "services": list(dev.services),
                    "interfaces": [
                        {"port": i.port, "name": i.name, "subnet": i.subnet, "ip": i.ip}
                        for i in dev.interfaces
                    ],
                    "datapath_id": dev.datapath_id,
                    "state": self._state.get_node(dev.name).state,
                }
                for dev in config.devices
            ],
            "links": [
                {
                    "src": lk.src,
                    "dst": lk.dst,
                    "src_port": lk.src_port,
                    "dst_port": lk.dst_port,
                    "state": link_index.get(
                        _canonical_pair(lk.src, lk.dst),
                        LinkState(src=lk.src, dst=lk.dst),
                    ).state,
                }
                for lk in config.links
            ],
        }

    def network_status(self) -> dict:
        return {
            "status": self._state.status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "nodes": [
                n.to_dict()
                for n in sorted(self._state.nodes.values(), key=lambda n: n.name)
            ],
            "links": [
                l.to_dict()
                for l in sorted(self._state.links.values(), key=lambda l: l.name)
            ],
        }

    def impairments(self) -> dict:
        return dict(self._impairments)

    @property
    def simulated_running(self) -> bool:
        return self._simulated_running

    def simulate_running(self) -> None:
        """Mark the (non-existent) network as running for simulated telemetry.

        Offline preview only: there is no data plane. This lets the
        simulated telemetry source show a healthy baseline instead of an
        all-DOWN network. Every API response flags the simulation.
        """
        self._simulated_running = True
        self._state.mark_all_nodes(_state_mod.NODE_ONLINE)
        self._state.mark_all_links(_state_mod.LINK_CONNECTED)
        self._state.set_running()

    def _register_configured_state(self) -> None:
        for dev in self._config.devices:
            self._state.register_device(dev.name, dev.kind)
        for link in self._config.links:
            self._state.register_link(link.src, link.dst)
