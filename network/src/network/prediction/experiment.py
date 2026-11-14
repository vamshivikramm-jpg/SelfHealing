"""Model comparison experiment and report (Submodule 4.5).

    PYTHONPATH=network/src python3 -m network.prediction.experiment --out data/module4

1. Builds the train / validation / test suite of simulated runs.
2. Fits every model on train.
3. Chooses each model's alarm threshold on validation (event recall,
   subject to the false-alarm budget).
4. Scores test once with that threshold: row and event metrics.
5. Checks the Module 4 objectives, picks the champion *on validation*,
   registers every model (challengers) and promotes the champion.
6. Writes ``report.json`` and ``report.md``.

Objectives (agreed before modelling):
- O1: >= 80 % of failures that have warning signs caught >= 30 s ahead,
      with <= 1 false alarm per hour across the network (test data).
- O2: an ML model is only worth using if it beats the z-score baseline.
- O3: confidence scores are reported with calibration (Brier, reliability).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, Optional, Sequence

import numpy as np

from network.config import load_topology_config
from network.prediction.data import PredictionData, SuiteConfig, build_suite
from network.prediction.evaluation import EventScorer, choose_threshold, reliability, row_metrics
from network.prediction.models import MODEL_NAMES, feature_importance, make_model
from network.prediction.registry import ModelRegistry

O1_RECALL = 0.80
O1_MIN_LEAD_S = 30.0
O1_MAX_FALSE_ALARMS_PER_HOUR = 1.0
DEFAULT_CONFIG = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "config",
                                               "enterprise.yaml"))


def evaluate_model(name: str, data: Dict[str, PredictionData], max_fa: float, min_lead_s: float,
                   val_scorer: EventScorer, test_scorer: EventScorer, random_state: int = 0) -> dict:
    model = make_model(name, random_state=random_state)
    started = time.perf_counter()
    model.fit(data["train"].X, data["train"].y, data["train"].feature_names)
    fit_s = time.perf_counter() - started

    val_scores = model.predict_proba(data["val"].X)
    choice = choose_threshold(val_scorer, val_scores, max_fa)
    threshold = choice["threshold"]

    started = time.perf_counter()
    test_scores = model.predict_proba(data["test"].X)
    predict_ms_per_row = (time.perf_counter() - started) * 1000.0 / max(1, len(test_scores))
    test_events = test_scorer.evaluate(test_scores, threshold)
    o1 = (
        test_events["recall_with_precursor"] is not None
        and test_events["recall_with_precursor"] >= O1_RECALL
        and (test_events["false_alarms_per_hour"] or 0.0) <= O1_MAX_FALSE_ALARMS_PER_HOUR
    )
    return {
        "model": model,
        "summary": {
            "name": name,
            "kind": model.kind,
            "threshold": threshold,
            "threshold_within_budget": choice["within_budget"],
            "validation": {
                "events": choice["validation"],
                "rows": row_metrics(data["val"].y, val_scores, threshold),
            },
            "test": {
                "events": test_events,
                "rows": row_metrics(data["test"].y, test_scores, threshold),
                "reliability": reliability(data["test"].y, test_scores),
            },
            "o1_met": bool(o1),
            "fit_seconds": fit_s,
            "predict_ms_per_row": predict_ms_per_row,
            "top_features": feature_importance(model),
        },
    }


def pick_champion(results: Sequence[dict]) -> Optional[str]:
    """Best on VALIDATION: within the false-alarm budget, highest event
    recall, then highest validation average precision."""
    eligible = [r for r in results if r["threshold_within_budget"] and r["validation"]["events"]]
    if not eligible:
        return None
    key = lambda r: (r["validation"]["events"]["event_recall"] or 0.0,
                     r["validation"]["rows"]["average_precision"] or 0.0)
    return max(eligible, key=key)["name"]


def run_experiment(topology_path: str = DEFAULT_CONFIG, suite: SuiteConfig = SuiteConfig(),
                   models: Sequence[str] = MODEL_NAMES, out_dir: Optional[str] = None,
                   registry_root: Optional[str] = None, data: Optional[Dict[str, PredictionData]] = None,
                   max_fa: float = O1_MAX_FALSE_ALARMS_PER_HOUR, min_lead_s: float = O1_MIN_LEAD_S) -> dict:
    started = time.perf_counter()
    if data is None:
        data = build_suite(load_topology_config(topology_path), suite)
    build_s = time.perf_counter() - started
    val_scorer = EventScorer(data["val"], min_lead_s)
    test_scorer = EventScorer(data["test"], min_lead_s)

    fitted = {}
    results = []
    for name in models:
        out = evaluate_model(name, data, max_fa, min_lead_s, val_scorer, test_scorer)
        fitted[name] = out["model"]
        results.append(out["summary"])

    by_name = {r["name"]: r for r in results}
    champion = pick_champion(results)
    zscore = by_name.get("zscore")
    o2 = {}
    if zscore:
        z_rec = zscore["test"]["events"]["recall_with_precursor"] or 0.0
        z_ap = zscore["test"]["rows"]["average_precision"] or 0.0
        for r in results:
            if r["kind"] in ("supervised", "unsupervised"):
                o2[r["name"]] = {
                    "beats_zscore_event_recall": (r["test"]["events"]["recall_with_precursor"] or 0.0) > z_rec,
                    "beats_zscore_average_precision": (r["test"]["rows"]["average_precision"] or 0.0) > z_ap,
                }
    provenance = {
        "suite": suite.to_dict(),
        "train": data["train"].summary(),
        "val": data["val"].summary(),
        "test": data["test"].summary(),
        "simulated": all(d.simulated for d in data.values()),
        "synthetic_precursors": any(d.synthetic_precursors for d in data.values()),
    }
    report = {
        "objectives": {
            "O1": {"recall_with_precursor": O1_RECALL, "min_lead_s": min_lead_s,
                   "max_false_alarms_per_hour": max_fa},
            "O2": "ML must beat the z-score baseline",
            "O3": "confidence scores with calibration",
        },
        "data": provenance,
        "warning": ("All results are on SIMULATED data"
                    + (" with DESIGNED warning patterns (synthetic precursors); they show the patterns can"
                       " be learned, not that real networks exhibit them." if provenance["synthetic_precursors"]
                       else ".")),
        "models": results,
        "champion": champion,
        "o2": o2,
        "seconds": {"build_suite": build_s, "total": time.perf_counter() - started},
    }

    if registry_root:
        registry = ModelRegistry(registry_root)
        versions = {}
        for r in results:
            metrics = {"validation": r["validation"], "test": {"events": r["test"]["events"],
                                                               "rows": r["test"]["rows"]}}
            versions[r["name"]] = registry.register(fitted[r["name"]], r["threshold"], metrics, provenance)[1]
        if champion:
            registry.promote(champion, versions[champion], reason="best validation event recall within budget")
        report["registry"] = {"root": registry_root, "versions": versions}

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "report.json"), "w") as fh:
            json.dump(report, fh, indent=2, sort_keys=True, default=_json_default)
        with open(os.path.join(out_dir, "report.md"), "w") as fh:
            fh.write(render_markdown(report))
    return report


def _json_default(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    return str(value)


def _pct(v):
    return "—" if v is None else f"{v * 100:.0f}%"


def _num(v, fmt="{:.2f}"):
    return "—" if v is None else fmt.format(v)


def render_markdown(report: dict) -> str:
    d = report["data"]
    lines = [
        "# Module 4 — failure prediction model comparison",
        "",
        f"> {report['warning']}",
        "",
        "## Data",
        "",
        "| Split | Runs | Hours | Rows | Positive rows | Failures | With warning signs |",
        "|---|---|---|---|---|---|---|",
    ]
    for split in ("train", "val", "test"):
        s = d[split]
        lines.append(f"| {split} | {', '.join(s['runs'])} | {s['hours']:.1f} | {s['rows']} | "
                     f"{s['positives']} ({s['positive_rate'] * 100:.1f}%) | {s['failures']} | "
                     f"{s['failures_with_precursor']} |")
    o1 = report["objectives"]["O1"]
    lines += [
        "",
        "## Results on test runs",
        "",
        f"O1 target: ≥ {o1['recall_with_precursor'] * 100:.0f}% of failures with warning signs caught "
        f"≥ {o1['min_lead_s']:.0f} s ahead, ≤ {o1['max_false_alarms_per_hour']:g} false alarm/hour. "
        "Thresholds were chosen on validation runs.",
        "",
        "| Model | Kind | Caught (with signs) | Caught (no signs) | All failures | Median lead | "
        "False alarms/h | Avg precision | Row F1 | O1 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in report["models"]:
        e, rows = r["test"]["events"], r["test"]["rows"]
        lines.append(
            f"| {r['name']}{' ★' if r['name'] == report['champion'] else ''} | {r['kind']} | "
            f"{_pct(e['recall_with_precursor'])} | {_pct(e['recall_without_precursor'])} | "
            f"{_pct(e['event_recall'])} | {_num(e['median_lead_s'], '{:.0f} s')} | "
            f"{_num(e['false_alarms_per_hour'])} | {_num(rows['average_precision'])} | "
            f"{_num(rows['f1'])} | {'met' if r['o1_met'] else 'not met'} |"
        )
    lines += ["", f"★ champion (chosen on validation): **{report['champion'] or 'none'}**", ""]
    if report["o2"]:
        lines += ["## O2 — does ML beat the z-score baseline? (test)", "",
                  "| Model | Higher caught rate | Higher avg precision |", "|---|---|---|"]
        for name, v in report["o2"].items():
            lines.append(f"| {name} | {'yes' if v['beats_zscore_event_recall'] else 'no'} | "
                         f"{'yes' if v['beats_zscore_average_precision'] else 'no'} |")
        lines.append("")
    lines += ["## Calibration (test, Brier score — lower is better)", "",
              "| Model | Brier |", "|---|---|"]
    for r in report["models"]:
        lines.append(f"| {r['name']} | {_num(r['test']['rows']['brier'], '{:.4f}')} |")
    lines += ["", "Rule and z-score scores are rankings, not probabilities; their Brier score is "
              "shown only for completeness.", ""]
    return "\n".join(lines)


def build_parser():
    p = argparse.ArgumentParser(prog="network.prediction.experiment",
                                description="Train and compare failure-prediction models on simulated data.")
    p.add_argument("--out", required=True, help="output directory for report.json / report.md")
    p.add_argument("--registry", help="model registry directory (default: <out>/models)")
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--models", default=",".join(MODEL_NAMES))
    p.add_argument("--quick", action="store_true", help="small suite for a fast smoke run")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    suite = SuiteConfig()
    if args.quick:
        suite = SuiteConfig(train_seeds=(1, 2), val_seeds=(3,), test_seeds=(4,), duration_s=5400,
                            n_failures=8)
    report = run_experiment(args.config, suite, args.models.split(","), out_dir=args.out,
                            registry_root=args.registry or os.path.join(args.out, "models"))
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
