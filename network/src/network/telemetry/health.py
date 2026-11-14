"""Health evaluation (Submodule 2.4).

Turns the latest telemetry of each link and node into a health status:

- ``DOWN``      the target is not working (status metric is 0)
- ``DEGRADED``  working, but at least one metric crossed a threshold
- ``HEALTHY``   working and every metric is within its threshold
- ``UNKNOWN``   no status reading for the target yet

Each report lists the reasons, so later modules (and people) can see
*why* a target is degraded. Thresholds are deliberately simple, static
values; learned baselines and prediction are Modules 3-4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple

from network.telemetry.metrics import Metric, Sample, TargetKind


class HealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Thresholds:
    """A metric above its threshold marks the target DEGRADED."""

    latency_ms: float = 50.0
    packet_loss_pct: float = 2.0
    utilization_pct: float = 85.0
    drop_rate_pps: float = 100.0
    cpu_pct: float = 90.0
    memory_pct: float = 90.0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class HealthReport:
    target: str
    target_kind: TargetKind
    status: HealthStatus
    timestamp: Optional[float]
    reasons: Tuple[str, ...] = ()
    metrics: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "target_kind": self.target_kind.value,
            "status": self.status.value,
            "timestamp": self.timestamp,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
        }


_STATUS_METRIC = {TargetKind.LINK: Metric.LINK_STATUS, TargetKind.NODE: Metric.NODE_STATUS}

# metric -> (threshold attribute, human label)
_CHECKS = {
    TargetKind.LINK: (
        (Metric.LINK_LATENCY_MS, "latency_ms", "latency"),
        (Metric.LINK_PACKET_LOSS_PCT, "packet_loss_pct", "packet loss"),
        (Metric.LINK_UTILIZATION_PCT, "utilization_pct", "utilization"),
        (Metric.LINK_DROP_RATE_PPS, "drop_rate_pps", "drop rate"),
    ),
    TargetKind.NODE: (
        (Metric.NODE_CPU_PCT, "cpu_pct", "CPU"),
        (Metric.NODE_MEMORY_PCT, "memory_pct", "memory"),
    ),
}


class HealthEvaluator:
    """Stateless threshold-based health evaluation."""

    def __init__(self, thresholds: Optional[Thresholds] = None):
        self.thresholds = thresholds or Thresholds()

    def evaluate(self, samples: Iterable[Sample]) -> Dict[str, HealthReport]:
        """Evaluate the latest sample per (metric, target).

        Returns a dict keyed by ``"<kind>:<target>"`` (e.g. ``link:h1-sw-a``).
        """
        latest: Dict[Tuple[TargetKind, str], Dict[Metric, Sample]] = {}
        for sample in samples:
            bucket = latest.setdefault((sample.target_kind, sample.target), {})
            current = bucket.get(sample.metric)
            if current is None or sample.timestamp >= current.timestamp:
                bucket[sample.metric] = sample

        reports: Dict[str, HealthReport] = {}
        for (kind, target), metrics in sorted(latest.items(), key=lambda kv: (kv[0][0].value, kv[0][1])):
            reports[report_key(kind, target)] = self._evaluate_target(kind, target, metrics)
        return reports

    def _evaluate_target(self, kind, target, metrics: Dict[Metric, Sample]) -> HealthReport:
        values = {m.value: s.value for m, s in metrics.items()}
        timestamp = max(s.timestamp for s in metrics.values())
        status_sample = metrics.get(_STATUS_METRIC[kind])
        if status_sample is None:
            return HealthReport(target, kind, HealthStatus.UNKNOWN, timestamp,
                                ("no status reading",), values)
        if status_sample.value < 1.0:
            return HealthReport(target, kind, HealthStatus.DOWN, timestamp,
                                (f"{kind.value} is down",), values)

        reasons: List[str] = []
        for metric, attribute, label in _CHECKS[kind]:
            sample = metrics.get(metric)
            if sample is None:
                continue
            limit = getattr(self.thresholds, attribute)
            if sample.value > limit:
                reasons.append(
                    f"{label} {sample.value:.2f} {sample.unit} > {limit:g} {sample.unit}"
                )
        status = HealthStatus.DEGRADED if reasons else HealthStatus.HEALTHY
        return HealthReport(target, kind, status, timestamp, tuple(reasons), values)


def report_key(kind: TargetKind, target: str) -> str:
    return f"{TargetKind(kind).value}:{target}"
