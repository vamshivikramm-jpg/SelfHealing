"""Decision intents (Submodule 6.2) — the Network Decision Ontology (NDO).

An intent says *what* should happen, not *how*: Module 7 (self-healing)
translates intents into controller / OpenFlow actions and checks each
intent's post-conditions afterwards. Keeping decisions vendor-neutral is
the research document's §1.3 contribution.

Post-conditions are plain dicts, e.g.::

    {"check": "link_up", "link": "h1-sw-a"}
    {"check": "node_up", "node": "sw-a"}
    {"check": "route_avoids", "src": "h4", "dst": "srv-web-1", "link": "sw-a-sw-b"}
    {"check": "utilization_below", "link": "rtr-core-sw-a", "pct": 70}
    {"check": "access_blocked", "subject": "h5"}
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

_ids = itertools.count(1)


class IntentType(str, Enum):
    RESTORE = "RESTORE"
    REROUTE = "REROUTE"
    LOAD_BALANCE = "LOAD_BALANCE"
    QUARANTINE = "QUARANTINE"
    ISOLATE = "ISOLATE"
    BLOCK = "BLOCK"
    MONITOR = "MONITOR"


# Higher wins when two intents compete for the same target.
PRIORITY = {
    IntentType.QUARANTINE: 90, IntentType.ISOLATE: 90, IntentType.BLOCK: 85,
    IntentType.RESTORE: 70, IntentType.REROUTE: 60, IntentType.LOAD_BALANCE: 50, IntentType.MONITOR: 10,
}
REVERSIBLE = {IntentType.RESTORE, IntentType.REROUTE, IntentType.LOAD_BALANCE, IntentType.QUARANTINE,
              IntentType.BLOCK, IntentType.MONITOR}
SECURITY = {IntentType.QUARANTINE, IntentType.ISOLATE, IntentType.BLOCK}
ACTIONS = set(IntentType) - {IntentType.MONITOR}


class IntentStatus(str, Enum):
    APPROVED = "APPROVED"                  # may be executed now
    PENDING_APPROVAL = "PENDING_APPROVAL"  # waits for a person (human gate)
    REJECTED = "REJECTED"
    RESOLVED = "RESOLVED"                  # the condition that caused it is gone
    SUPERSEDED = "SUPERSEDED"


@dataclass
class Intent:
    type: IntentType
    target: str                 # health key, e.g. "link:h1-sw-a", "node:h5"
    reason: str
    risk: float
    ts: float
    params: Dict[str, Any] = field(default_factory=dict)
    postconditions: List[dict] = field(default_factory=list)
    status: IntentStatus = IntentStatus.APPROVED
    approval_reason: Optional[str] = None
    id: str = field(default_factory=lambda: f"intent-{next(_ids)}")

    @property
    def priority(self) -> int:
        return PRIORITY[self.type]

    @property
    def reversible(self) -> bool:
        return self.type in REVERSIBLE

    def to_dict(self) -> dict:
        return {
            "id": self.id, "type": self.type.value, "target": self.target, "reason": self.reason,
            "risk": self.risk, "ts": self.ts, "params": self.params, "postconditions": self.postconditions,
            "status": self.status.value, "approval_reason": self.approval_reason,
            "priority": self.priority, "reversible": self.reversible,
        }
