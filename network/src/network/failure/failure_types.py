"""Failure data model (Submodule 1.4A).

Strongly typed failure objects and their enums. Every failure carries the
common fields (id, timestamp, severity, affected_nodes, state,
recovered_at, injected_by, metadata) plus type-specific fields.

This module depends only on the standard library and
:mod:`network.failure.exceptions`; it never touches the network layer.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4

from network.failure.exceptions import (
    InvalidBandwidthError,
    InvalidLatencyError,
    InvalidPacketLossError,
)


class FailureState(str, Enum):
    """Lifecycle state of an injected failure."""

    ACTIVE = "ACTIVE"
    RECOVERED = "RECOVERED"


class Severity(str, Enum):
    """Impact severity of a failure."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class FailureType(str, Enum):
    """Kinds of failures supported by the injection framework."""

    ROUTER = "ROUTER"
    SWITCH = "SWITCH"
    LINK = "LINK"
    LATENCY = "LATENCY"
    PACKET_LOSS = "PACKET_LOSS"
    BANDWIDTH = "BANDWIDTH"


def _new_id() -> str:
    return uuid4().hex


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Failure:
    """Base class for every failure object (abstract).

    Subclasses add type-specific fields and set :attr:`_failure_type`.
    """

    severity: Severity = Severity.MEDIUM
    affected_nodes: list = field(default_factory=list)
    state: FailureState = FailureState.ACTIVE
    id: str = field(default_factory=_new_id)
    timestamp: str = field(default_factory=_utc_now)
    recovered_at: Optional[str] = None
    injected_by: str = ""
    metadata: dict = field(default_factory=dict)

    _failure_type = None

    @property
    def failure_type(self) -> FailureType:
        """The concrete :class:`FailureType` of this failure."""
        return self._failure_type

    def __post_init__(self) -> None:
        if not isinstance(self.severity, Severity):
            self.severity = Severity(self.severity)
        if not isinstance(self.state, FailureState):
            self.state = FailureState(self.state)

    def to_dict(self) -> dict:
        """Serializable dict, merging base fields with type-specific ones."""
        data = {
            "failure_type": self.failure_type.value,
            "id": self.id,
            "timestamp": self.timestamp,
            "severity": self.severity.value,
            "affected_nodes": list(self.affected_nodes),
            "state": self.state.value,
            "recovered_at": self.recovered_at,
            "injected_by": self.injected_by,
            "metadata": dict(self.metadata),
        }
        for dataclass_field in dataclasses.fields(self):
            if dataclass_field.name in data or dataclass_field.name.startswith("_"):
                continue
            data[dataclass_field.name] = getattr(self, dataclass_field.name)
        return data


@dataclass
class RouterFailure(Failure):
    """A router device failure (router down / crash)."""

    router: str = ""
    _failure_type = FailureType.ROUTER

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.router:
            raise ValueError("router name is required")
        if not self.affected_nodes:
            self.affected_nodes = [self.router]


@dataclass
class SwitchFailure(Failure):
    """A switch device failure (switch down / crash)."""

    switch: str = ""
    _failure_type = FailureType.SWITCH

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.switch:
            raise ValueError("switch name is required")
        if not self.affected_nodes:
            self.affected_nodes = [self.switch]


@dataclass
class LinkFailure(Failure):
    """A link failure between two devices (cable cut / port down)."""

    src: str = ""
    dst: str = ""
    _failure_type = FailureType.LINK

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.src or not self.dst:
            raise ValueError("link failure requires src and dst")
        if self.src == self.dst:
            raise ValueError("link failure src and dst must differ")
        if not self.affected_nodes:
            self.affected_nodes = [self.src, self.dst]

    @property
    def link(self) -> str:
        """Canonical (sorted) link name, e.g. 'h1-sw-a'."""
        a, b = sorted((self.src, self.dst))
        return f"{a}-{b}"


@dataclass
class LatencyFailure(Failure):
    """Elevated latency on a device, in milliseconds."""

    node: str = ""
    latency_ms: int = 0
    _failure_type = FailureType.LATENCY

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.node:
            raise ValueError("node is required")
        if self.latency_ms <= 0:
            raise InvalidLatencyError(
                f"latency must be positive (got {self.latency_ms} ms)"
            )
        if not self.affected_nodes:
            self.affected_nodes = [self.node]


@dataclass
class PacketLossFailure(Failure):
    """Elevated packet loss on a device, in percent."""

    node: str = ""
    loss_percent: int = 0
    _failure_type = FailureType.PACKET_LOSS

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.node:
            raise ValueError("node is required")
        if not 0 <= self.loss_percent <= 100:
            raise InvalidPacketLossError(
                f"packet loss must be within [0, 100] (got {self.loss_percent})"
            )
        if not self.affected_nodes:
            self.affected_nodes = [self.node]


@dataclass
class BandwidthFailure(Failure):
    """Reduced bandwidth on a device, in bits per second."""

    node: str = ""
    bandwidth_bps: int = 0
    _failure_type = FailureType.BANDWIDTH

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.node:
            raise ValueError("node is required")
        if self.bandwidth_bps <= 0:
            raise InvalidBandwidthError(
                f"bandwidth must be positive (got {self.bandwidth_bps} bps)"
            )
        if not self.affected_nodes:
            self.affected_nodes = [self.node]
