"""Labelled datasets for failure detection and prediction (Submodule 3.7).

Combines feature rows (:class:`FeatureExtractor`) with scenario ground
truth (:class:`FailureRecord`) into labelled rows, one per target per
time step, and exports them as CSV plus a JSON metadata file.

Labels for a row stamped ``t`` (its features use data from before ``t``;
its last bucket is ``[t - step, t)``):

- ``label_failure_active``    1 if a failure affecting this target
                              overlaps the last bucket (detection task)
- ``label_failure_type``      that failure's kind, or ``none``
- ``label_failure_imminent``  1 if no failure is active and one affecting
                              this target starts within ``horizon_s``
                              after ``t`` (prediction task)
- ``label_time_to_failure_s`` seconds until that failure, else empty
- ``label_precursor_active``  1 if a synthetic warning ramp for a failure
                              affecting this target overlaps the bucket
- ``label_cooldown``          1 if any failure ended less than
                              ``cooldown_s`` before ``t``: the row's
                              feature windows still see that failure.
                              Exclude these rows when training or scoring
                              prediction, or the model can learn the
                              scenario schedule instead of real signals.

For prediction, train on rows with ``label_failure_active == 0``; rows
during a failure are trivially recognisable from link/node status.

Datasets built from :class:`ScenarioRunner` output are SIMULATED and,
when precursors are used, SYNTHETIC: metadata records both.
"""

from __future__ import annotations

import csv
import json
from typing import Dict, Iterable, List, Optional, Sequence

from network.analytics.features import DEFAULT_LINK_METRICS, DEFAULT_NODE_METRICS, FeatureExtractor

LABEL_COLUMNS = (
    "label_failure_active",
    "label_failure_type",
    "label_failure_imminent",
    "label_time_to_failure_s",
    "label_precursor_active",
    "label_cooldown",
)
ID_COLUMNS = ("timestamp", "target_kind", "target")


def label_row(row: dict, target_kind: str, records: Sequence, horizon_s: float, step_s: float,
              cooldown_s: float = 0.0) -> dict:
    t = row["timestamp"]
    key = f"{target_kind}:{row['target']}"
    bucket_start = t - step_s
    relevant = [r for r in records if key in r.affected]

    active = next((r for r in relevant if r.start < t and r.end > bucket_start), None)
    upcoming = [r.start - t for r in relevant if r.start >= t and r.start - t < horizon_s]
    precursor = any(
        r.precursor_start is not None and r.precursor_start < t and r.start > bucket_start
        for r in relevant
    )
    labelled = dict(row)
    labelled["target_kind"] = target_kind
    labelled["label_failure_active"] = 1 if active else 0
    labelled["label_failure_type"] = active.kind if active else "none"
    imminent = (not active) and bool(upcoming)
    labelled["label_failure_imminent"] = 1 if imminent else 0
    labelled["label_time_to_failure_s"] = min(upcoming) if imminent else None
    labelled["label_precursor_active"] = 1 if precursor else 0
    labelled["label_cooldown"] = 1 if any(
        r.end <= t and t - r.end < cooldown_s for r in records
    ) else 0
    return labelled


def build_dataset(
    result,
    extractor: Optional[FeatureExtractor] = None,
    horizon_s: float = 60.0,
    metrics: Optional[Dict[str, Sequence]] = None,
    cooldown_s: Optional[float] = None,
) -> Dict[str, List[dict]]:
    """Labelled rows per target kind (``link`` / ``node``) from a ScenarioResult.

    ``cooldown_s`` defaults to the longest feature window.
    """
    extractor = extractor or FeatureExtractor(step_s=result.plan.step_s)
    if cooldown_s is None:
        cooldown_s = extractor.windows_s[-1]
    metrics = metrics or {"link": DEFAULT_LINK_METRICS, "node": DEFAULT_NODE_METRICS}
    end = result.end + result.plan.step_s
    datasets: Dict[str, List[dict]] = {}
    for kind, kind_metrics in metrics.items():
        rows: List[dict] = []
        for target in result.repository.targets(kind):
            for row in extractor.extract(result.repository, target, kind_metrics, result.start, end):
                rows.append(label_row(row, kind, result.records, horizon_s, extractor.step_s, cooldown_s))
        rows.sort(key=lambda r: (r["timestamp"], r["target"]))
        datasets[kind] = rows
    return datasets


def columns_for(rows: Sequence[dict]) -> List[str]:
    if not rows:
        return list(ID_COLUMNS) + list(LABEL_COLUMNS)
    features = [c for c in rows[0] if c not in ID_COLUMNS and c not in LABEL_COLUMNS]
    return list(ID_COLUMNS) + features + list(LABEL_COLUMNS)


def write_csv(rows: Sequence[dict], path: str) -> int:
    columns = columns_for(rows)
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in columns})
    return len(rows)


def label_counts(rows: Iterable[dict]) -> dict:
    counts = {"rows": 0, "failure_active": 0, "failure_imminent": 0, "precursor_active": 0,
              "cooldown": 0, "by_type": {}}
    for row in rows:
        counts["rows"] += 1
        counts["failure_active"] += row["label_failure_active"]
        counts["failure_imminent"] += row["label_failure_imminent"]
        counts["precursor_active"] += row["label_precursor_active"]
        counts["cooldown"] += row.get("label_cooldown", 0)
        kind = row["label_failure_type"]
        counts["by_type"][kind] = counts["by_type"].get(kind, 0) + 1
    return counts


def dataset_metadata(result, datasets: Dict[str, List[dict]], extractor: FeatureExtractor,
                     horizon_s: float) -> dict:
    has_precursors = any(r.precursor_kind for r in result.records)
    return {
        "simulated": True,
        "synthetic_precursors": has_precursors,
        "warning": (
            "Generated by SimulatedSource on an in-memory network. Not measured on a real data plane."
            + (" Contains designed warning patterns (precursors); model results on this data show"
               " the patterns can be learned, not that real networks exhibit them." if has_precursors else "")
        ),
        "plan": result.plan.to_dict(),
        "scenario": result.summary(),
        "failures": [r.to_dict() for r in result.records],
        "features": {
            "step_s": extractor.step_s,
            "windows_s": list(extractor.windows_s),
            "max_gap_buckets": extractor.max_gap_buckets,
        },
        "horizon_s": horizon_s,
        "label_columns": list(LABEL_COLUMNS),
        "datasets": {
            kind: {"columns": columns_for(rows), "labels": label_counts(rows)}
            for kind, rows in datasets.items()
        },
    }


def write_metadata(metadata: dict, path: str) -> None:
    with open(path, "w") as fh:
        json.dump(metadata, fh, indent=2, sort_keys=True, default=str)
