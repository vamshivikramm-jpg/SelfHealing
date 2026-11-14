"""Prediction datasets (Submodule 4.0).

Turns Module 3 scenario runs into numpy arrays for failure *prediction*
on links, and builds a suite of independent runs for training,
validation (threshold and model choice) and testing.

Rows kept for prediction:
- ``label_failure_active == 0``: during a failure the link status gives
  it away, so those rows say nothing about prediction;
- ``label_cooldown == 0``: shortly after a failure the feature windows
  still contain it, which would let a model learn the scenario schedule.

Train, validation and test always come from different runs (different
seeds). Rows of one run are strongly correlated, so a random row split
would overstate performance.

Requires numpy.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from network.analytics.dataset import ID_COLUMNS, LABEL_COLUMNS, build_dataset
from network.analytics.features import FeatureExtractor
from network.analytics.scenarios import ScenarioRunner, generate_plan
from network.config import TopologyConfig


@dataclass
class PredictionData:
    """Feature matrix, labels and the context needed for event-level scoring."""

    X: np.ndarray
    y: np.ndarray
    feature_names: List[str]
    timestamps: np.ndarray
    targets: np.ndarray
    run_ids: np.ndarray
    precursor: np.ndarray
    time_to_failure: np.ndarray
    records: Dict[str, List[dict]] = field(default_factory=dict)
    spans: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    step_s: float = 5.0
    horizon_s: float = 60.0
    simulated: bool = True
    synthetic_precursors: bool = False

    def __len__(self) -> int:
        return len(self.y)

    @property
    def positives(self) -> int:
        return int(self.y.sum())

    @property
    def hours(self) -> float:
        return sum(end - start for start, end in self.spans.values()) / 3600.0

    def summary(self) -> dict:
        return {
            "rows": len(self),
            "positives": self.positives,
            "positive_rate": float(self.y.mean()) if len(self) else 0.0,
            "runs": sorted(self.spans),
            "hours": self.hours,
            "failures": sum(len(r) for r in self.records.values()),
            "failures_with_precursor": sum(
                1 for rs in self.records.values() for r in rs if r.get("precursor_kind")
            ),
            "features": len(self.feature_names),
            "simulated": self.simulated,
            "synthetic_precursors": self.synthetic_precursors,
        }


def _num(value) -> float:
    if value is None or value == "":
        return np.nan
    return float(value)


def from_rows(rows: Sequence[dict], run_id: str, records: Sequence[dict], span: Tuple[float, float],
              step_s: float, horizon_s: float, simulated: bool = True,
              synthetic_precursors: bool = False) -> PredictionData:
    """Build prediction arrays from labelled link rows of one run."""
    keep = [r for r in rows
            if int(r["label_failure_active"]) == 0 and int(r.get("label_cooldown", 0) or 0) == 0]
    if not rows:
        raise ValueError("no rows")
    features = [c for c in rows[0] if c not in ID_COLUMNS and c not in LABEL_COLUMNS]
    X = np.array([[_num(r[c]) for c in features] for r in keep], dtype=float).reshape(len(keep), len(features))
    return PredictionData(
        X=X,
        y=np.array([int(r["label_failure_imminent"]) for r in keep], dtype=int),
        feature_names=features,
        timestamps=np.array([float(r["timestamp"]) for r in keep]),
        targets=np.array([r["target"] for r in keep], dtype=object),
        run_ids=np.array([run_id] * len(keep), dtype=object),
        precursor=np.array([int(r["label_precursor_active"]) for r in keep], dtype=int),
        time_to_failure=np.array([_num(r["label_time_to_failure_s"]) for r in keep]),
        records={run_id: [dict(r) for r in records]},
        spans={run_id: span},
        step_s=step_s,
        horizon_s=horizon_s,
        simulated=simulated,
        synthetic_precursors=synthetic_precursors,
    )


def concat(parts: Sequence[PredictionData]) -> PredictionData:
    if not parts:
        raise ValueError("nothing to concatenate")
    names = parts[0].feature_names
    for p in parts[1:]:
        if p.feature_names != names:
            raise ValueError("feature columns differ between parts")
    records: Dict[str, List[dict]] = {}
    spans: Dict[str, Tuple[float, float]] = {}
    for p in parts:
        records.update(p.records)
        spans.update(p.spans)
    return PredictionData(
        X=np.vstack([p.X for p in parts]),
        y=np.concatenate([p.y for p in parts]),
        feature_names=list(names),
        timestamps=np.concatenate([p.timestamps for p in parts]),
        targets=np.concatenate([p.targets for p in parts]),
        run_ids=np.concatenate([p.run_ids for p in parts]),
        precursor=np.concatenate([p.precursor for p in parts]),
        time_to_failure=np.concatenate([p.time_to_failure for p in parts]),
        records=records,
        spans=spans,
        step_s=parts[0].step_s,
        horizon_s=parts[0].horizon_s,
        simulated=all(p.simulated for p in parts),
        synthetic_precursors=any(p.synthetic_precursors for p in parts),
    )


def load_dataset_dir(path: str, run_id: Optional[str] = None) -> PredictionData:
    """Load ``links.csv`` + ``metadata.json`` written by ``network.analytics.generate``."""
    with open(os.path.join(path, "metadata.json")) as fh:
        meta = json.load(fh)
    with open(os.path.join(path, "links.csv")) as fh:
        rows = list(csv.DictReader(fh))
    scenario = meta["scenario"]
    return from_rows(
        rows, run_id or os.path.basename(os.path.normpath(path)), meta["failures"],
        (scenario["start"], scenario["end"]), meta["features"]["step_s"], meta["horizon_s"],
        simulated=meta.get("simulated", True),
        synthetic_precursors=meta.get("synthetic_precursors", False),
    )


@dataclass(frozen=True)
class SuiteConfig:
    """Which simulated runs make up train / validation / test."""

    train_seeds: Tuple[int, ...] = (1, 2, 3, 4)
    val_seeds: Tuple[int, ...] = (5, 6)
    test_seeds: Tuple[int, ...] = (7, 8, 9)
    duration_s: float = 14400.0
    n_failures: int = 20
    precursor_probability: float = 0.5
    step_s: float = 5.0
    windows_s: Tuple[float, ...] = (30.0, 120.0)
    horizon_s: float = 60.0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def build_run(config: TopologyConfig, seed: int, suite: SuiteConfig) -> PredictionData:
    plan = generate_plan(config, suite.duration_s, seed=seed, n_failures=suite.n_failures,
                         step_s=suite.step_s, precursor_probability=suite.precursor_probability)
    result = ScenarioRunner(plan, config=config).run()
    extractor = FeatureExtractor(step_s=suite.step_s, windows_s=suite.windows_s)
    rows = build_dataset(result, extractor, horizon_s=suite.horizon_s)["link"]
    records = [r.to_dict() for r in result.records]
    data = from_rows(rows, f"seed{seed}", records, (result.start, result.end), suite.step_s,
                     suite.horizon_s, simulated=True,
                     synthetic_precursors=any(r["precursor_kind"] for r in records))
    result.repository.close()
    return data


def build_suite(config: TopologyConfig, suite: SuiteConfig = SuiteConfig()) -> Dict[str, PredictionData]:
    """Generate every run of the suite (a few seconds per simulated hour)."""
    return {
        split: concat([build_run(config, seed, suite) for seed in seeds])
        for split, seeds in (("train", suite.train_seeds), ("val", suite.val_seeds),
                             ("test", suite.test_seeds))
    }
