"""Reproducible experiment suite (Module 9).

    PYTHONPATH=network/src python3 -m network.experiments.runner --profile full --out results/run1
    PYTHONPATH=network/src python3 -m network.experiments.runner --profile quick --out results/quick

Runs, with fixed seeds, every evaluation from Modules 4-8 plus the
research document's §9 protocol:

| Experiment   | What | Paired comparison |
|--------------|------|-------------------|
| prediction   | Module 4 models on independent train/val/test suites | each model vs z-score baseline |
| security     | Module 5 Zero Trust metrics (§8.3) | — |
| decisions    | Module 6 decision quality (§8.4) | — |
| healing      | Module 7 modes incl. baselines (a)-(d) and ablations (§9.2, §9.4) | each mode vs no_healing and vs full |
| baseline_d   | random vs cost-based rerouting on a larger generated campus | random_reroute vs full |
| sensitivity  | physical-fault share, warning-sign share, collection interval | — |
| scalability  | per-step processing time vs topology size | — |

Each metric is summarised as mean with a 95 % confidence interval over
seeds; comparisons use the paired Wilcoxon signed-rank test with Holm
correction (§9.3). Output: ``results.json``, ``report.md``, one CSV per
experiment and SVG figures, plus provenance (git commit, versions,
config hashes). Everything is SIMULATED.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from network.experiments import provenance, svg
from network.experiments.stats import bounds_for, fmt_ci, holm, mean_ci, paired_test

CONFIG_DIR = provenance.CONFIG_DIR


@dataclass(frozen=True)
class Profile:
    seeds: Tuple[int, ...]
    duration_s: float
    n_failures: int
    n_attacks: int
    prediction_reps: int
    prediction_duration_s: float
    prediction_models: Tuple[str, ...]
    sensitivity_seeds: Tuple[int, ...]
    scalability_sizes: Tuple[Tuple[int, int], ...]
    scalability_steps: int
    campus: Tuple[int, int]
    experiments: Tuple[str, ...] = ("prediction", "security", "decisions", "healing", "baseline_d",
                                    "sensitivity", "scalability")


PROFILES = {
    "full": Profile(seeds=tuple(range(1, 11)), duration_s=10800.0, n_failures=12, n_attacks=6,
                    prediction_reps=10, prediction_duration_s=7200.0,
                    prediction_models=("threshold", "zscore", "isolation_forest", "logistic_regression",
                                       "random_forest", "gradient_boosting"),
                    sensitivity_seeds=(1, 2, 3, 4, 5),
                    scalability_sizes=((2, 4), (4, 8), (8, 8), (16, 8), (32, 16)), scalability_steps=60,
                    campus=(8, 4)),
    "quick": Profile(seeds=(1, 2), duration_s=3600.0, n_failures=4, n_attacks=2,
                     prediction_reps=2, prediction_duration_s=5400.0,
                     prediction_models=("threshold", "zscore", "logistic_regression"),
                     sensitivity_seeds=(1,), scalability_sizes=((2, 4), (4, 4)), scalability_steps=20,
                     campus=(4, 3)),
}


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def summarize(rows: List[dict], group: str, metrics: Sequence[str]) -> Dict[str, Dict[str, dict]]:
    groups: Dict[str, List[dict]] = {}
    for r in rows:
        groups.setdefault(r[group], []).append(r)
    return {g: {m: mean_ci([r.get(m) for r in rs], 0.95, *bounds_for(m)) for m in metrics}
            for g, rs in groups.items()}


def compare(rows: List[dict], group: str, metrics: Sequence[str], pairs: Sequence[Tuple[str, str]],
            pair_key: str = "seed") -> Dict[str, dict]:
    """Paired Wilcoxon per (a vs b, metric), Holm-corrected per metric."""
    by = {(r[group], r[pair_key]): r for r in rows}
    keys = sorted({r[pair_key] for r in rows})
    out: Dict[str, dict] = {}
    for metric in metrics:
        tests = {}
        for a, b in pairs:
            va = [by.get((a, k), {}).get(metric) for k in keys]
            vb = [by.get((b, k), {}).get(metric) for k in keys]
            tests[f"{a} vs {b}"] = paired_test(va, vb)
        adjusted = holm({k: t["p"] for k, t in tests.items()})
        for k, t in tests.items():
            t.update(p_adj=adjusted[k]["p_adj"], significant=adjusted[k]["significant"])
        out[metric] = tests
    return out


# ----------------------------------------------------------------------
# Experiments
# ----------------------------------------------------------------------


def exp_prediction(p: Profile, precursor_probability: float = 0.5, models: Optional[Sequence[str]] = None,
                   reps: Optional[int] = None) -> dict:
    from network.config import load_topology_config
    from network.prediction.data import SuiteConfig, build_suite
    from network.prediction.experiment import run_experiment

    topology_path = os.path.join(CONFIG_DIR, "enterprise.yaml")
    topology = load_topology_config(topology_path)
    rows = []
    for rep in range(reps or p.prediction_reps):
        base = 1000 * (rep + 1)
        suite = SuiteConfig(train_seeds=(base + 1, base + 2, base + 3, base + 4), val_seeds=(base + 5, base + 6),
                            test_seeds=(base + 7, base + 8, base + 9), duration_s=p.prediction_duration_s,
                            n_failures=max(4, int((p.prediction_duration_s - 300) / 800)),
                            precursor_probability=precursor_probability)
        data = build_suite(topology, suite)
        report = run_experiment(topology_path, suite, models or p.prediction_models, data=data)
        for m in report["models"]:
            e, r = m["test"]["events"], m["test"]["rows"]
            rows.append({"model": m["name"], "seed": rep, "recall_with_precursor": e["recall_with_precursor"],
                         "recall_without_precursor": e["recall_without_precursor"],
                         "false_alarms_per_hour": e["false_alarms_per_hour"], "median_lead_s": e["median_lead_s"],
                         "average_precision": r["average_precision"], "f1": r["f1"], "brier": r["brier"],
                         "o1_met": 1.0 if m["o1_met"] else 0.0})
        _log(f"prediction rep {rep + 1}/{reps or p.prediction_reps} done")
    metrics = ("recall_with_precursor", "recall_without_precursor", "false_alarms_per_hour", "median_lead_s",
               "average_precision", "f1", "brier", "o1_met")
    names = [m for m in (models or p.prediction_models)]
    pairs = [(m, "zscore") for m in names if m != "zscore" and "zscore" in names]
    return {"rows": rows, "summary": summarize(rows, "model", metrics),
            "comparisons": compare(rows, "model", ("recall_with_precursor", "average_precision",
                                                   "false_alarms_per_hour"), pairs)}


def exp_security(p: Profile) -> dict:
    from network.config import load_topology_config
    from network.security.evaluation import evaluate_run, run_scenario
    from network.security.identity import IdentityRegistry
    from network.security.policy import ZeroTrustPolicy
    from network.security.traffic import generate_security_plan

    topology = load_topology_config(os.path.join(CONFIG_DIR, "enterprise.yaml"))
    registry = IdentityRegistry.from_config(topology, os.path.join(CONFIG_DIR, "identities.yaml"))
    policy = ZeroTrustPolicy.from_file(os.path.join(CONFIG_DIR, "zero_trust_policy.yaml"))
    rows = []
    for seed in p.seeds:
        plan = generate_security_plan(registry, p.duration_s, seed=seed,
                                      n_attacks=max(2, int(p.duration_s / 1400)))
        r = evaluate_run(run_scenario(plan, registry, policy), registry, policy)
        ttd = [a["time_to_detect_s"] for a in r["attacks"] if a["time_to_detect_s"] is not None]
        ttq = [a["time_to_quarantine_s"] for a in r["attacks"] if a["time_to_quarantine_s"] is not None]
        rows.append({"condition": "zero_trust", "seed": seed, "detection_auroc": r["detection_auroc"],
                     "far": r["far"], "frr": r["frr"], "containment_cost": r["containment_cost"],
                     "trust_variance_benign": r["trust_variance_benign_mean"],
                     "detected_pct": 100.0 * sum(a["detected"] for a in r["attacks"]) / len(r["attacks"]),
                     "quarantined_pct": 100.0 * sum(a["quarantined"] for a in r["attacks"]) / len(r["attacks"]),
                     "mean_time_to_detect_s": statistics.mean(ttd) if ttd else None,
                     "mean_time_to_quarantine_s": statistics.mean(ttq) if ttq else None,
                     "enforcement_mismatches": r["enforcement_mismatches"]})
    metrics = [k for k in rows[0] if k not in ("condition", "seed")]
    _log("security done")
    return {"rows": rows, "summary": summarize(rows, "condition", metrics)}


def _predictor():
    from network.config import load_topology_config
    from network.decision.evaluation import train_predictor

    return train_predictor(load_topology_config(os.path.join(CONFIG_DIR, "enterprise-redundant.yaml")))


def exp_decisions(p: Profile, predictor) -> dict:
    from network.decision.evaluation import run_evaluation

    rows = []
    for seed in p.seeds:
        r = run_evaluation(seeds=(seed,), duration_s=p.duration_s, n_failures=p.n_failures,
                           n_attacks=p.n_attacks, predictor=predictor)
        run = r["runs"][0]
        rows.append({"condition": "decision_engine", "seed": seed, "correct_action_rate": run["correct_action_rate"],
                     "false_negative_rate": run["false_negative_rate"],
                     "false_positive_per_hour": run["false_positive_per_hour"],
                     "proactive_actions": run["proactive_actions"],
                     "decision_latency_ms_p95": run["decision_latency_ms_p95"]})
    _log("decisions done")
    metrics = [k for k in rows[0] if k not in ("condition", "seed")]
    return {"rows": rows, "summary": summarize(rows, "condition", metrics)}


HEALING_METRICS = ("mttr_s", "median_time_to_heal_s", "healed_before_manual_repair_pct", "user_loss_pct",
                   "success_rate", "rollback_rate", "escalated", "false_success_claims",
                   "median_attack_containment_s", "path_stretch")


def _healing_rows(report: dict, extra: Optional[dict] = None) -> List[dict]:
    from network.healing.evaluation import aggregate

    rows = []
    for run in report["runs"]:
        s = aggregate([run])[run["mode"]]
        row = {"mode": run["mode"], "seed": run["seed"], **{m: s.get(m) for m in HEALING_METRICS}}
        row.update(extra or {})
        rows.append(row)
    return rows


def exp_healing(p: Profile, predictor) -> dict:
    from network.healing.evaluation import MODES, run_evaluation

    rows = []
    for seed in p.seeds:
        r = run_evaluation(seeds=(seed,), duration_s=p.duration_s, modes=tuple(MODES), n_failures=p.n_failures,
                           n_attacks=p.n_attacks, use_predictor=False) if predictor is None else \
            _healing_with_predictor(seed, p, predictor, tuple(MODES))
        rows.extend(_healing_rows(r))
        _log(f"healing seed {seed} done")
    baselines = [(m, "no_healing") for m in ("restore_only", "full", "random_reroute")]
    ablations = [("full_no_prediction", "full"), ("full_no_verify", "full"), ("full_no_trust", "full")]
    return {"rows": rows, "summary": summarize(rows, "mode", HEALING_METRICS),
            "comparisons": {"vs_no_healing": compare(rows, "mode", ("mttr_s", "user_loss_pct"), baselines),
                            "ablations": compare(rows, "mode", ("mttr_s", "user_loss_pct", "success_rate",
                                                               "false_success_claims"), ablations)}}


def _healing_with_predictor(seed: int, p: Profile, predictor, modes, config_dir: str = CONFIG_DIR,
                            topology_file: str = "enterprise-redundant.yaml", step_s: float = 5.0,
                            physical=None) -> dict:
    """Like healing.evaluation.run_evaluation, but reusing one trained predictor."""
    from network.config import load_topology_config
    from network.decision.policy import DecisionPolicy
    from network.healing.evaluation import run_mode
    from network.security.identity import IdentityRegistry
    from network.security.policy import ZeroTrustPolicy

    topology = load_topology_config(os.path.join(config_dir, topology_file))
    registry = IdentityRegistry.from_config(topology, os.path.join(config_dir, "identities.yaml"))
    zt = ZeroTrustPolicy.from_file(os.path.join(config_dir, "zero_trust_policy.yaml"))
    dp = DecisionPolicy.from_file(os.path.join(config_dir, "decision_policy.yaml"))
    model, threshold = predictor if predictor else (None, None)
    runs = [run_mode(m, seed, topology, registry, zt, dp, model, threshold, p.duration_s, p.n_failures,
                     p.n_attacks, step_s, physical) for m in modes]
    return {"runs": runs}


def exp_baseline_d(p: Profile, workdir: str) -> dict:
    from network.experiments.topologies import write_config_dir

    n, h = p.campus
    config_dir = write_config_dir(os.path.join(workdir, f"campus-{n}x{h}"), n, h)
    rows = []
    for seed in p.seeds:
        r = _healing_with_predictor(seed, p, None, ("no_healing", "full", "random_reroute"), config_dir,
                                    "topology.yaml")
        rows.extend(_healing_rows(r))
    _log("baseline (d) done")
    return {"topology": f"campus-{n}x{h}", "rows": rows, "summary": summarize(rows, "mode", HEALING_METRICS),
            "comparisons": compare(rows, "mode", ("mttr_s", "user_loss_pct", "path_stretch"),
                                   [("random_reroute", "full"), ("full", "no_healing")])}


def exp_sensitivity(p: Profile, predictor) -> dict:
    seeds = p.sensitivity_seeds
    physical_rows, interval_rows = [], []
    for share in (0.0, 0.25, 0.5, 0.75):
        for seed in seeds:
            r = _healing_with_predictor(seed, p, predictor, ("full",),
                                        physical={"link": share, "switch": share, "router": share})
            physical_rows.extend(_healing_rows(r, {"physical_share": share}))
    for step in (2.0, 5.0, 10.0):
        for seed in seeds:
            r = _healing_with_predictor(seed, p, predictor, ("full",), step_s=step)
            interval_rows.extend(_healing_rows(r, {"interval_s": step}))
    _log("sensitivity (healing) done")
    precursor = {}
    for share in (0.0, 0.5, 1.0):
        res = exp_prediction(p, precursor_probability=share, models=("zscore", "logistic_regression"),
                             reps=len(seeds))
        precursor[str(share)] = res["summary"]
    _log("sensitivity (prediction) done")
    return {
        "physical_share": summarize(physical_rows, "physical_share", HEALING_METRICS),
        "interval_s": summarize(interval_rows, "interval_s", HEALING_METRICS),
        "precursor_share": precursor,
        "rows": physical_rows + interval_rows,
    }


def exp_scalability(p: Profile, workdir: str) -> dict:
    from network.analytics.scenarios import PlannedFailure, ScenarioPlan, ScenarioRunner
    from network.config import load_topology_config
    from network.decision.policy import DecisionPolicy
    from network.experiments.topologies import write_config_dir
    from network.healing.loop import ClosedLoop
    from network.security.identity import IdentityRegistry
    from network.security.policy import ZeroTrustPolicy
    from network.security.traffic import SecurityPlan

    rows = []
    for n, h in p.scalability_sizes:
        d = write_config_dir(os.path.join(workdir, f"scale-{n}x{h}"), n, h)
        topology = load_topology_config(os.path.join(d, "topology.yaml"))
        registry = IdentityRegistry.from_config(topology, os.path.join(d, "identities.yaml"))
        steps = p.scalability_steps
        plan = ScenarioPlan(duration_s=steps * 5.0, step_s=5.0, seed=1, failures=(
            PlannedFailure("link", ("rtr-core", "sw-2"), steps * 5.0 / 3, steps * 5.0 / 3),))
        runner = ScenarioRunner(plan, config=topology)
        loop = ClosedLoop(runner, registry, ZeroTrustPolicy.from_file(os.path.join(d, "zero_trust_policy.yaml")),
                          DecisionPolicy.from_file(os.path.join(d, "decision_policy.yaml")), topology,
                          SecurityPlan(duration_s=plan.duration_s, seed=1))
        timings: Dict[str, List[float]] = {"telemetry": [], "delivery": [], "zero_trust": [], "decisions": [],
                                           "healing": []}

        def timed(name, fn):
            def wrapper(*args, **kwargs):
                t0 = time.perf_counter()
                out = fn(*args, **kwargs)
                timings[name].append((time.perf_counter() - t0) * 1000.0)
                return out
            return wrapper

        source = runner.collector.sources[0]
        source.collect = timed("telemetry", source.collect)
        loop.service.step = timed("delivery", loop.service.step)
        loop.security.step = timed("zero_trust", loop.security.step)
        loop.decisions.step = timed("decisions", loop.decisions.step)
        loop.healing.step = timed("healing", loop.healing.step)
        started = time.perf_counter()
        runner.run()
        total_ms = (time.perf_counter() - started) * 1000.0 / (steps + 1)
        row = {"size": f"{n}x{h}", "seed": 0, "devices": len(topology.devices), "links": len(topology.links),
               "demands": len(loop.service.routes), "total_ms_per_step": total_ms}
        for k, v in timings.items():
            row[f"{k}_ms"] = statistics.mean(v) if v else None
            row[f"{k}_ms_p95"] = sorted(v)[int(0.95 * (len(v) - 1))] if v else None
        row["healing_verified"] = loop.healing.stats()["verified"]
        rows.append(row)
        runner.repository.close()
        _log(f"scalability {n}x{h} ({row['devices']} devices): {total_ms:.1f} ms/step")
    return {"rows": rows}


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------


def _write_csv(path: str, rows: List[dict]) -> None:
    if not rows:
        return
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def _figures(results: dict, outdir: str) -> List[str]:
    figs = os.path.join(outdir, "figures")
    os.makedirs(figs, exist_ok=True)
    written = []

    def save(name, content):
        with open(os.path.join(figs, name), "w") as fh:
            fh.write(content)
        written.append(f"figures/{name}")

    def bars(exp, metric, title, ylabel, scale=1.0, order=None, note="mean with 95% CI over seeds; simulated"):
        summ = results[exp]["summary"]
        labels = order or list(summ)
        labels = [l for l in labels if l in summ]
        m = [None if summ[l][metric]["mean"] is None else summ[l][metric]["mean"] * scale for l in labels]
        lo = [None if summ[l][metric]["ci_low"] is None else summ[l][metric]["ci_low"] * scale for l in labels]
        hi = [None if summ[l][metric]["ci_high"] is None else summ[l][metric]["ci_high"] * scale for l in labels]
        return svg.bar_chart(title, labels, m, lo, hi, ylabel, note=note)

    order = ["no_healing", "restore_only", "random_reroute", "full", "full_no_prediction", "full_no_trust",
             "full_no_verify"]
    if "healing" in results:
        save("healing_mttr.svg", bars("healing", "mttr_s", "Mean time to repair by mode", "seconds", order=order))
        save("healing_user_loss.svg", bars("healing", "user_loss_pct", "User-visible traffic loss during outages",
                                           "% of affected traffic", order=order))
        save("healing_false_claims.svg", bars("healing", "false_success_claims",
                                              "Repairs falsely reported successful (per run)", "count", order=order))
    if "prediction" in results:
        save("prediction_recall.svg", bars("prediction", "recall_with_precursor",
                                           "Failures with warning signs caught >= 30 s ahead", "%", scale=100))
        save("prediction_ap.svg", bars("prediction", "average_precision", "Average precision (test runs)", "AP"))
    if "scalability" in results:
        rows = results["scalability"]["rows"]
        series = {k: [(r["devices"], r[f"{k}_ms"]) for r in rows]
                  for k in ("telemetry", "delivery", "zero_trust", "decisions", "healing")}
        series["total"] = [(r["devices"], r["total_ms_per_step"]) for r in rows]
        save("scalability.svg", svg.line_chart("Processing time per 5 s step vs network size", series,
                                               "devices (log scale)", "ms per step", log_x=True,
                                               note="generated campus topologies; simulated; single Python process"))
    if "sensitivity" in results:
        s = results["sensitivity"]
        save("sensitivity_physical.svg", svg.line_chart(
            "Effect of physical (unfixable) fault share", {
                "MTTR (s)": [(float(k) * 100, v["mttr_s"]["mean"]) for k, v in s["physical_share"].items()],
                "user loss (%)": [(float(k) * 100, v["user_loss_pct"]["mean"]) for k, v in s["physical_share"].items()],
            }, "physical share of link/switch/router failures (%)", "value"))
        save("sensitivity_interval.svg", svg.line_chart(
            "Effect of the telemetry collection interval", {
                "MTTR (s)": [(float(k), v["mttr_s"]["mean"]) for k, v in s["interval_s"].items()],
                "user loss (%)": [(float(k), v["user_loss_pct"]["mean"]) for k, v in s["interval_s"].items()],
            }, "collection interval (s)", "value"))
    return written


def _table(summary: Dict[str, Dict[str, dict]], cols: Sequence[Tuple[str, str, float, int, str]],
           label: str) -> List[str]:
    lines = [f"| {label} | " + " | ".join(c[1] for c in cols) + " |", "|---" * (len(cols) + 1) + "|"]
    for g, s in summary.items():
        lines.append(f"| {g} | " + " | ".join(fmt_ci(s[c[0]], c[3], c[2], c[4]) for c in cols) + " |")
    return lines


def _tests(comp: Dict[str, dict]) -> List[str]:
    lines = ["| Metric | Comparison | n | Median difference | Effect r | p (Holm) | Significant |",
             "|---|---|---|---|---|---|---|"]
    for metric, tests in comp.items():
        for name, t in tests.items():
            md = "—" if t["median_diff"] is None else f"{t['median_diff']:.3g}"
            r = "—" if t["effect_r"] is None else f"{t['effect_r']:+.2f}"
            p = "—" if t.get("p_adj") is None else f"{t['p_adj']:.3g}"
            sig = {True: "yes", False: "no", None: "—"}[t.get("significant")]
            lines.append(f"| {metric} | {name} | {t['n']} | {md} | {r} | {p} | {sig} |")
    return lines


def render_report(results: dict) -> str:
    prov = results["provenance"]
    L = ["# Experiment report (Module 9)", "",
         "> All results come from the SIMULATED network. Failures, warning patterns (precursors) and attacks are "
         "SYNTHETIC; \"correct\" decisions mean agreement with written rules. See docs/results.md for interpretation.",
         "", f"- Profile: **{results['profile']}**, seeds {list(results['settings']['seeds'])}",
         f"- Commit: `{prov['git_commit']}` (uncommitted changes: {prov['git_uncommitted_changes']})",
         f"- Python {prov['python']} · {prov['platform']}",
         f"- Libraries: {', '.join(f'{k} {v}' for k, v in prov['libraries'].items() if v)}",
         f"- Runtime: {results['seconds']:.0f} s", "",
         "Cells: mean [95 % CI] over seeds. Tests: paired Wilcoxon signed-rank, Holm-corrected, α = 0.05.", ""]
    if "prediction" in results:
        L += ["## Failure prediction (Module 4)", ""]
        L += _table(results["prediction"]["summary"], [
            ("recall_with_precursor", "Caught ≥30 s, with signs", 100, 0, "%"),
            ("recall_without_precursor", "Caught, no signs", 100, 0, "%"),
            ("false_alarms_per_hour", "False alarms/h", 1, 2, ""),
            ("median_lead_s", "Median lead", 1, 0, " s"),
            ("average_precision", "Avg precision", 1, 2, ""),
            ("o1_met", "O1 met (share of reps)", 100, 0, "%")], "Model")
        L += ["", *_tests(results["prediction"]["comparisons"]), ""]
    if "security" in results:
        L += ["## Zero Trust (Module 5)", ""]
        L += _table(results["security"]["summary"], [
            ("detection_auroc", "AUROC", 1, 3, ""), ("far", "FAR", 100, 2, "%"), ("frr", "FRR", 100, 2, "%"),
            ("containment_cost", "Containment cost", 100, 1, "%"),
            ("detected_pct", "Attacks detected", 1, 0, "%"), ("quarantined_pct", "Attackers quarantined", 1, 0, "%"),
            ("mean_time_to_detect_s", "Time to detect", 1, 1, " s"),
            ("enforcement_mismatches", "PDP/PEP mismatches", 1, 0, "")], "Condition")
        L.append("")
    if "decisions" in results:
        L += ["## Decision engine (Module 6)", ""]
        L += _table(results["decisions"]["summary"], [
            ("correct_action_rate", "Correct actions", 100, 1, "%"),
            ("false_negative_rate", "Missed", 100, 1, "%"),
            ("false_positive_per_hour", "Unneeded actions/h", 1, 2, ""),
            ("proactive_actions", "Proactive actions/run", 1, 1, ""),
            ("decision_latency_ms_p95", "Latency p95", 1, 2, " ms")], "Condition")
        L.append("")
    healing_cols = [("mttr_s", "MTTR", 1, 0, " s"), ("healed_before_manual_repair_pct", "Healed before repair", 1, 0, "%"),
                    ("user_loss_pct", "User loss", 1, 1, "%"), ("success_rate", "Success", 100, 0, "%"),
                    ("escalated", "Escalated/run", 1, 1, ""), ("false_success_claims", "False claims/run", 1, 1, ""),
                    ("path_stretch", "Path stretch", 1, 2, "")]
    if "healing" in results:
        L += ["## Self-healing (Module 7): baselines and ablations", "",
              "Baselines (§9.2): (a) threshold alarms only and (c) static routing + manual recovery = `no_healing`; "
              "(b) prediction without actuation = `no_healing` with the predictor running (it does not act); "
              "(d) = `random_reroute`. Ablations (§9.4): `full_no_prediction`, `full_no_trust`, `full_no_verify`.", ""]
        L += _table(results["healing"]["summary"], healing_cols, "Mode")
        L += ["", "### Against no healing", "", *_tests(results["healing"]["comparisons"]["vs_no_healing"]),
              "", "### Ablations against full", "", *_tests(results["healing"]["comparisons"]["ablations"]), ""]
    if "baseline_d" in results:
        L += [f"## Baseline (d): random vs cost-based rerouting on {results['baseline_d']['topology']}", ""]
        L += _table(results["baseline_d"]["summary"], healing_cols, "Mode")
        L += ["", *_tests(results["baseline_d"]["comparisons"]), ""]
    if "sensitivity" in results:
        s = results["sensitivity"]
        L += ["## Sensitivity", "", "### Physical (software-unfixable) fault share", ""]
        L += _table(s["physical_share"], healing_cols[:4], "Physical share")
        L += ["", "### Telemetry collection interval", ""]
        L += _table(s["interval_s"], healing_cols[:4], "Interval (s)")
        L += ["", "### Share of failures with warning signs (prediction)", "",
              "| Warning-sign share | Model | Caught ≥30 s (with signs) | Caught (all) proxy: AP | False alarms/h |",
              "|---|---|---|---|---|"]
        for share, summ in s["precursor_share"].items():
            for model, m in summ.items():
                L.append(f"| {share} | {model} | {fmt_ci(m['recall_with_precursor'], 0, 100, '%')} | "
                         f"{fmt_ci(m['average_precision'], 2)} | {fmt_ci(m['false_alarms_per_hour'], 2)} |")
        L.append("")
    if "scalability" in results:
        L += ["## Scalability (generated campus topologies, one Python process)", "",
              "| Size | Devices | Links | Demands | Total ms/step | Telemetry | Delivery | Zero Trust | Decisions | Healing |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for r in results["scalability"]["rows"]:
            f = lambda k: "—" if r[k] is None else f"{r[k]:.1f}"
            L.append(f"| {r['size']} | {r['devices']} | {r['links']} | {r['demands']} | {r['total_ms_per_step']:.1f} | "
                     f"{f('telemetry_ms')} | {f('delivery_ms')} | {f('zero_trust_ms')} | {f('decisions_ms')} | "
                     f"{f('healing_ms')} |")
        L.append("")
    if results.get("figures"):
        L += ["## Figures", ""] + [f"![{f}]({f})" for f in results["figures"]] + [""]
    return "\n".join(L)


def run(profile_name: str, outdir: str, only: Optional[Sequence[str]] = None) -> dict:
    p = PROFILES[profile_name]
    experiments = [e for e in p.experiments if not only or e in only]
    os.makedirs(outdir, exist_ok=True)
    settings = {k: v for k, v in p.__dict__.items()}
    results = {"profile": profile_name, "settings": settings, "provenance": provenance.collect(settings)}
    started = time.perf_counter()
    workdir = tempfile.mkdtemp(prefix="selfheal-exp-")
    predictor = None
    if any(e in experiments for e in ("decisions", "healing", "sensitivity")):
        _log("training the shared predictor")
        predictor = _predictor()
    runners: Dict[str, Callable[[], dict]] = {
        "prediction": lambda: exp_prediction(p),
        "security": lambda: exp_security(p),
        "decisions": lambda: exp_decisions(p, predictor),
        "healing": lambda: exp_healing(p, predictor),
        "baseline_d": lambda: exp_baseline_d(p, workdir),
        "sensitivity": lambda: exp_sensitivity(p, predictor),
        "scalability": lambda: exp_scalability(p, workdir),
    }
    for name in experiments:
        _log(f"running {name}")
        results[name] = runners[name]()
        _write_csv(os.path.join(outdir, f"{name}.csv"), results[name].get("rows", []))
    results["seconds"] = time.perf_counter() - started
    results["figures"] = _figures(results, outdir)
    with open(os.path.join(outdir, "results.json"), "w") as fh:
        json.dump(results, fh, indent=2, sort_keys=True, default=str)
    with open(os.path.join(outdir, "report.md"), "w") as fh:
        fh.write(render_report(results))
    _log(f"done in {results['seconds']:.0f} s -> {outdir}")
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="network.experiments.runner", description=__doc__.split("\n")[0])
    ap.add_argument("--profile", choices=sorted(PROFILES), default="quick")
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="", help="comma-separated subset of experiments")
    args = ap.parse_args(argv)
    run(args.profile, args.out, [e for e in args.only.split(",") if e] or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
