"""Live decision loop (Submodule 6.8).

Attached to a TelemetryCollector: after every collection it gathers the
latest health (Module 2), predictions (Module 4, if a predictor is
running) and trust/threat (Module 5, if a Zero Trust engine is running)
and runs one DecisionEngine step. Used by the viewer.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from network.decision.engine import DecisionEngine
from network.decision.risk import Signals


class LiveDecisions:
    def __init__(self, topology, policy, collector, event_bus=None, predictor=None, security=None):
        self.engine = DecisionEngine(topology, policy, event_bus)
        self.collector = collector
        self.predictor = predictor
        self.security = security  # a ZeroTrustEngine (or None)
        self._token: Optional[int] = None

    def attach(self) -> "LiveDecisions":
        if self._token is None:
            self._token = self.collector.add_listener(self.on_collection)
        return self

    def detach(self) -> None:
        if self._token is not None:
            self.collector.remove_listener(self._token)
            self._token = None

    def signals(self, ts: float) -> Signals:
        predictions = {}
        if self.predictor is not None:
            predictions = {p["target"]: p["score"] for p in self.predictor.predictions() if p["score"] is not None}
        trust, threat, anomalies = {}, 0.0, defaultdict(list)
        if self.security is not None:
            snap = self.security.trust.snapshot(ts)
            trust = {s: {"trust": v["trust"], "level": v["level"]} for s, v in snap.items()}
            threat = self.security.last_threat.value if self.security.last_threat else 0.0
            for e in self.security.recent_events(200):
                if e["type"] == "SECURITY_ANOMALY" and ts - e["ts"] <= self.collector.interval_s:
                    anomalies[e["data"]["subject"]].append(e["data"]["kind"])
        return Signals(ts=ts, health={k: r.to_dict() for k, r in self.collector.health().items()},
                       predictions=predictions, trust=trust, threat=threat, anomalies=dict(anomalies))

    def on_collection(self, result) -> None:
        self.engine.step(self.signals(result.timestamp))

    def status(self) -> dict:
        return {**self.engine.status(), "attached": self._token is not None,
                "inputs": {"prediction": self.predictor is not None, "zero_trust": self.security is not None}}
