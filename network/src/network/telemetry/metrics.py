"""Telemetry data model (Submodule 2.1).

A telemetry *sample* is one measurement of one metric for one target
(a link or a node) at one point in time. Every sample records which
source produced it and whether that source is simulated, so simulated
data can never be mistaken for live measurements.

Metric catalogue
----------------
Gauges are point-in-time readings; counters are cumulative totals that
only increase (until the device restarts). Rates such as throughput are
*derived* from counter deltas (see :mod:`network.telemetry.derive`),
exactly as they would be from real OpenFlow port statistics.

This module depends only on the standard library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class TargetKind(str, Enum):
    """What a sample describes."""

    LINK = "link"
    NODE = "node"


class MetricKind(str, Enum):
    """How a metric's values behave over time."""

    GAUGE = "gauge"
    COUNTER = "counter"
    DERIVED = "derived"


class Metric(str, Enum):
    """Every metric the telemetry pipeline understands."""

    # Link gauges
    LINK_STATUS = "link.status"
    LINK_LATENCY_MS = "link.latency_ms"
    LINK_PACKET_LOSS_PCT = "link.packet_loss_pct"
    LINK_CAPACITY_MBPS = "link.capacity_mbps"
    # Link counters
    LINK_BYTES = "link.bytes"
    LINK_PACKETS = "link.packets"
    LINK_DROPPED_PACKETS = "link.dropped_packets"
    # Link derived rates
    LINK_THROUGHPUT_MBPS = "link.throughput_mbps"
    LINK_UTILIZATION_PCT = "link.utilization_pct"
    LINK_DROP_RATE_PPS = "link.drop_rate_pps"
    # Node gauges
    NODE_STATUS = "node.status"
    NODE_CPU_PCT = "node.cpu_pct"
    NODE_MEMORY_PCT = "node.memory_pct"


@dataclass(frozen=True)
class MetricSpec:
    """Static description of a metric."""

    metric: Metric
    kind: MetricKind
    unit: str
    target_kind: TargetKind
    description: str


_SPECS = (
    MetricSpec(Metric.LINK_STATUS, MetricKind.GAUGE, "bool", TargetKind.LINK,
               "1 if the link carries traffic, 0 if it is down"),
    MetricSpec(Metric.LINK_LATENCY_MS, MetricKind.GAUGE, "ms", TargetKind.LINK,
               "one-way latency across the link"),
    MetricSpec(Metric.LINK_PACKET_LOSS_PCT, MetricKind.GAUGE, "%", TargetKind.LINK,
               "probe packet loss across the link"),
    MetricSpec(Metric.LINK_CAPACITY_MBPS, MetricKind.GAUGE, "Mbps", TargetKind.LINK,
               "current link capacity (configured bandwidth limit or line rate)"),
    MetricSpec(Metric.LINK_BYTES, MetricKind.COUNTER, "bytes", TargetKind.LINK,
               "cumulative bytes carried by the link"),
    MetricSpec(Metric.LINK_PACKETS, MetricKind.COUNTER, "packets", TargetKind.LINK,
               "cumulative packets carried by the link"),
    MetricSpec(Metric.LINK_DROPPED_PACKETS, MetricKind.COUNTER, "packets", TargetKind.LINK,
               "cumulative packets dropped on the link"),
    MetricSpec(Metric.LINK_THROUGHPUT_MBPS, MetricKind.DERIVED, "Mbps", TargetKind.LINK,
               "throughput derived from the byte counter"),
    MetricSpec(Metric.LINK_UTILIZATION_PCT, MetricKind.DERIVED, "%", TargetKind.LINK,
               "throughput as a percentage of capacity"),
    MetricSpec(Metric.LINK_DROP_RATE_PPS, MetricKind.DERIVED, "packets/s", TargetKind.LINK,
               "drop rate derived from the dropped-packet counter"),
    MetricSpec(Metric.NODE_STATUS, MetricKind.GAUGE, "bool", TargetKind.NODE,
               "1 if the node is up, 0 if it is down"),
    MetricSpec(Metric.NODE_CPU_PCT, MetricKind.GAUGE, "%", TargetKind.NODE,
               "CPU utilisation"),
    MetricSpec(Metric.NODE_MEMORY_PCT, MetricKind.GAUGE, "%", TargetKind.NODE,
               "memory utilisation"),
)

METRIC_SPECS: Dict[Metric, MetricSpec] = {spec.metric: spec for spec in _SPECS}


def spec_for(metric: Metric) -> MetricSpec:
    """Return the :class:`MetricSpec` for ``metric``."""
    return METRIC_SPECS[Metric(metric)]


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class Sample:
    """One measurement of one metric for one target.

    Attributes:
        metric: the :class:`Metric` measured
        target: link name (canonical ``a-b``) or node name
        value: numeric value in the metric's unit
        timestamp: UNIX epoch seconds (UTC)
        source: name of the telemetry source that produced the sample
        simulated: True when the value did not come from a real network
        labels: optional extra dimensions (e.g. ``{"src": ..., "dst": ...}``)
    """

    metric: Metric
    target: str
    value: float
    timestamp: float
    source: str
    simulated: bool
    labels: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.metric, Metric):
            object.__setattr__(self, "metric", Metric(self.metric))
        object.__setattr__(self, "value", float(self.value))

    @property
    def spec(self) -> MetricSpec:
        return METRIC_SPECS[self.metric]

    @property
    def target_kind(self) -> TargetKind:
        return self.spec.target_kind

    @property
    def unit(self) -> str:
        return self.spec.unit

    @property
    def key(self) -> Tuple[Metric, str]:
        """Series key: one time series per (metric, target)."""
        return (self.metric, self.target)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric.value,
            "target": self.target,
            "target_kind": self.target_kind.value,
            "value": self.value,
            "unit": self.unit,
            "timestamp": self.timestamp,
            "time": _iso(self.timestamp),
            "source": self.source,
            "simulated": self.simulated,
            "labels": dict(self.labels),
        }


def make_sample(
    metric: Metric,
    target: str,
    value: float,
    timestamp: float,
    source: str,
    simulated: bool,
    labels: Optional[Dict[str, Any]] = None,
) -> Sample:
    """Convenience constructor."""
    return Sample(
        metric=metric,
        target=target,
        value=value,
        timestamp=timestamp,
        source=source,
        simulated=simulated,
        labels=dict(labels or {}),
    )
