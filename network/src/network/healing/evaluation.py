"""Self-healing evaluation (Submodule 7.7).

    PYTHONPATH=network/src python3 -m network.healing.evaluation --out data/module7

Runs the closed loop on identical failure + attack timelines (redundant
simulation topology, physical faults enabled) in several modes:

| Mode | What runs |
|---|---|
| ``no_healing``         | detection + decisions only; failures last until manual repair (baselines a/c) |
| ``restore_only``       | only RESTORE actions are executed (no rerouting, no security actions) |
| ``full``               | everything |
| ``full_no_prediction`` | ablation: no failure predictor (§9.4) |
| ``full_no_verify``     | ablation: actions are claimed successful without checking (§9.4) |

Metrics (research doc §8.2), over *outages* (link, switch and router
failures; impairments do not stop traffic in the delivery model):

- **time to detect**: failure start -> first non-HEALTHY health reading
- **time to heal / MTTR**: detection -> every affected demand delivered
  again (via repair or reroute), as seen by the delivery model; for an
  outage that carries no client traffic (e.g. the router on the redundant
  topology), detection -> the failed element is reported up again
- **user-visible loss**: share of affected demands' traffic lost during
  the outage
- **success rate**: verified / executed actions (every attempt counts);
  **rollback rate**: rolled back / executed; escalations to a person
- **false success claims**: actions reported successful while telemetry
  still showed the target down (only possible without verification)
- **attack containment**: attack start -> first verified security action

Everything is SIMULATED; failures, warning patterns and attacks SYNTHETIC.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from typing import Dict, List, Optional, Sequence

from network.analytics.scenarios import ScenarioRunner, generate_plan
from network.config import load_topology_config
from network.decision.evaluation import train_predictor
from network.decision.policy import DecisionPolicy
from network.healing.engine import HealingPolicy
from network.healing.loop import ClosedLoop
from network.security.identity import IdentityRegistry
from network.security.policy import ZeroTrustPolicy
from network.security.traffic import generate_security_plan
from network.telemetry.metrics import Metric

CONFIG_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "config"))
PHYSICAL = {"link": 0.5, "switch": 0.2, "router": 0.2}
OUTAGE_KINDS = ("link", "switch", "router")
SECURITY = ("QUARANTINE", "BLOCK", "ISOLATE")

MODES = {
    "no_healing": {"heal": False},
    "restore_only": {"policy": HealingPolicy(execute_types=frozenset({"RESTORE"}))},
    "full": {},
    "full_no_prediction": {"predictor": False},
    "full_no_verify": {"policy": HealingPolicy(verify=False)},
}


def run_mode(mode: str, seed: int, topology, registry, zt_policy, decision_policy, model, threshold,
             duration_s: float, n_failures: int, n_attacks: int) -> dict:
    from network.analytics.scenarios import _quiet_logger
    from network.prediction.online import OnlinePredictor

    spec = MODES[mode]
    plan = generate_plan(topology, duration_s, seed=seed, n_failures=n_failures, precursor_probability=0.5,
                         physical_probability=PHYSICAL)
    runner = ScenarioRunner(plan, config=topology)
    predictor = None
    if model is not None and spec.get("predictor", True):
        predictor = OnlinePredictor(model, threshold, runner.collector, logger=_quiet_logger("predictor")).attach()
    security_plan = generate_security_plan(registry, duration_s, seed=seed, n_attacks=n_attacks,
                                           warmup_s=900.0, gap_s=(600.0, 1500.0))
    loop = ClosedLoop(runner, registry, zt_policy, decision_policy, topology, security_plan, predictor,
                      healing_policy=spec.get("policy", HealingPolicy()), heal=spec.get("heal", True))
    result = runner.run()
    report = score(result, security_plan, loop)
    report.update({"mode": mode, "seed": seed})
    result.repository.close()
    return report


def _status_at(repo, key: str, ts: float) -> Optional[float]:
    kind, name = key.split(":", 1)
    metric = Metric.LINK_STATUS if kind == "link" else Metric.NODE_STATUS
    points = repo.query_points(metric, name, start=ts, end=ts + 0.5)
    return points[0][1] if points else None


def _target_restored_at(repo, key: str, start: float, end: float) -> Optional[float]:
    """First time in [start, end] from which telemetry reports the target up."""
    kind, name = key.split(":", 1)
    metric = Metric.LINK_STATUS if kind == "link" else Metric.NODE_STATUS
    good_from = None
    for ts, value in repo.query_points(metric, name, start=start, end=end + 0.5):
        if value == 1.0 and good_from is None:
            good_from = ts
        elif value != 1.0:
            good_from = None
    return good_from


def score(result, security_plan, loop) -> dict:
    svc = loop.service
    repo = result.repository
    step = result.plan.step_s
    outages = []
    for r in result.records:
        if r.kind not in OUTAGE_KINDS:
            continue
        name = r.affected[0].split(":", 1)[1]
        pairs = svc.demands_using(node=name) if r.kind in ("switch", "router") else svc.demands_using(link=name)
        detected = r.detected_at
        restored = (svc.restored_at(pairs, r.start, r.end + 60.0) if pairs
                    else _target_restored_at(repo, r.affected[0], r.start, r.end + 60.0))
        outages.append({
            "kind": r.kind, "target": list(r.target), "physical": r.physical, "affected_demands": len(pairs),
            "time_to_detect_s": None if detected is None else detected - r.start,
            "time_to_heal_s": None if (restored is None or detected is None) else max(0.0, restored - detected),
            "healed_before_manual_repair": restored is not None and restored < r.end,
            "user_loss": svc.lost_fraction(pairs, r.start, r.end) if pairs else None,
        })

    healing = loop.healing.stats() if loop.healing else None
    history = loop.healing.history() if loop.healing else []
    false_claims = 0
    for a in history:
        if a["verified_at"] is None or a["type"] != "RESTORE":
            continue
        if _status_at(repo, a["target"], a["verified_at"] + step) == 0.0:
            false_claims += 1

    containment = []
    for attack in security_plan.attacks:
        start = result.start + attack.start_s
        verified = [a["verified_at"] for a in history if a["type"] in SECURITY
                    and a["target"] == f"node:{attack.attacker}" and a["verified_at"] is not None
                    and a["verified_at"] >= start - step]
        containment.append(None if not verified else max(0.0, min(verified) - start))

    return {"outages": outages, "healing": healing, "false_success_claims": false_claims,
            "attack_containment_s": containment, "hours": (result.end - result.start) / 3600.0}


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _mean(values):
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


def aggregate(runs: List[dict]) -> dict:
    out = {}
    for mode in MODES:
        rs = [r for r in runs if r["mode"] == mode]
        if not rs:
            continue
        outages = [o for r in rs for o in r["outages"]]
        heal = [r["healing"] for r in rs if r["healing"]]
        executed = sum(h["executed"] for h in heal)
        out[mode] = {
            "outages": len(outages),
            "physical_outages": sum(o["physical"] for o in outages),
            "median_time_to_detect_s": _median(o["time_to_detect_s"] for o in outages),
            "mttr_s": _mean(o["time_to_heal_s"] for o in outages),
            "median_time_to_heal_s": _median(o["time_to_heal_s"] for o in outages),
            "healed_before_manual_repair_pct": 100.0 * sum(o["healed_before_manual_repair"] for o in outages)
            / len(outages) if outages else None,
            "user_loss_pct": None if _mean(o["user_loss"] for o in outages) is None
            else 100.0 * _mean(o["user_loss"] for o in outages),
            "executed": executed,
            "success_rate": (sum(h["verified"] for h in heal) / executed) if executed else None,
            "rollback_rate": (sum(h["rollbacks"] for h in heal) / executed) if executed else None,
            "escalated": sum(h["escalated"] for h in heal),
            "false_success_claims": sum(r["false_success_claims"] for r in rs),
            "median_attack_containment_s": _median(c for r in rs for c in r["attack_containment_s"]),
            "attacks_contained_pct": 100.0 * sum(c is not None for r in rs for c in r["attack_containment_s"])
            / max(1, sum(len(r["attack_containment_s"]) for r in rs)),
        }
    return out


def run_evaluation(seeds: Sequence[int] = (1, 2, 3), duration_s: float = 10800.0, modes: Sequence[str] = tuple(MODES),
                   use_predictor: bool = True, n_failures: int = 12, n_attacks: int = 6,
                   config_dir: str = CONFIG_DIR) -> dict:
    topology = load_topology_config(os.path.join(config_dir, "enterprise-redundant.yaml"))
    registry = IdentityRegistry.from_config(topology, os.path.join(config_dir, "identities.yaml"))
    zt_policy = ZeroTrustPolicy.from_file(os.path.join(config_dir, "zero_trust_policy.yaml"))
    decision_policy = DecisionPolicy.from_file(os.path.join(config_dir, "decision_policy.yaml"))
    model = threshold = None
    if use_predictor:
        model, threshold = train_predictor(topology)
    runs = [run_mode(m, s, topology, registry, zt_policy, decision_policy, model, threshold, duration_s,
                     n_failures, n_attacks) for s in seeds for m in modes]
    return {
        "simulated": True,
        "synthetic": True,
        "physical_probability": PHYSICAL,
        "warning": ("SIMULATED network and execution; SYNTHETIC failures, warning patterns and attacks; the "
                    "delivery model's link load is not route-aware."),
        "summary": aggregate(runs),
        "runs": runs,
    }


def _f(v, spec="{:.1f}"):
    return "—" if v is None else spec.format(v)


def render_markdown(report: dict) -> str:
    lines = ["# Module 7 — self-healing evaluation", "", f"> {report['warning']}", "",
             "Outages = link, switch and router failures; physical (unfixable by software) with probability "
             + ", ".join(f"{k} {v:.0%}" for k, v in report["physical_probability"].items()) + ".", "",
             "| Mode | Outages | MTTR | Median time to heal | Healed before manual repair | User-visible loss "
             "| Success rate | Rollback rate | Escalated | False success claims | Attack containment |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for mode, s in report["summary"].items():
        lines.append(
            f"| {mode} | {s['outages']} ({s['physical_outages']} physical) | {_f(s['mttr_s'], '{:.0f} s')} | "
            f"{_f(s['median_time_to_heal_s'], '{:.0f} s')} | {_f(s['healed_before_manual_repair_pct'], '{:.0f}%')} | "
            f"{_f(s['user_loss_pct'], '{:.1f}%')} | {_f(s['success_rate'], '{:.0%}')} | "
            f"{_f(s['rollback_rate'], '{:.0%}')} | {s['escalated']} | {s['false_success_claims']} | "
            f"{_f(s['median_attack_containment_s'], '{:.0f} s')} ({_f(s['attacks_contained_pct'], '{:.0f}%')}) |")
    lines += ["", "MTTR runs from detection to every affected demand being delivered again (repair or reroute); "
              "without healing that is the manual repair at the failure's planned end. Time to detect: "
              f"{_f(next(iter(report['summary'].values()))['median_time_to_detect_s'], '{:.0f} s')} (median; "
              "simulated telemetry reacts within one 5 s collection).", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="network.healing.evaluation", description=__doc__.split("\n")[0])
    p.add_argument("--out", required=True)
    p.add_argument("--seeds", default="1,2,3")
    p.add_argument("--duration", type=float, default=10800.0)
    p.add_argument("--modes", default=",".join(MODES))
    p.add_argument("--no-predictor", action="store_true")
    args = p.parse_args(argv)
    report = run_evaluation([int(s) for s in args.seeds.split(",")], args.duration, args.modes.split(","),
                            not args.no_predictor)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "report.json"), "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True, default=str)
    with open(os.path.join(args.out, "report.md"), "w") as fh:
        fh.write(render_markdown(report))
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
