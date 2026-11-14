"""Evaluation (Submodule 4.4).

Two views of the same predictions:

Row level (one row = one link at one time step)
    precision, recall, F1 at the alarm threshold; average precision
    (area under the precision-recall curve, suited to rare positives);
    ROC AUC; Brier score and a reliability table for calibration.

Event level (one event = one failure) — what objective O1 is about
    A failure counts as *warned* if any link it affects raises an alarm
    in its warning window: from the start of its synthetic precursor (or
    ``horizon`` seconds before the failure, whichever is earlier) up to
    the failure start. Lead time = failure start - first alarm. A failure
    is *caught* if warned at least ``min_lead_s`` ahead. Alarms are merged
    into episodes (consecutive alarm rows on one link); an episode with no
    row inside any warning window of that link is a *false alarm*.

The alarm threshold is chosen on validation data only
(:func:`choose_threshold`) and then applied unchanged to test data.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

import numpy as np


def row_metrics(y: np.ndarray, scores: np.ndarray, threshold: float) -> dict:
    from sklearn.metrics import (
        average_precision_score,
        brier_score_loss,
        f1_score,
        precision_score,
        recall_score,
        roc_auc_score,
    )

    y = np.asarray(y, dtype=int)
    pred = (scores >= threshold).astype(int)
    both = 0 < y.sum() < len(y)
    return {
        "rows": int(len(y)),
        "positives": int(y.sum()),
        "alarms": int(pred.sum()),
        "precision": float(precision_score(y, pred, zero_division=0)),
        "recall": float(recall_score(y, pred, zero_division=0)),
        "f1": float(f1_score(y, pred, zero_division=0)),
        "average_precision": float(average_precision_score(y, scores)) if both else None,
        "roc_auc": float(roc_auc_score(y, scores)) if both else None,
        "brier": float(brier_score_loss(y, scores)),
    }


def reliability(y: np.ndarray, scores: np.ndarray, bins: int = 10) -> List[dict]:
    """Mean predicted score vs. observed positive rate per score bin."""
    edges = np.linspace(0.0, 1.0, bins + 1)
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (scores >= lo) & ((scores < hi) if hi < 1.0 else (scores <= hi))
        if mask.any():
            out.append({"bin": [float(lo), float(hi)], "rows": int(mask.sum()),
                        "mean_score": float(scores[mask].mean()),
                        "positive_rate": float(np.asarray(y)[mask].mean())})
    return out


class EventScorer:
    """Precomputes per-run, per-link structure so many thresholds are cheap."""

    def __init__(self, data, min_lead_s: float = 30.0):
        self.data = data
        self.min_lead_s = min_lead_s
        self.order = np.lexsort((data.timestamps, data.targets.astype(str), data.run_ids.astype(str)))
        self.true_window = np.zeros(len(data), dtype=bool)
        self.events: List[dict] = []
        for run, records in data.records.items():
            in_run = data.run_ids == run
            for r in records:
                links = [k.split(":", 1)[1] for k in r["affected"] if k.startswith("link:")]
                start = float(r["start"])
                warn_from = start - data.horizon_s
                if r.get("precursor_start") is not None:
                    warn_from = min(warn_from, float(r["precursor_start"]))
                mask = (in_run & np.isin(data.targets, links)
                        & (data.timestamps >= warn_from) & (data.timestamps <= start))
                self.true_window |= mask
                self.events.append({"run": run, "kind": r["kind"], "start": start,
                                    "precursor": bool(r.get("precursor_kind")), "rows": np.flatnonzero(mask)})

    def evaluate(self, scores: np.ndarray, threshold: float) -> dict:
        alarm = scores >= threshold
        leads: List[float] = []
        per_kind: Dict[str, Dict[str, int]] = {}
        groups = {"with_precursor": [0, 0], "without_precursor": [0, 0]}
        caught = 0
        for ev in self.events:
            rows = ev["rows"][alarm[ev["rows"]]]
            lead = (ev["start"] - float(self.data.timestamps[rows].min())) if len(rows) else None
            hit = lead is not None and lead >= self.min_lead_s
            if lead is not None:
                leads.append(lead)
            caught += hit
            group = groups["with_precursor" if ev["precursor"] else "without_precursor"]
            group[0] += 1
            group[1] += hit
            k = per_kind.setdefault(ev["kind"], {"failures": 0, "caught": 0})
            k["failures"] += 1
            k["caught"] += hit
        false_episodes, episodes = self._false_episodes(alarm)
        n = len(self.events)
        hours = self.data.hours
        return {
            "failures": n,
            "caught": int(caught),
            "event_recall": caught / n if n else None,
            "recall_with_precursor": _ratio(*groups["with_precursor"][::-1]),
            "recall_without_precursor": _ratio(*groups["without_precursor"][::-1]),
            "failures_with_precursor": groups["with_precursor"][0],
            "warned_any_lead": len(leads),
            "median_lead_s": float(np.median(leads)) if leads else None,
            "alarm_episodes": episodes,
            "false_alarms": false_episodes,
            "false_alarms_per_hour": false_episodes / hours if hours else None,
            "per_kind": per_kind,
            "min_lead_s": self.min_lead_s,
        }

    def _false_episodes(self, alarm: np.ndarray):
        o = self.order
        a = alarm[o]
        if not a.any():
            return 0, 0
        runs = self.data.run_ids[o]
        targets = self.data.targets[o]
        ts = self.data.timestamps[o]
        true = self.true_window[o]
        gap = self.data.step_s * 1.5
        idx = np.flatnonzero(a)
        new = np.ones(len(idx), dtype=bool)
        new[1:] = ((runs[idx[1:]] != runs[idx[:-1]]) | (targets[idx[1:]] != targets[idx[:-1]])
                   | (ts[idx[1:]] - ts[idx[:-1]] > gap))
        episode_id = np.cumsum(new) - 1
        n_episodes = int(episode_id[-1] + 1)
        has_true = np.zeros(n_episodes, dtype=bool)
        np.logical_or.at(has_true, episode_id, true[idx])
        return int((~has_true).sum()), n_episodes


def _ratio(hits: int, total: int) -> Optional[float]:
    return hits / total if total else None


def choose_threshold(scorer: EventScorer, scores: np.ndarray, max_false_alarms_per_hour: float = 1.0,
                     candidates: int = 200) -> dict:
    """Pick the alarm threshold on validation data.

    Maximises event recall subject to ``false_alarms_per_hour <= budget``;
    ties go to the higher (more conservative) threshold. If no threshold
    meets the budget, the one with the fewest false alarms is used and
    ``within_budget`` is False.
    """
    unique = np.unique(scores)
    if len(unique) > candidates:
        unique = np.unique(np.quantile(scores, np.linspace(0.5, 1.0, candidates)))
    best = None
    fallback = None
    for thr in unique[::-1]:  # high -> low
        thr = float(thr)
        result = scorer.evaluate(scores, thr)
        fa = result["false_alarms_per_hour"] or 0.0
        if fallback is None or fa < fallback[1]:
            fallback = (thr, fa, result)
        if fa <= max_false_alarms_per_hour:
            recall = result["event_recall"] or 0.0
            if best is None or recall > best[1]:
                best = (thr, recall, result)
    if best is not None:
        return {"threshold": best[0], "within_budget": True, "validation": best[2]}
    if fallback is None:
        return {"threshold": 1.0, "within_budget": False, "validation": None}
    return {"threshold": fallback[0], "within_budget": False, "validation": fallback[2]}


def finite(value):
    """JSON-friendly: NaN/inf -> None."""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value
