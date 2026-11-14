"""Preprocessing (Submodule 3.4).

Cleans aligned series before feature extraction and model training:

- ``fill_gaps``: forward-fill short gaps (``None`` runs up to
  ``max_gap`` buckets); longer gaps stay missing so a long outage is never
  disguised as data. Returns the filled values and a mask of which
  positions were filled.
- ``clip`` / ``clip_percentiles``: bound extreme values.
- ``StandardScaler`` / ``MinMaxScaler``: fit on training data, apply to
  any data, and serialise their parameters with the dataset so the exact
  same scaling can be reused at prediction time.

Missing values (``None``) pass through every function unchanged.
Pure standard library.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Values = List[Optional[float]]


def fill_gaps(values: Sequence[Optional[float]], max_gap: int) -> Tuple[Values, List[bool]]:
    """Forward-fill runs of ``None`` no longer than ``max_gap``.

    Leading ``None`` values (no previous value) are never filled. A run
    longer than ``max_gap`` is left entirely missing.
    """
    if max_gap < 0:
        raise ValueError("max_gap must be >= 0")
    out: Values = list(values)
    filled = [False] * len(out)
    i = 0
    while i < len(out):
        if out[i] is not None:
            i += 1
            continue
        j = i
        while j < len(out) and out[j] is None:
            j += 1
        run = j - i
        # a short run at the very end is also forward-filled
        if i > 0 and run <= max_gap and out[i - 1] is not None:
            for k in range(i, j):
                out[k] = out[i - 1]
                filled[k] = True
        i = j
    return out, filled


def missing_ratio(values: Sequence[Optional[float]]) -> float:
    if not values:
        return 0.0
    return sum(1 for v in values if v is None) / len(values)


def clip(values: Sequence[Optional[float]], low: Optional[float] = None,
         high: Optional[float] = None) -> Values:
    def one(v):
        if v is None:
            return None
        if low is not None and v < low:
            return low
        if high is not None and v > high:
            return high
        return v
    return [one(v) for v in values]


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile of already sorted data (``q`` in 0..100)."""
    if not sorted_values:
        raise ValueError("percentile of empty data")
    if not 0 <= q <= 100:
        raise ValueError("q must be within [0, 100]")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (len(sorted_values) - 1) * q / 100.0
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    frac = pos - lo
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac)


def clip_percentiles(values: Sequence[Optional[float]], low_q: float = 1.0,
                     high_q: float = 99.0) -> Values:
    present = sorted(v for v in values if v is not None)
    if not present:
        return list(values)
    return clip(values, percentile(present, low_q), percentile(present, high_q))


def _present(values: Iterable[Optional[float]]) -> List[float]:
    return [float(v) for v in values if v is not None]


class StandardScaler:
    """z = (x - mean) / std, fitted per column."""

    def __init__(self, min_std: float = 1e-9):
        self.min_std = min_std
        self.params: Dict[str, Dict[str, float]] = {}

    def fit(self, columns: Dict[str, Sequence[Optional[float]]]) -> "StandardScaler":
        for name, values in columns.items():
            data = _present(values)
            if not data:
                self.params[name] = {"mean": 0.0, "std": 1.0}
                continue
            mean = sum(data) / len(data)
            var = sum((x - mean) ** 2 for x in data) / len(data)
            self.params[name] = {"mean": mean, "std": max(math.sqrt(var), self.min_std)}
        return self

    def transform(self, columns: Dict[str, Sequence[Optional[float]]]) -> Dict[str, Values]:
        out = {}
        for name, values in columns.items():
            p = self.params.get(name)
            if p is None:
                raise KeyError(f"scaler was not fitted on column {name!r}")
            out[name] = [None if v is None else (v - p["mean"]) / p["std"] for v in values]
        return out

    def inverse(self, columns: Dict[str, Sequence[Optional[float]]]) -> Dict[str, Values]:
        return {
            name: [None if v is None else v * self.params[name]["std"] + self.params[name]["mean"]
                   for v in values]
            for name, values in columns.items()
        }

    def to_dict(self) -> dict:
        return {"type": "standard", "params": {k: dict(v) for k, v in self.params.items()}}

    @classmethod
    def from_dict(cls, data: dict) -> "StandardScaler":
        scaler = cls()
        scaler.params = {k: dict(v) for k, v in data["params"].items()}
        return scaler


class MinMaxScaler:
    """x' = (x - min) / (max - min), fitted per column; constant columns map to 0."""

    def __init__(self):
        self.params: Dict[str, Dict[str, float]] = {}

    def fit(self, columns: Dict[str, Sequence[Optional[float]]]) -> "MinMaxScaler":
        for name, values in columns.items():
            data = _present(values)
            self.params[name] = {"min": min(data) if data else 0.0, "max": max(data) if data else 0.0}
        return self

    def transform(self, columns: Dict[str, Sequence[Optional[float]]]) -> Dict[str, Values]:
        out = {}
        for name, values in columns.items():
            p = self.params.get(name)
            if p is None:
                raise KeyError(f"scaler was not fitted on column {name!r}")
            span = p["max"] - p["min"]
            out[name] = [None if v is None else (0.0 if span == 0 else (v - p["min"]) / span)
                         for v in values]
        return out

    def to_dict(self) -> dict:
        return {"type": "minmax", "params": {k: dict(v) for k, v in self.params.items()}}
