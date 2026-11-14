"""Time-series operations (Submodule 3.3).

Raw telemetry arrives at irregular times and from many series. Analysis
and machine learning need evenly spaced, aligned data. This module:

- ``resample``: groups ``(timestamp, value)`` points into fixed-width
  buckets ``[start + k*step, start + (k+1)*step)`` and aggregates each
  bucket (mean, min, max, last, sum, count). Empty buckets are ``None``.
- ``align``: resamples several series onto the same bucket grid.
- ``load_aligned``: does the same straight from a repository.

Pure standard library.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Point = Tuple[float, float]


class Aggregation(str, Enum):
    MEAN = "mean"
    MIN = "min"
    MAX = "max"
    LAST = "last"
    SUM = "sum"
    COUNT = "count"


def bucket_starts(start: float, end: float, step: float) -> List[float]:
    """Start times of the buckets covering ``[start, end)``."""
    if step <= 0:
        raise ValueError("step must be > 0")
    if end <= start:
        return []
    count = int(math.ceil((end - start) / step - 1e-9))
    return [start + i * step for i in range(count)]


def _aggregate(values: List[float], how: Aggregation) -> Optional[float]:
    if how is Aggregation.COUNT:
        return float(len(values))
    if not values:
        return None
    if how is Aggregation.MEAN:
        return sum(values) / len(values)
    if how is Aggregation.MIN:
        return min(values)
    if how is Aggregation.MAX:
        return max(values)
    if how is Aggregation.LAST:
        return values[-1]
    if how is Aggregation.SUM:
        return float(sum(values))
    raise ValueError(f"unknown aggregation: {how}")


def resample(
    points: Iterable[Point],
    start: float,
    end: float,
    step: float,
    how: Aggregation = Aggregation.MEAN,
) -> List[Tuple[float, Optional[float]]]:
    """Aggregate points into fixed buckets over ``[start, end)``.

    Points outside the range are ignored. Points are assumed to be in time
    order within a bucket for ``LAST``.
    """
    how = Aggregation(how)
    starts = bucket_starts(start, end, step)
    buckets: List[List[float]] = [[] for _ in starts]
    for ts, value in points:
        if ts < start or ts >= end or value is None:
            continue
        index = int((ts - start) // step)
        if 0 <= index < len(buckets):
            buckets[index].append(float(value))
    return [(t, _aggregate(b, how)) for t, b in zip(starts, buckets)]


def align(
    series: Dict[str, Iterable[Point]],
    start: float,
    end: float,
    step: float,
    how: Aggregation = Aggregation.MEAN,
) -> Tuple[List[float], Dict[str, List[Optional[float]]]]:
    """Resample every named series onto one shared bucket grid."""
    grid = bucket_starts(start, end, step)
    values = {
        name: [v for _, v in resample(points, start, end, step, how)]
        for name, points in series.items()
    }
    return grid, values


def load_aligned(
    repository,
    target: str,
    metrics: Sequence,
    start: float,
    end: float,
    step: float,
    how: Aggregation = Aggregation.MEAN,
) -> Tuple[List[float], Dict[str, List[Optional[float]]]]:
    """Load several metrics of one target from a repository, aligned."""
    series = {
        getattr(m, "value", m): repository.query_points(m, target, start=start, end=end)
        for m in metrics
    }
    return align(series, start, end, step, how)
