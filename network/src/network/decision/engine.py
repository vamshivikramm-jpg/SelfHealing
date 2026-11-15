"""Decision engine (Submodule 6.6).

``step(signals)`` runs the research document's decision flow (§6) once:

    signals -> graph + combined risk (Eq. 4) -> strategies -> conflicts,
    de-duplication, cooldown -> human gate -> intents

and keeps the set of *active* intents. An active intent whose trigger
has disappeared (the strategy no longer proposes it) is RESOLVED; a
resolved reroute restores the original routes (fail-back).

Strategies always plan from the original (baseline) routes, so an
intent stays active exactly as long as its trigger lasts. Approved
REROUTE / LOAD_BALANCE intents update ``routes`` (the routes traffic is
assumed to use); they are *decisions only*: Module 7 executes and
verifies them.

Events: ``DECISION_MADE`` (every new intent), ``APPROVAL_REQUIRED``
(intents waiting for a person), ``INTENT_RESOLVED``. Every decision is
also kept in a bounded in-memory log.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Tuple

from network.decision.graph import NetworkGraph
from network.decision.intents import Intent, IntentStatus, IntentType
from network.decision.risk import RiskAssessment, Signals, assess
from network.decision.safety import SafetyGuard, resolve_conflicts
from network.decision.strategies import network_candidates, security_candidates
from network.events.event import Event
from network.events.event_types import EventType

SOURCE = "decision.engine"
ROUTE_TYPES = (IntentType.REROUTE, IntentType.LOAD_BALANCE)


@dataclass
class DecisionResult:
    ts: float
    assessments: Dict[str, RiskAssessment] = field(default_factory=dict)
    new: List[Intent] = field(default_factory=list)
    resolved: List[Intent] = field(default_factory=list)
    suppressed: int = 0
    latency_ms: float = 0.0


class DecisionEngine:
    def __init__(self, topology, policy, event_bus=None, log_size: int = 1000):
        self.policy = policy
        self.event_bus = event_bus
        self.base_graph = NetworkGraph.from_topology(topology)
        self.guard = SafetyGuard(policy, self.base_graph.kinds)
        self.baseline = self.base_graph.baseline_routes()
        self.routes = dict(self.baseline)
        self.active: Dict[Tuple[str, str], Intent] = {}
        self.log: Deque[dict] = deque(maxlen=log_size)
        self.latencies_ms: List[float] = []
        self.last_assessments: Dict[str, RiskAssessment] = {}
        self._lock = threading.RLock()
        self.steps = 0

    # -- main loop ------------------------------------------------------------

    def step(self, signals: Signals) -> DecisionResult:
        started = time.perf_counter()
        with self._lock:
            graph = self.base_graph.copy().apply_signals(signals.health, signals.predictions)
            assessments = assess(graph, signals, self.policy)
            # plan from the original routes: planning from already-moved routes
            # would see no traffic on a degraded link, cancel the reroute and
            # fail traffic back onto the bad link (flip-flop)
            candidates = (network_candidates(graph, signals, assessments, self.baseline, self.policy)
                          + security_candidates(graph, signals, assessments))
            candidates = resolve_conflicts(candidates)
            wanted = {(c.type.value, c.target) for c in candidates}
            result = DecisionResult(ts=signals.ts, assessments=assessments)

            for key, intent in list(self.active.items()):
                if key not in wanted:
                    intent.status = IntentStatus.RESOLVED
                    self.guard.note_resolved(intent, signals.ts)
                    self._fail_back(intent)
                    result.resolved.append(intent)
                    del self.active[key]

            for intent in candidates:
                key = (intent.type.value, intent.target)
                if key in self.active:
                    continue
                if self.guard.in_cooldown(intent, signals.ts):
                    result.suppressed += 1
                    self.guard.suppressed += 1
                    continue
                quarantines = sum(1 for k in self.active if k[0] == IntentType.QUARANTINE.value)
                self.guard.gate(intent, quarantines)
                self.active[key] = intent
                if intent.status == IntentStatus.APPROVED:
                    self._apply_routes(intent)
                result.new.append(intent)

            result.latency_ms = (time.perf_counter() - started) * 1000.0
            self.latencies_ms.append(result.latency_ms)
            self.last_assessments = assessments
            self.steps += 1
        self._publish(result)
        return result

    # -- human gate -------------------------------------------------------------

    def approve(self, intent_id: str) -> Intent:
        with self._lock:
            intent = self._pending(intent_id)
            intent.status = IntentStatus.APPROVED
            self._apply_routes(intent)
        self._emit(EventType.DECISION_MADE, intent.ts, {**intent.to_dict(), "approved_by": "operator"})
        return intent

    def reject(self, intent_id: str) -> Intent:
        with self._lock:
            intent = self._pending(intent_id)
            intent.status = IntentStatus.REJECTED
        self._log("rejected", intent)
        return intent

    def _pending(self, intent_id: str) -> Intent:
        for intent in self.active.values():
            if intent.id == intent_id:
                if intent.status != IntentStatus.PENDING_APPROVAL:
                    raise ValueError(f"{intent_id} is {intent.status.value}, not waiting for approval")
                return intent
        raise KeyError(f"no active intent {intent_id}")

    # -- routes -------------------------------------------------------------------

    def _apply_routes(self, intent: Intent) -> None:
        if intent.type in ROUTE_TYPES:
            for move in intent.params.get("moves", []):
                self.routes[(move["src"], move["dst"])] = tuple(move["new"])

    def _fail_back(self, intent: Intent) -> None:
        if intent.type in ROUTE_TYPES and intent.status != IntentStatus.REJECTED:
            for move in intent.params.get("moves", []):
                pair = (move["src"], move["dst"])
                self.routes[pair] = self.baseline[pair]

    # -- events / introspection -----------------------------------------------------

    def _publish(self, result: DecisionResult) -> None:
        for intent in result.new:
            self._emit(EventType.DECISION_MADE, result.ts, intent.to_dict())
            if intent.status == IntentStatus.PENDING_APPROVAL:
                self._emit(EventType.APPROVAL_REQUIRED, result.ts, intent.to_dict())
        for intent in result.resolved:
            self._emit(EventType.INTENT_RESOLVED, result.ts, intent.to_dict())

    def _emit(self, event_type: EventType, ts: float, data: dict) -> None:
        self._log(event_type.value, data)
        if self.event_bus is not None:
            self.event_bus.publish(Event(type=event_type, source=SOURCE, data=data,
                                         timestamp=datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()))

    def _log(self, what: str, data) -> None:
        payload = data.to_dict() if isinstance(data, Intent) else data
        with self._lock:
            self.log.appendleft({"event": what, **payload})

    def active_intents(self) -> List[dict]:
        with self._lock:
            return [i.to_dict() for _, i in sorted(self.active.items(), key=lambda kv: -kv[1].priority)]

    def pending(self) -> List[dict]:
        return [i for i in self.active_intents() if i["status"] == IntentStatus.PENDING_APPROVAL.value]

    def risk_table(self, top: Optional[int] = None) -> List[dict]:
        with self._lock:
            rows = sorted((a.to_dict() for a in self.last_assessments.values()), key=lambda r: -r["risk"])
        return rows[:top] if top else rows

    def status(self) -> dict:
        lat = sorted(self.latencies_ms)
        p95 = lat[int(0.95 * (len(lat) - 1))] if lat else None
        return {
            "steps": self.steps,
            "autonomy": self.policy.autonomy,
            "active": len(self.active),
            "pending_approval": len(self.pending()),
            "suppressed_by_cooldown": self.guard.suppressed,
            "decision_latency_ms_p95": p95,
            "rerouted_demands": sum(1 for k, v in self.routes.items() if v != self.baseline.get(k)),
        }
