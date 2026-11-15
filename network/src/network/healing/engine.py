"""Self-healing orchestration (Submodules 7.5-7.6).

Call ``step(ts)`` once per telemetry collection, after the decision
engine's step. For every APPROVED intent the decision engine holds:

1. **Execute** it (``HEALING_STARTED``). Execution errors count as a
   failed attempt.
2. **Verify** it over the following collections: all post-conditions must
   pass on ``confirmations`` consecutive collections within
   ``verify_collections`` collections (``HEALING_VERIFIED``).
   Success rate = verified / executed, counting every execution attempt
   (research doc §8.2).
3. **Undo** it if verification fails (``HEALING_ROLLED_BACK``) and retry
   after ``retry_backoff_s`` while attempts remain. A RESTORE that never
   verifies is **escalated** to a person (``ESCALATED``): software cannot
   fix the fault (e.g. a cut cable); any reroute keeps traffic flowing
   meanwhile. Other actions end as ``HEALING_FAILED``.
4. **Fail back** when the decision engine resolves the intent (the problem
   is gone): routes return to normal at once; security blocks are lifted
   only after ``block_hold_s`` so an attacker cannot flap in and out.

With ``verify=False`` (an ablation) an executed action is *claimed*
verified immediately, without looking at the network.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, FrozenSet, List, Optional

from network.decision.intents import ACTIONS, IntentStatus, IntentType
from network.events.event import Event
from network.events.event_types import EventType

SOURCE = "healing.engine"
_ids = itertools.count(1)
SECURITY_TYPES = {"QUARANTINE", "BLOCK", "ISOLATE"}
ROUTE_TYPES = {"REROUTE", "LOAD_BALANCE"}

EXECUTING, VERIFYING, VERIFIED, RETRY = "EXECUTING", "VERIFYING", "VERIFIED", "RETRY"
FAILED, ROLLED_BACK, ESCALATED, FAILED_BACK = "FAILED", "ROLLED_BACK", "ESCALATED", "FAILED_BACK"
FINAL = {FAILED, ESCALATED, FAILED_BACK}


@dataclass(frozen=True)
class HealingPolicy:
    verify: bool = True
    verify_collections: int = 3
    confirmations: int = 2
    max_attempts: int = 2
    retry_backoff_s: float = 10.0
    block_hold_s: float = 120.0
    execute_types: FrozenSet[str] = frozenset(t.value for t in ACTIONS)

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        data["execute_types"] = sorted(self.execute_types)
        return data


@dataclass
class HealingAction:
    intent: dict
    started: float
    attempts: int = 1
    state: str = EXECUTING
    passes: int = 0
    waited: int = 0
    undo: dict = field(default_factory=dict)
    detail: str = ""
    verified_at: Optional[float] = None
    finished_at: Optional[float] = None
    retry_at: Optional[float] = None
    resolved_at: Optional[float] = None
    checks: List[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: f"heal-{next(_ids)}")

    @property
    def type(self) -> str:
        return self.intent["type"]

    @property
    def target(self) -> str:
        return self.intent["target"]

    def to_dict(self) -> dict:
        return {
            "id": self.id, "intent_id": self.intent["id"], "type": self.type, "target": self.target,
            "state": self.state, "attempts": self.attempts, "started": self.started,
            "verified_at": self.verified_at, "finished_at": self.finished_at, "detail": self.detail,
            "checks": list(self.checks[-4:]), "reason": self.intent.get("reason"),
        }


class HealingEngine:
    def __init__(self, decisions, executor, verifier, policy: HealingPolicy = HealingPolicy(), event_bus=None):
        self.decisions = decisions
        self.executor = executor
        self.verifier = verifier
        self.policy = policy
        self.event_bus = event_bus
        self.actions: Dict[str, HealingAction] = {}   # by intent id
        self._lock = threading.RLock()
        self.events: List[dict] = []

    # -- main loop --------------------------------------------------------------

    def step(self, ts: float) -> None:
        with self._lock:
            active = {i["id"]: i for i in self.decisions.active_intents()}
            for action in list(self.actions.values()):
                if action.state == VERIFYING:
                    self._verify(action, ts)
                elif action.state == RETRY and action.retry_at is not None and ts >= action.retry_at:
                    action.attempts += 1
                    self._execute(action, ts)
            for intent_id, intent in active.items():
                if intent_id in self.actions or intent["status"] != IntentStatus.APPROVED.value:
                    continue
                if intent["type"] not in self.policy.execute_types or intent["type"] == IntentType.MONITOR.value:
                    continue
                action = HealingAction(intent=intent, started=ts)
                self.actions[intent_id] = action
                self._execute(action, ts)
            self._fail_back(active, ts)

    def _execute(self, action: HealingAction, ts: float) -> None:
        result = self.executor.execute(action.intent)
        action.detail = result.detail
        self._emit(EventType.HEALING_STARTED, ts, action)
        if not result.ok:
            self._failed_attempt(action, ts, f"execution failed: {result.detail}")
            return
        action.undo = result.undo
        action.passes = action.waited = 0
        if not self.policy.verify:
            action.state = VERIFIED
            action.verified_at = ts
            action.detail += " (claimed: verification disabled)"
            self._emit(EventType.HEALING_VERIFIED, ts, action)
            return
        action.state = VERIFYING

    def _verify(self, action: HealingAction, ts: float) -> None:
        results = [self.verifier.check(post, ts) for post in action.intent.get("postconditions", [])]
        ok = all(r[0] for r in results) if results else True
        action.checks.append(f"{ts:.0f}: {'pass' if ok else 'fail'}: " + "; ".join(r[1] for r in results))
        action.waited += 1
        action.passes = action.passes + 1 if ok else 0
        if action.passes >= self.policy.confirmations:
            action.state = VERIFIED
            action.verified_at = ts
            self._emit(EventType.HEALING_VERIFIED, ts, action)
        elif action.waited >= self.policy.verify_collections:
            self._failed_attempt(action, ts, "post-conditions did not hold")

    def _failed_attempt(self, action: HealingAction, ts: float, why: str) -> None:
        if action.undo:
            action.detail = f"{why}; undo: {self.executor.undo(action.intent, action.undo)}"
            action.undo = {}
            action.state = ROLLED_BACK
            self._emit(EventType.HEALING_ROLLED_BACK, ts, action)
        else:
            action.detail = why
        if action.attempts < self.policy.max_attempts:
            action.state = RETRY
            action.retry_at = ts + self.policy.retry_backoff_s
            return
        action.finished_at = ts
        if action.type == IntentType.RESTORE.value:
            action.state = ESCALATED
            self._emit(EventType.ESCALATED, ts, action)
        else:
            action.state = FAILED
            self._emit(EventType.HEALING_FAILED, ts, action)

    def _fail_back(self, active: Dict[str, dict], ts: float) -> None:
        for intent_id, action in self.actions.items():
            if action.state in FINAL or intent_id in active:
                continue
            if action.state in (EXECUTING, VERIFYING):
                continue  # finish verifying first; fail back on a later step
            if action.resolved_at is None:
                action.resolved_at = ts
            if action.type in SECURITY_TYPES and ts - action.resolved_at < self.policy.block_hold_s:
                continue
            if action.undo:
                action.detail = f"problem resolved; {self.executor.undo(action.intent, action.undo)}"
                action.undo = {}
            action.state = FAILED_BACK if action.state in (VERIFIED, VERIFYING, ROLLED_BACK, RETRY) else action.state
            action.finished_at = action.finished_at or ts

    # -- events / reporting --------------------------------------------------------

    def _emit(self, event_type: EventType, ts: float, action: HealingAction) -> None:
        data = action.to_dict()
        self.events.append({**data, "action_type": data["type"], "type": event_type.value, "ts": ts})
        if self.event_bus is not None:
            self.event_bus.publish(Event(type=event_type, source=SOURCE, data=data,
                                         result="failure" if event_type in (EventType.HEALING_FAILED,
                                                                             EventType.ESCALATED) else "success",
                                         timestamp=datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()))

    def history(self) -> List[dict]:
        with self._lock:
            return [a.to_dict() for a in sorted(self.actions.values(), key=lambda a: a.started)]

    def stats(self) -> dict:
        with self._lock:
            actions = list(self.actions.values())
            executed = sum(a.attempts for a in actions)   # every execution attempt counts
            verified = sum(1 for a in actions if a.verified_at is not None)
            rolled = sum(1 for e in self.events if e["type"] == EventType.HEALING_ROLLED_BACK.value)
            return {
                "actions": len(actions),
                "executed": executed,
                "verified": verified,
                "success_rate": verified / executed if executed else None,
                "rollbacks": rolled,
                "rollback_rate": rolled / executed if executed else None,
                "escalated": sum(1 for a in actions if a.state == ESCALATED),
                "failed": sum(1 for a in actions if a.state == FAILED),
                "in_progress": sum(1 for a in actions if a.state in (EXECUTING, VERIFYING, RETRY)),
                "verification": self.policy.verify,
            }
