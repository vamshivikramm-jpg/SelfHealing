"""Event model for the Network Control Layer (Submodule 1.3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict

from network.events.event_types import EventType


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Event:
    """Immutable event published by the NetworkController.

    Attributes:
        type: one of :class:`EventType`
        source: origin of the event (e.g. "network.controller")
        timestamp: UTC ISO-8601 timestamp of publication
        data: payload describing the operation (node/link names, ...)
        result: outcome of the operation ("success" for now)
    """

    type: EventType
    source: str
    timestamp: str = field(default_factory=_utc_now)
    data: Dict[str, Any] = field(default_factory=dict)
    result: str = "success"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type.value,
            "source": self.source,
            "timestamp": self.timestamp,
            "data": dict(self.data),
            "result": self.result,
        }

    def __str__(self) -> str:
        return f"Event({self.type.value}, {self.timestamp})"
