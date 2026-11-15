"""Statistics for the experiments (Module 9), following research doc §9.3:

- ``mean_ci``: mean with a 95 % confidence interval (Student t)
- ``paired_test``: paired Wilcoxon signed-rank test with the matched-pairs
  rank-biserial correlation as effect size and the median difference
- ``holm``: Holm-Bonferroni adjustment when several comparisons are made

Uses scipy when available (t quantiles, exact Wilcoxon p-values); without
it, a normal approximation is used and reported.
"""

from __future__ import annotations

import math
import statistics
from typing import Dict, List, Optional, Sequence

try:  # pragma: no cover - availability depends on the environment
    from scipy import stats as _sp
except Exception:  # noqa: BLE001
    _sp = None


def _clean(values: Sequence[Optional[float]]) -> List[float]:
    return [float(v) for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]


def mean_ci(values: Sequence[Optional[float]], confidence: float = 0.95,
            lower: Optional[float] = None, upper: Optional[float] = None) -> dict:
    """Mean and t-based confidence interval, clipped to ``[lower, upper]``
    when given (e.g. a percentage cannot be negative)."""
    data = _clean(values)
    n = len(data)
    if n == 0:
        return {"n": 0, "mean": None, "ci_low": None, "ci_high": None, "sd": None}
    mean = statistics.mean(data)
    if n == 1:
        return {"n": 1, "mean": mean, "ci_low": None, "ci_high": None, "sd": None}
    sd = statistics.stdev(data)
    if _sp is not None:
        q = float(_sp.t.ppf(0.5 + confidence / 2, n - 1))
    else:
        q = 1.959963984540054
    half = q * sd / math.sqrt(n)
    lo, hi = mean - half, mean + half
    if lower is not None:
        lo = max(lo, lower)
    if upper is not None:
        hi = min(hi, upper)
    return {"n": n, "mean": mean, "ci_low": lo, "ci_high": hi, "sd": sd}


def bounds_for(metric: str):
    """Natural bounds of a metric, from its name."""
    if metric.endswith("_pct"):
        return 0.0, 100.0
    if any(k in metric for k in ("rate", "recall", "precision", "auroc", "far", "frr", "o1_met", "cost", "f1",
                                 "brier")):
        return 0.0, 1.0
    return 0.0, None


def paired_test(a: Sequence[Optional[float]], b: Sequence[Optional[float]]) -> dict:
    """Wilcoxon signed-rank test of paired samples a vs b (pairs with a
    missing value are dropped). Effect size: rank-biserial r in [-1, 1],
    positive when a tends to be larger than b."""
    pairs = [(float(x), float(y)) for x, y in zip(a, b) if x is not None and y is not None]
    n = len(pairs)
    diffs = [x - y for x, y in pairs]
    out = {"n": n, "median_diff": statistics.median(diffs) if diffs else None, "p": None,
           "effect_r": None, "method": None}
    nonzero = [d for d in diffs if d != 0]
    if not nonzero:
        out.update(p=1.0, effect_r=0.0, method="all differences zero")
        return out
    ranks = _ranks([abs(d) for d in nonzero])
    w_plus = sum(r for r, d in zip(ranks, nonzero) if d > 0)
    w_minus = sum(r for r, d in zip(ranks, nonzero) if d < 0)
    out["effect_r"] = (w_plus - w_minus) / (w_plus + w_minus)
    if _sp is not None:
        res = _sp.wilcoxon([x for x, _ in pairs], [y for _, y in pairs], zero_method="wilcox")
        out.update(p=float(res.pvalue), method="scipy wilcoxon")
    else:
        m = len(nonzero)
        mu = m * (m + 1) / 4
        sigma = math.sqrt(m * (m + 1) * (2 * m + 1) / 24)
        z = (min(w_plus, w_minus) - mu) / sigma
        out.update(p=min(1.0, 2 * 0.5 * math.erfc(-z / math.sqrt(2))), method="normal approximation")
    return out


def _ranks(values: List[float]) -> List[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def holm(pvalues: Dict[str, Optional[float]], alpha: float = 0.05) -> Dict[str, dict]:
    """Holm-Bonferroni adjusted p-values and significance decisions."""
    items = sorted(((k, p) for k, p in pvalues.items() if p is not None), key=lambda kv: kv[1])
    m = len(items)
    out: Dict[str, dict] = {k: {"p": None, "p_adj": None, "significant": None} for k in pvalues}
    running = 0.0
    for i, (key, p) in enumerate(items):
        adj = min(1.0, max(running, (m - i) * p))
        running = adj
        out[key] = {"p": p, "p_adj": adj, "significant": adj < alpha}
    return out


def fmt_ci(summary: dict, digits: int = 1, scale: float = 1.0, unit: str = "") -> str:
    """'12.3 [10.1, 14.5] unit' (or '—')."""
    if summary.get("mean") is None:
        return "—"
    m = summary["mean"] * scale
    if summary.get("ci_low") is None:
        return f"{m:.{digits}f}{unit}"
    return f"{m:.{digits}f} [{summary['ci_low'] * scale:.{digits}f}, {summary['ci_high'] * scale:.{digits}f}]{unit}"
