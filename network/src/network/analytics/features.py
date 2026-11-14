"""Feature extraction (Submodule 3.5).

Turns stored telemetry into one feature row per target per time step,
ready for Module 4's models. For every metric and every rolling window it
computes: mean, std, min, max, last, delta (last - first) and slope
(least-squares trend per second).

No look-ahead: the row stamped ``t`` only uses data from buckets that
ended at or before ``t``. Rows are produced only once the longest window
is fully covered, so every row has the same history length.

Column names: ``<metric>__<window>s__<stat>``, e.g.
``link.latency_ms__30s__mean``. Missing data stays ``None``.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

from network.analytics.preprocess import fill_gaps
from network.analytics.timeseries import Aggregation, load_aligned
from network.telemetry.metrics import Metric

STATS = ("mean", "std", "min", "max", "last", "delta", "slope")

DEFAULT_LINK_METRICS = (
    Metric.LINK_STATUS,
    Metric.LINK_LATENCY_MS,
    Metric.LINK_PACKET_LOSS_PCT,
    Metric.LINK_UTILIZATION_PCT,
    Metric.LINK_THROUGHPUT_MBPS,
    Metric.LINK_DROP_RATE_PPS,
)
DEFAULT_NODE_METRICS = (Metric.NODE_STATUS, Metric.NODE_CPU_PCT, Metric.NODE_MEMORY_PCT)


def window_stats(values: Sequence[Optional[float]], step_s: float) -> Dict[str, Optional[float]]:
    """Statistics of one window; positions are ``step_s`` seconds apart."""
    points = [(i * step_s, v) for i, v in enumerate(values) if v is not None]
    if not points:
        return {name: None for name in STATS}
    data = [v for _, v in points]
    n = len(data)
    mean = sum(data) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in data) / n)
    slope = None
    if n >= 2:
        t_mean = sum(t for t, _ in points) / n
        denom = sum((t - t_mean) ** 2 for t, _ in points)
        if denom > 0:
            slope = sum((t - t_mean) * (v - mean) for t, v in points) / denom
    return {
        "mean": mean,
        "std": std,
        "min": min(data),
        "max": max(data),
        "last": data[-1],
        "delta": data[-1] - data[0],
        "slope": slope,
    }


def feature_name(metric, window_s: float, stat: str) -> str:
    return f"{getattr(metric, 'value', metric)}__{int(window_s)}s__{stat}"


class FeatureExtractor:
    """Rolling-window feature rows from a :class:`TelemetryRepository`."""

    def __init__(self, step_s: float = 5.0, windows_s: Sequence[float] = (30.0, 120.0),
                 max_gap_buckets: int = 2):
        if step_s <= 0:
            raise ValueError("step_s must be > 0")
        if not windows_s or any(w < step_s for w in windows_s):
            raise ValueError("every window must be >= step_s")
        self.step_s = float(step_s)
        self.windows_s = tuple(float(w) for w in sorted(windows_s))
        self.max_gap_buckets = max_gap_buckets

    def window_buckets(self, window_s: float) -> int:
        return int(round(window_s / self.step_s))

    def feature_names(self, metrics: Sequence) -> List[str]:
        return [feature_name(m, w, stat) for m in metrics for w in self.windows_s for stat in STATS]

    def extract(self, repository, target: str, metrics: Sequence, start: float,
                end: float) -> List[Dict[str, Optional[float]]]:
        """Feature rows for one target over ``[start, end)``.

        Each row has ``timestamp`` (end of its last bucket), ``target``
        and every feature column.
        """
        grid, series = load_aligned(repository, target, metrics, start, end, self.step_s,
                                    Aggregation.MEAN)
        filled = {name: fill_gaps(values, self.max_gap_buckets)[0] for name, values in series.items()}
        longest = self.window_buckets(self.windows_s[-1])
        rows: List[Dict[str, Optional[float]]] = []
        for k in range(longest - 1, len(grid)):
            row: Dict[str, Optional[float]] = {"timestamp": grid[k] + self.step_s, "target": target}
            for metric in metrics:
                values = filled[getattr(metric, "value", metric)]
                for window in self.windows_s:
                    n = self.window_buckets(window)
                    stats = window_stats(values[k - n + 1:k + 1], self.step_s)
                    for stat in STATS:
                        row[feature_name(metric, window, stat)] = stats[stat]
            rows.append(row)
        return rows
