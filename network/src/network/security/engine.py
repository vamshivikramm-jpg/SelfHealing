"""Zero Trust engine (Submodule 5.7).

One ``step(ts, flows, auth)`` runs the whole loop for a time step:

1. take every device's current trust and level (from earlier steps);
2. compile switch rules for that trust and apply them (enforcer);
3. decide every flow with the policy (PDP) and run it through the
   enforcer (PEP); the two must agree, disagreements are counted;
4. run the detectors on the step's flows, decisions and logins;
5. feed the anomalies into the trust engine and update levels.

Events published on the EventBus (stamped with ``ts``):
``SECURITY_ANOMALY`` (one per anomaly), ``ACCESS_DENIED`` (one per denied
sender per step, with counts per reason), ``TRUST_CHANGED`` (level
changes), ``DEVICE_QUARANTINED`` / ``DEVICE_RELEASED``.

Manual blocks (Module 7): ``add_block`` / ``remove_block`` let the
self-healing engine block a device regardless of its trust; a blocked
device is enforced exactly like a quarantined one.
"""

from __future__ import annotations

import threading
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Tuple

from network.events.event import Event
from network.events.event_types import EventType
from network.security.detectors import DetectorSuite, ThreatIndex
from network.security.enforcement import SimulatedEnforcer, compile_rules
from network.security.trust import TrustEngine, TrustLevel

SOURCE = "security.zero_trust"


@dataclass
class StepResult:
    ts: float
    decided: List[tuple] = field(default_factory=list)   # (flow, decision, enforced_allowed)
    anomalies: list = field(default_factory=list)
    level_changes: list = field(default_factory=list)
    threat: Optional[ThreatIndex] = None
    mismatches: int = 0
    rule_changes: int = 0


class ZeroTrustEngine:
    def __init__(self, registry, policy, event_bus=None, recent_events: int = 500):
        self.registry = registry
        self.policy = policy
        self.event_bus = event_bus
        self.trust = TrustEngine(registry, policy.trust, policy.anomaly_weights)
        self.detectors = DetectorSuite(registry.names())
        self.enforcer = SimulatedEnforcer()
        self._recent: Deque[dict] = deque(maxlen=recent_events)
        self._lock = threading.RLock()
        self.steps = 0
        self.mismatches = 0
        self.last_threat: Optional[ThreatIndex] = None
        self.last_ts: Optional[float] = None
        self.manual_blocks: set = set()

    def add_block(self, subject: str) -> bool:
        """Block a device's traffic until ``remove_block``; True if newly blocked."""
        if self.registry.get(subject) is None:
            raise KeyError(f"unknown device {subject!r}")
        with self._lock:
            if subject in self.manual_blocks:
                return False
            self.manual_blocks.add(subject)
            self._apply_rules()
            return True

    def remove_block(self, subject: str) -> bool:
        with self._lock:
            if subject not in self.manual_blocks:
                return False
            self.manual_blocks.discard(subject)
            self._apply_rules()
            return True

    def _apply_rules(self):
        ts = self.last_ts if self.last_ts is not None else 0.0
        trust_now = {s: self.trust.trust(s, ts) for s in self.registry.names()}
        blocked = set(self.quarantined()) | self.manual_blocks
        return self.enforcer.apply(compile_rules(self.policy, self.registry, trust_now, blocked))

    def quarantined(self) -> List[str]:
        return [s for s in self.registry.names() if self.trust.level(s) == TrustLevel.QUARANTINED]

    def step(self, ts: float, flows, auth) -> StepResult:
        with self._lock:
            result = StepResult(ts=ts)
            trust_now = {s: self.trust.trust(s, ts) for s in self.registry.names()}
            quarantined = set(self.quarantined()) | self.manual_blocks
            diff = self.enforcer.apply(compile_rules(self.policy, self.registry, trust_now, quarantined))
            result.rule_changes = len(diff["added"]) + len(diff["removed"])

            for flow in flows:
                decision = self.policy.decide(self.registry, flow.src, flow.src_ip, flow.src_mac, flow.dst,
                                              flow.dst_port, trust_now.get(flow.src, 0.0),
                                              flow.src in quarantined)
                enforced, _ = self.enforcer.admit(flow)
                if enforced != decision.allowed:
                    result.mismatches += 1
                result.decided.append((flow, decision, enforced))

            anomalies, threat = self.detectors.process(ts, [(f, d) for f, d, _ in result.decided], auth)
            self.trust.add(anomalies)
            result.anomalies = anomalies
            result.threat = threat
            result.level_changes = self.trust.update_levels(ts)
            self.steps += 1
            self.mismatches += result.mismatches
            self.last_threat = threat
            self.last_ts = ts
        self._publish_step(result)
        return result

    # -- events --------------------------------------------------------------

    def _publish_step(self, r: StepResult) -> None:
        for a in r.anomalies:
            self._publish(EventType.SECURITY_ANOMALY, r.ts, {"subject": a.subject, "kind": a.kind,
                                                             "magnitude": a.magnitude, "detail": a.detail})
        denied: Dict[str, Counter] = {}
        for flow, decision, _ in r.decided:
            if not decision.allowed:
                denied.setdefault(flow.src, Counter())[decision.code] += 1
        for src, codes in sorted(denied.items()):
            self._publish(EventType.ACCESS_DENIED, r.ts, {"subject": src, "count": sum(codes.values()),
                                                          "codes": dict(codes)}, result="denied")
        for subject, previous, current, value in r.level_changes:
            data = {"subject": subject, "previous": previous.value, "level": current.value, "trust": value}
            self._publish(EventType.TRUST_CHANGED, r.ts, data)
            if current == TrustLevel.QUARANTINED:
                self._publish(EventType.DEVICE_QUARANTINED, r.ts, data)
            elif previous == TrustLevel.QUARANTINED:
                self._publish(EventType.DEVICE_RELEASED, r.ts, data)

    def _publish(self, event_type: EventType, ts: float, data: dict, result: str = "success") -> None:
        record = {"type": event_type.value, "ts": ts, "data": data, "result": result}
        with self._lock:
            self._recent.appendleft(record)
        if self.event_bus is not None:
            self.event_bus.publish(Event(type=event_type, source=SOURCE, data=data, result=result,
                                         timestamp=datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()))

    # -- introspection ---------------------------------------------------------

    def recent_events(self, limit: int = 100) -> List[dict]:
        with self._lock:
            return list(self._recent)[:limit]

    def status(self) -> dict:
        with self._lock:
            ts = self.last_ts
            return {
                "steps": self.steps,
                "last_ts": ts,
                "enforcement_mismatches": self.mismatches,
                "quarantined": self.quarantined(),
                "manual_blocks": sorted(self.manual_blocks),
                "threat": self.last_threat.to_dict() if self.last_threat else None,
                "trust": self.trust.snapshot(ts) if ts is not None else None,
                "rules": len(self.enforcer.rules),
            }
