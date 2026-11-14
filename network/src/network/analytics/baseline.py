"""Behavioural baselines (Submodule 3.6).

Learns what "normal" looks like for every (metric, target) series from a
period the operator marks as healthy, then scores new readings against
it. This complements Module 2's fixed thresholds: a link whose latency
jumps from 0.05 ms to 5 ms is still under the 50 ms threshold but is
far outside its own normal range.

Per series the baseline stores count, mean, std, min, max and the 50th,
95th and 99th percentiles. Counters are skipped (they only grow).

A z-score needs a non-zero spread. Series that never vary while healthy
(e.g. packet loss of exactly 0 %) get a floor of ``min_std`` so any
change is reported as a large but finite deviation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from network.analytics.preprocess import percentile
from network.telemetry.metrics import METRIC_SPECS, Metric, MetricKind, Sample

Key = Tuple[Metric, str]


@dataclass(frozen=True)
class Baseline:
    metric: Metric
    target: str
    count: int
    mean: float
    std: float
    min: float
    max: float
    p50: float
    p95: float
    p99: float
    start: float
    end: float

    def to_dict(self) -> dict:
        data = asdict(self)
        data["metric"] = self.metric.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Baseline":
        values = dict(data)
        values["metric"] = Metric(values["metric"])
        return cls(**values)


@dataclass(frozen=True)
class Deviation:
    metric: Metric
    target: str
    value: float
    zscore: float
    above_p99: bool
    baseline_mean: float
    baseline_p99: float

    def to_dict(self) -> dict:
        data = asdict(self)
        data["metric"] = self.metric.value
        return data


def _baselineable(metric: Metric) -> bool:
    return METRIC_SPECS[metric].kind is not MetricKind.COUNTER


class BaselineModel:
    """Per-series statistical baseline with z-score scoring."""

    def __init__(self, min_std: float = 1e-3, z_threshold: float = 3.0, min_samples: int = 10):
        self.min_std = min_std
        self.z_threshold = z_threshold
        self.min_samples = min_samples
        self.baselines: Dict[Key, Baseline] = {}

    # -- fitting ------------------------------------------------------------

    def fit_samples(self, samples: Iterable[Sample]) -> "BaselineModel":
        grouped: Dict[Key, List[Sample]] = {}
        for sample in samples:
            if _baselineable(sample.metric):
                grouped.setdefault(sample.key, []).append(sample)
        for key, group in grouped.items():
            if len(group) < self.min_samples:
                continue
            values = sorted(s.value for s in group)
            n = len(values)
            mean = sum(values) / n
            std = (sum((v - mean) ** 2 for v in values) / n) ** 0.5
            self.baselines[key] = Baseline(
                metric=key[0], target=key[1], count=n, mean=mean, std=std,
                min=values[0], max=values[-1],
                p50=percentile(values, 50), p95=percentile(values, 95), p99=percentile(values, 99),
                start=min(s.timestamp for s in group), end=max(s.timestamp for s in group),
            )
        return self

    def fit(self, repository, start: Optional[float] = None, end: Optional[float] = None,
            metrics: Optional[Sequence[Metric]] = None) -> "BaselineModel":
        """Fit from repository samples in ``[start, end)`` (a healthy period)."""
        if metrics is None:
            samples = repository.query_samples(start=start, end=end)
        else:
            samples = [s for m in metrics for s in repository.query_samples(metric=m, start=start, end=end)]
        return self.fit_samples(samples)

    # -- scoring ------------------------------------------------------------

    def get(self, metric: Metric, target: str) -> Optional[Baseline]:
        return self.baselines.get((Metric(metric), target))

    def zscore(self, metric: Metric, target: str, value: float) -> Optional[float]:
        baseline = self.get(metric, target)
        if baseline is None:
            return None
        return (value - baseline.mean) / max(baseline.std, self.min_std)

    def assess(self, samples: Iterable[Sample]) -> List[Deviation]:
        """Samples whose |z| exceeds ``z_threshold`` or that exceed the p99."""
        deviations = []
        for sample in samples:
            baseline = self.get(sample.metric, sample.target)
            if baseline is None:
                continue
            z = self.zscore(sample.metric, sample.target, sample.value)
            above = sample.value > baseline.p99
            if abs(z) > self.z_threshold or above:
                deviations.append(Deviation(sample.metric, sample.target, sample.value, z, above,
                                            baseline.mean, baseline.p99))
        return sorted(deviations, key=lambda d: -abs(d.zscore))

    # -- persistence ----------------------------------------------------------

    def save(self, repository, created_ts: float) -> int:
        for (metric, target), baseline in self.baselines.items():
            repository.save_baseline(metric, target, baseline.to_dict(), created_ts)
        return len(self.baselines)

    @classmethod
    def load(cls, repository, **kwargs) -> "BaselineModel":
        model = cls(**kwargs)
        model.baselines = {key: Baseline.from_dict(data) for key, data in repository.load_baselines().items()}
        return model
