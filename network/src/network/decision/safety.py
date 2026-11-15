"""Safety checks and prioritisation (Submodule 6.5).

Applied to candidate intents before anything is executed:

1. **Conflicts**: per target keep the highest-priority intent. RESTORE and
   REROUTE may coexist (fix the link *and* move traffic meanwhile);
   MONITOR is dropped when any real action exists for the target.
2. **De-duplication**: an intent of the same type for the same target that
   is still active is not issued again.
3. **Cooldown**: after an intent is resolved, the same type on the same
   target is suppressed for ``cooldown_s`` (prevents flapping). RESTORE,
   security actions and anything triggered by a hard fault (``params
   ["fault"] == "DOWN"``) are exempt: a dead link or an attacker is never
   left alone because of a timer (e.g. an earlier proactive reroute on the
   same link must not delay the reroute when it actually fails).
4. **Human gate** (autonomy level):
   L1 every action waits for approval; L3 nothing does; L2 waits for
   approval for ``always_approve`` types, security actions on protected
   device kinds (servers, switches, routers), and quarantines beyond
   ``max_quarantined`` devices at once.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

from network.decision.intents import ACTIONS, SECURITY, Intent, IntentStatus, IntentType

COEXIST = {frozenset({IntentType.RESTORE, IntentType.REROUTE})}
COOLDOWN_EXEMPT = {IntentType.RESTORE} | SECURITY


def resolve_conflicts(candidates: Iterable[Intent]) -> List[Intent]:
    by_target: Dict[str, List[Intent]] = {}
    for intent in candidates:
        by_target.setdefault(intent.target, []).append(intent)
    kept: List[Intent] = []
    for target, items in sorted(by_target.items()):
        actions = [i for i in items if i.type in ACTIONS]
        if not actions:
            kept.append(max(items, key=lambda i: i.priority))
            continue
        actions.sort(key=lambda i: -i.priority)
        chosen = [actions[0]]
        for other in actions[1:]:
            if all(frozenset({other.type, c.type}) in COEXIST for c in chosen):
                chosen.append(other)
        kept.extend(chosen)
    return kept


class SafetyGuard:
    def __init__(self, policy, kinds: Dict[str, str]):
        self.policy = policy
        self.kinds = kinds
        self.suppressed = 0
        self._resolved_at: Dict[Tuple[str, str], float] = {}

    def note_resolved(self, intent: Intent, ts: float) -> None:
        self._resolved_at[(intent.type.value, intent.target)] = ts

    def in_cooldown(self, intent: Intent, ts: float) -> bool:
        if intent.type in COOLDOWN_EXEMPT or intent.params.get("fault") == "DOWN":
            return False
        last = self._resolved_at.get((intent.type.value, intent.target))
        return last is not None and ts - last < self.policy.cooldown_s

    def gate(self, intent: Intent, active_quarantines: int) -> Intent:
        """Set APPROVED or PENDING_APPROVAL (with the reason)."""
        p = self.policy
        reason = None
        if intent.type not in ACTIONS:
            reason = None
        elif p.autonomy == "L1":
            reason = "autonomy L1: every action needs approval"
        elif p.autonomy == "L2":
            node = intent.target.split(":", 1)[1]
            if intent.type.value in p.always_approve:
                reason = f"{intent.type.value} always needs approval"
            elif intent.type in SECURITY and self.kinds.get(node) in p.protected_kinds:
                reason = f"security action on protected {self.kinds.get(node)} {node}"
            elif intent.type == IntentType.QUARANTINE and active_quarantines >= p.max_quarantined:
                reason = f"more than {p.max_quarantined} devices would be quarantined"
        intent.status = IntentStatus.PENDING_APPROVAL if reason else IntentStatus.APPROVED
        intent.approval_reason = reason
        return intent
