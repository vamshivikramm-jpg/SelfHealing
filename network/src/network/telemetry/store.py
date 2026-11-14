"""In-memory telemetry store (Submodule 2.2).

Keeps a bounded ring buffer per time series, where a series is one
(metric, target) pair. Thread-safe: the collector writes from its
background thread while API handlers read.

This is deliberately short-term, in-process storage for live monitoring.
Long-term history, persistence and analytics belong to Module 3.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Deque, Dict, Iterable, List, Optional, Tuple

from network.telemetry.metrics import Metric, Sample

DEFAULT_MAX_POINTS = 720  # e.g. one hour at a 5 s interval

SeriesKey = Tuple[Metric, str]


class TelemetryStore:
    """Bounded per-series ring buffers of :class:`Sample` objects."""

    def __init__(self, max_points_per_series: int = DEFAULT_MAX_POINTS):
        if max_points_per_series < 1:
            raise ValueError("max_points_per_series must be >= 1")
        self._max_points = max_points_per_series
        self._series: Dict[SeriesKey, Deque[Sample]] = {}
        self._lock = threading.RLock()
        self._total_added = 0

    @property
    def max_points_per_series(self) -> int:
        return self._max_points

    def add(self, samples: Iterable[Sample]) -> int:
        """Append samples to their series. Returns how many were added."""
        added = 0
        with self._lock:
            for sample in samples:
                series = self._series.get(sample.key)
                if series is None:
                    series = deque(maxlen=self._max_points)
                    self._series[sample.key] = series
                series.append(sample)
                added += 1
            self._total_added += added
        return added

    def latest(
        self, metric: Optional[Metric] = None, target: Optional[str] = None
    ) -> List[Sample]:
        """Most recent sample of every series, optionally filtered."""
        metric = Metric(metric) if metric is not None else None
        with self._lock:
            result = [
                series[-1]
                for (m, t), series in self._series.items()
                if series
                and (metric is None or m == metric)
                and (target is None or t == target)
            ]
        return sorted(result, key=lambda s: (s.metric.value, s.target))

    def latest_value(self, metric: Metric, target: str) -> Optional[float]:
        """Most recent value of one series, or None if it has no data."""
        with self._lock:
            series = self._series.get((Metric(metric), target))
            return series[-1].value if series else None

    def series(
        self,
        metric: Metric,
        target: str,
        since: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> List[Sample]:
        """Samples of one series in time order.

        ``since`` keeps samples with ``timestamp >= since``; ``limit``
        keeps only the most recent ``limit`` samples.
        """
        with self._lock:
            data = list(self._series.get((Metric(metric), target), ()))
        if since is not None:
            data = [s for s in data if s.timestamp >= since]
        if limit is not None:
            data = data[-limit:] if limit > 0 else []
        return data

    def series_keys(self) -> List[SeriesKey]:
        with self._lock:
            return sorted(self._series, key=lambda k: (k[0].value, k[1]))

    def targets(self) -> List[str]:
        with self._lock:
            return sorted({target for _, target in self._series})

    def clear(self) -> None:
        with self._lock:
            self._series.clear()
            self._total_added = 0

    def stats(self) -> dict:
        with self._lock:
            return {
                "series": len(self._series),
                "points": sum(len(s) for s in self._series.values()),
                "total_added": self._total_added,
                "max_points_per_series": self._max_points,
            }
