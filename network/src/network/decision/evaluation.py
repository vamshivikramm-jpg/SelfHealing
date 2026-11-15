"""End-to-end decision evaluation (Submodule 6.7).

    PYTHONPATH=network/src python3 -m network.decision.evaluation --out data/module6

On the redundant simulation topology, every 5 s step runs the whole
intelligence layer on one virtual clock:

    failures (Module 3 scenarios) -> telemetry + health (Module 2)
    -> failure prediction (Module 4, z-score model trained on separate runs)
    -> Zero Trust on simulated traffic with synthetic attacks (Module 5)
    -> DecisionEngine (Module 6)

and scores the decisions against ground truth with the §8.4 metrics:

- **Correct-action rate**: incidents where every expected intent was
  active at some point during the incident (decided during it, or
  already active when it began, e.g. a quarantine from an earlier attack
  or a MONITOR raised during the warning period). Expected intents come from written rules
  (``expected_intents``), not from an operator, so this measures
  agreement with those rules.
- **False-negative healing**: incidents with no intent at all on the
  affected targets.
- **False-positive healing**: real actions (not MONITOR) on targets no
  incident affects, per hour. Proactive reroutes during a failure's
  warning period are counted separately as *proactive*, not as false.
- **Decision latency**: p95 wall-clock time of one engine step.
- **Time to decide**: from incident start to the first correct intent.

Everything is SIMULATED; warning patterns and attacks are SYNTHETIC.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Set, Tuple

from network.analytics.scenarios import ScenarioRunner, generate_plan
from network.config import load_topology_config
from network.decision.engine import DecisionEngine
from network.decision.graph import NetworkGraph, path_links
from network.decision.intents import ACTIONS, IntentType
from network.decision.policy import DecisionPolicy
from network.decision.risk import Signals
from network.security.engine import ZeroTrustEngine
from network.security.identity import IdentityRegistry
from network.security.policy import ZeroTrustPolicy
from network.security.traffic import TrafficGenerator, generate_security_plan

CONFIG_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "config"))
GRACE_S = 30.0
SECURITY_ACTIONS = {"QUARANTINE", "BLOCK", "ISOLATE"}


def expected_intents(record, baseline: Dict[tuple, tuple], graph: NetworkGraph) -> List[Set[Tuple[str, str]]]:
    """Ground-truth rules. Returns a list of alternatives per requirement:
    the incident is handled correctly if, for each set, at least one
    ``(type, target)`` in it was decided."""
    kind, target = record["kind"], tuple(record["target"])
    if kind in ("router", "switch"):
        key = f"node:{target[0]}"
        needs = [{("RESTORE", key)}]
        used = any(target[0] in path[1:-1] for path in baseline.values())
        if used and _has_alternative(graph, baseline, avoid_nodes={target[0]}):
            needs.append({("REROUTE", key)})
        return needs
    link = record["affected"][0].split(":", 1)[1]
    key = f"link:{link}"
    used = any(link in path_links(p) for p in baseline.values())
    alternative = used and _has_alternative(graph, baseline, avoid_links={link})
    if kind == "link":
        return [{("RESTORE", key)}] + ([{("REROUTE", key)}] if alternative else [])
    if kind == "bandwidth":
        return [{("LOAD_BALANCE", key), ("REROUTE", key)}] if alternative else [{("MONITOR", key)}]
    return [{("REROUTE", key)}] if alternative else [{("MONITOR", key)}]  # latency / packet_loss


def _has_alternative(graph, baseline, avoid_links=frozenset(), avoid_nodes=frozenset()) -> bool:
    for (src, dst), path in baseline.items():
        if set(path_links(path)) & set(avoid_links) or set(path[1:-1]) & set(avoid_nodes):
            if graph.shortest_path(src, dst, avoid_links=set(avoid_links), avoid_nodes=set(avoid_nodes)):
                return True
    return False


def train_predictor(topology, seeds=(101, 102), duration_s: float = 7200.0):
    """Z-score model (Module 4 baseline that met O1) + threshold from a validation run."""
    from network.prediction.data import SuiteConfig, build_run
    from network.prediction.evaluation import EventScorer, choose_threshold
    from network.prediction.models import ZScoreModel

    suite = SuiteConfig(duration_s=duration_s, n_failures=10, precursor_probability=0.6)
    train = build_run(topology, seeds[0], suite)
    val = build_run(topology, seeds[1], suite)
    model = ZScoreModel().fit(train.X, train.y, train.feature_names)
    threshold = choose_threshold(EventScorer(val), model.predict_proba(val.X))["threshold"]
    return model, threshold


class IntelligenceLoop:
    """Runs Zero Trust + decisions after every telemetry collection."""

    def __init__(self, runner, registry, zt_policy, decision_policy, topology, security_plan, predictor=None):
        self.runner = runner
        self.predictor = predictor
        self.security = ZeroTrustEngine(registry, zt_policy)
        self.traffic = TrafficGenerator(registry, security_plan)
        self.decisions = DecisionEngine(topology, decision_policy)
        self.intents: List[dict] = []
        self._by_id: Dict[str, dict] = {}
        self.start = runner.start_time
        runner.collector.add_listener(self.on_collection)

    def on_collection(self, result) -> None:
        ts = result.timestamp
        flows, auth = self.traffic.step(ts - self.start, ts)
        sec = self.security.step(ts, flows, auth)
        anomalies: Dict[str, List[str]] = defaultdict(list)
        for a in sec.anomalies:
            anomalies[a.subject].append(a.kind)
        snapshot = self.security.trust.snapshot(ts)
        predictions = {}
        threshold = None
        if self.predictor is not None:
            threshold = self.predictor.threshold
            predictions = {p["target"]: p["score"] for p in self.predictor.predictions()
                           if p["score"] is not None and p["timestamp"] == ts}
        signals = Signals(
            ts=ts,
            health={k: r.to_dict() for k, r in self.runner.collector.health().items()},
            predictions=predictions,
            trust={s: {"trust": v["trust"], "level": v["level"]} for s, v in snapshot.items()},
            threat=sec.threat.value if sec.threat else 0.0,
            anomalies=dict(anomalies),
            prediction_threshold=threshold,
        )
        decided = self.decisions.step(signals)
        for intent in decided.new:
            row = intent.to_dict()
            row["resolved_ts"] = None
            self._by_id[intent.id] = row
            self.intents.append(row)
        for intent in decided.resolved:
            if intent.id in self._by_id:
                self._by_id[intent.id]["resolved_ts"] = ts


def run_once(seed: int, topology, registry, zt_policy, decision_policy, predictor_model=None,
             predictor_threshold=None, duration_s: float = 10800.0, n_failures: int = 12, n_attacks: int = 6):
    from network.prediction.online import OnlinePredictor

    plan = generate_plan(topology, duration_s, seed=seed, n_failures=n_failures, precursor_probability=0.5)
    runner = ScenarioRunner(plan, config=topology)
    predictor = None
    if predictor_model is not None:
        # proactive decisions need a score above the decision policy's proactive
        # level; use the model's own alarm threshold if that is stricter
        from network.analytics.scenarios import _quiet_logger

        predictor = OnlinePredictor(predictor_model, predictor_threshold, runner.collector,
                                    logger=_quiet_logger("predictor")).attach()
    security_plan = generate_security_plan(registry, duration_s, seed=seed, n_attacks=n_attacks,
                                           warmup_s=900.0, gap_s=(600.0, 1500.0))
    loop = IntelligenceLoop(runner, registry, zt_policy, decision_policy, topology, security_plan, predictor)
    result = runner.run()
    return result, security_plan, loop


def score_run(result, security_plan, loop) -> dict:
    engine = loop.decisions
    graph = engine.base_graph
    baseline = engine.baseline
    intents = loop.intents
    start = result.start

    incidents = []
    for r in result.records:
        rec = r.to_dict()
        incidents.append({
            "kind": rec["kind"], "target": rec["target"], "start": rec["start"], "end": rec["end"],
            "precursor_start": rec["precursor_start"],
            "keys": set(rec["affected"]), "expected": expected_intents(rec, baseline, graph),
        })
    for a in security_plan.attacks:
        key = f"node:{a.attacker}"
        incidents.append({
            "kind": f"attack:{a.kind}", "target": [a.attacker], "start": start + a.start_s,
            "end": start + a.end_s, "precursor_start": None, "keys": {key},
            "expected": [{(t, key) for t in SECURITY_ACTIONS}],
        })

    rows = []
    for inc in incidents:
        lo, hi = inc["start"], inc["end"] + GRACE_S
        during = [i for i in intents if i["target"] in inc["keys"] and i["ts"] <= hi
                  and (i["resolved_ts"] is None or i["resolved_ts"] >= lo)]
        decided = {(i["type"], i["target"]) for i in during}
        satisfied = [bool(options & decided) for options in inc["expected"]]
        first_ok = min((max(i["ts"], inc["start"]) for i in during
                        if any((i["type"], i["target"]) in options for options in inc["expected"])), default=None)
        rows.append({
            "kind": inc["kind"], "target": inc["target"],
            "expected": [sorted(f"{t}@{k}" for t, k in options) for options in inc["expected"]],
            "decided": sorted(f"{t}@{k}" for t, k in decided),
            "correct": all(satisfied), "missed": not during,
            "time_to_decide_s": None if first_ok is None else first_ok - inc["start"],
        })

    def related(intent) -> bool:
        for inc in incidents:
            lo = (inc["precursor_start"] or inc["start"]) - GRACE_S
            if intent["target"] in inc["keys"] and lo <= intent["ts"] <= inc["end"] + GRACE_S:
                return True
        return False

    actions = [i for i in intents if i["type"] in {t.value for t in ACTIONS}]
    proactive = [i for i in actions if i["params"].get("proactive") and related(i)]
    false_pos = [i for i in actions if not related(i)]
    hours = (result.end - result.start) / 3600.0
    lat = sorted(engine.latencies_ms)
    return {
        "seed": result.plan.seed,
        "hours": hours,
        "incidents": rows,
        "correct_action_rate": sum(r["correct"] for r in rows) / len(rows) if rows else None,
        "false_negative_rate": sum(r["missed"] for r in rows) / len(rows) if rows else None,
        "false_positive_actions": len(false_pos),
        "false_positive_per_hour": len(false_pos) / hours if hours else None,
        "false_positive_examples": [{k: i[k] for k in ("type", "target", "reason", "ts")} for i in false_pos[:5]],
        "proactive_actions": len(proactive),
        "intents": len(intents),
        "pending_approval": sum(1 for i in intents if i["status"] == "PENDING_APPROVAL"),
        "decision_latency_ms_p95": lat[int(0.95 * (len(lat) - 1))] if lat else None,
        "decision_latency_ms_max": lat[-1] if lat else None,
    }


def aggregate(runs: List[dict]) -> dict:
    by_kind: Dict[str, List[dict]] = defaultdict(list)
    for run in runs:
        for row in run["incidents"]:
            by_kind[row["kind"]].append(row)

    def mean(key):
        values = [r[key] for r in runs if r[key] is not None]
        return statistics.mean(values) if values else None

    return {
        "runs": len(runs),
        "hours": sum(r["hours"] for r in runs),
        "incidents": sum(len(r["incidents"]) for r in runs),
        "correct_action_rate": mean("correct_action_rate"),
        "false_negative_rate": mean("false_negative_rate"),
        "false_positive_per_hour": mean("false_positive_per_hour"),
        "proactive_actions": sum(r["proactive_actions"] for r in runs),
        "decision_latency_ms_p95": max((r["decision_latency_ms_p95"] or 0) for r in runs),
        "by_kind": {
            kind: {
                "incidents": len(rows),
                "correct_pct": 100.0 * sum(r["correct"] for r in rows) / len(rows),
                "missed_pct": 100.0 * sum(r["missed"] for r in rows) / len(rows),
                "median_time_to_decide_s": statistics.median(t) if (t := [r["time_to_decide_s"] for r in rows
                                                                          if r["time_to_decide_s"] is not None])
                else None,
            }
            for kind, rows in sorted(by_kind.items())
        },
    }


def run_evaluation(seeds: Sequence[int] = (1, 2, 3), duration_s: float = 10800.0, use_predictor: bool = True,
                   config_dir: str = CONFIG_DIR, n_failures: int = 12, n_attacks: int = 6,
                   topology_file: str = "enterprise-redundant.yaml", predictor=None) -> dict:
    topology = load_topology_config(os.path.join(config_dir, topology_file))
    registry = IdentityRegistry.from_config(topology, os.path.join(config_dir, "identities.yaml"))
    zt_policy = ZeroTrustPolicy.from_file(os.path.join(config_dir, "zero_trust_policy.yaml"))
    decision_policy = DecisionPolicy.from_file(os.path.join(config_dir, "decision_policy.yaml"))
    model = threshold = None
    if predictor is not None:
        model, threshold = predictor
    elif use_predictor:
        model, threshold = train_predictor(topology)
    runs = []
    for seed in seeds:
        result, security_plan, loop = run_once(seed, topology, registry, zt_policy, decision_policy, model,
                                               threshold, duration_s, n_failures, n_attacks)
        runs.append(score_run(result, security_plan, loop))
        result.repository.close()
    return {
        "simulated": True,
        "synthetic": True,
        "topology": f"{topology.name} (simulation only)",
        "predictor": {"model": "zscore", "threshold": threshold} if model is not None else None,
        "policy": decision_policy.to_dict(),
        "warning": ("SIMULATED network, SYNTHETIC warning patterns and attacks, and expected actions from "
                    "written rules: the correct-action rate measures agreement with those rules."),
        "summary": aggregate(runs),
        "runs": runs,
    }


def _fmt(v, spec="{:.2f}"):
    return "—" if v is None else spec.format(v)


def render_markdown(report: dict) -> str:
    s = report["summary"]
    lines = [
        "# Module 6 — decision engine evaluation", "", f"> {report['warning']}", "",
        f"{s['runs']} runs, {s['hours']:.1f} simulated hours, {s['incidents']} incidents "
        f"(failures + attacks) on the redundant simulation topology. Predictor: "
        f"{'z-score, threshold ' + _fmt(report['predictor']['threshold'], '{:.3f}') if report['predictor'] else 'off'}.",
        "", "| Metric | Value |", "|---|---|",
        f"| Correct-action rate | {_fmt(s['correct_action_rate'], '{:.1%}')} |",
        f"| Missed incidents (false-negative healing) | {_fmt(s['false_negative_rate'], '{:.1%}')} |",
        f"| Unneeded actions per hour (false-positive healing) | {_fmt(s['false_positive_per_hour'])} |",
        f"| Proactive actions before a failure | {s['proactive_actions']} |",
        f"| Decision latency p95 | {_fmt(s['decision_latency_ms_p95'], '{:.2f} ms')} |",
        "", "## By incident type", "",
        "| Incident | Count | Correct | Missed | Median time to decide |", "|---|---|---|---|---|",
    ]
    for kind, k in s["by_kind"].items():
        lines.append(f"| {kind} | {k['incidents']} | {k['correct_pct']:.0f}% | {k['missed_pct']:.0f}% | "
                     f"{_fmt(k['median_time_to_decide_s'], '{:.0f} s')} |")
    lines += ["", "Reroute convergence time (§8.4) needs Module 7 to execute reroutes and is not measured here.",
              ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="network.decision.evaluation", description=__doc__.split("\n")[0])
    p.add_argument("--out", required=True)
    p.add_argument("--seeds", default="1,2,3")
    p.add_argument("--duration", type=float, default=10800.0)
    p.add_argument("--no-predictor", action="store_true", help="skip Module 4 (no numpy needed)")
    args = p.parse_args(argv)
    report = run_evaluation([int(s) for s in args.seeds.split(",")], args.duration, not args.no_predictor)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "report.json"), "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True, default=str)
    with open(os.path.join(args.out, "report.md"), "w") as fh:
        fh.write(render_markdown(report))
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
