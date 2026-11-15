"""Combined risk (Submodule 6.1), Eq. (4) of the research document:

    R(n, t) = a * p(n, t) + b * (1 - T(n, t)) + c * Thr(t) + d * I_fault(n, t)

per link and per device, where

- p        failure prediction score (Module 4). Links use their own score;
           a node uses the highest score among its links.
- T        trust (Module 5) of the host/server involved: the device itself,
           or for a link the least-trusted endpoint. Switches/routers and
           links between them have no trust term (1 - T = 0).
- Thr      network threat index (Module 5, Eq. 3), the same for all targets.
- I_fault  current fault from health (Module 2): 1.0 DOWN, 0.5 DEGRADED.

Every assessment keeps its four components so a decision can say *why*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from network.decision.graph import ENDPOINT_KINDS, NetworkGraph


@dataclass
class Signals:
    """Everything the decision engine looks at in one step."""

    ts: float
    health: Dict[str, dict] = field(default_factory=dict)       # "link:x"/"node:y" -> report dict
    predictions: Dict[str, float] = field(default_factory=dict)  # link name -> score
    trust: Dict[str, dict] = field(default_factory=dict)         # device -> {"trust", "level"}
    threat: float = 0.0
    anomalies: Dict[str, List[str]] = field(default_factory=dict)  # device -> anomaly kinds this step
    prediction_threshold: Optional[float] = None


@dataclass(frozen=True)
class RiskAssessment:
    key: str          # "link:<name>" or "node:<name>"
    target: str
    kind: str         # "link" or "node"
    risk: float
    components: Dict[str, float]
    status: str

    def to_dict(self) -> dict:
        return {"key": self.key, "target": self.target, "kind": self.kind, "risk": self.risk,
                "components": dict(self.components), "status": self.status}


def assess(graph: NetworkGraph, signals: Signals, policy) -> Dict[str, RiskAssessment]:
    w = policy.weights
    out: Dict[str, RiskAssessment] = {}

    def distrust(devices) -> float:
        values = [1.0 - float(signals.trust[d]["trust"]) for d in devices
                  if graph.kinds.get(d) in ENDPOINT_KINDS and d in signals.trust]
        return max(values) if values else 0.0

    def fault(key) -> (float, str):
        status = (signals.health.get(key) or {}).get("status", "UNKNOWN")
        return policy.fault_levels.get(status, 0.0), status

    def build(key, target, kind, p, d, f, status):
        components = {"prediction": p, "distrust": d, "threat": signals.threat, "fault": f}
        risk = w.prediction * p + w.trust * d + w.threat * signals.threat + w.fault * f
        out[key] = RiskAssessment(key, target, kind, min(1.0, max(0.0, risk)), components, status)

    for name, edge in sorted(graph.edges.items()):
        key = f"link:{name}"
        f, status = fault(key)
        build(key, name, "link", float(signals.predictions.get(name, 0.0)), distrust((edge.a, edge.b)), f, status)
    for node in sorted(graph.kinds):
        key = f"node:{node}"
        f, status = fault(key)
        p = max((float(signals.predictions.get(l, 0.0)) for l in graph.links_of(node)), default=0.0)
        build(key, node, "node", p, distrust((node,)), f, status)
    return out
