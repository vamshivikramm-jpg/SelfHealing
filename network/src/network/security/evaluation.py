"""Zero Trust evaluation (Submodule 5.7).

    PYTHONPATH=network/src python3 -m network.security.evaluation --out data/module5

Runs simulated security scenarios (benign traffic + synthetic attacks)
through the :class:`ZeroTrustEngine` on a virtual clock and measures the
research document's §8.3 metrics:

- **Detection AUROC**: how well ``trust drop`` (base trust - trust) ranks
  "this device is attacking right now" above "it is not", over every
  device and step.
- **False-accept rate (FAR)**: share of attack flows that were allowed.
- **False-reject rate (FRR)**: share of legitimate flows (benign, allowed
  by the static policy, genuine identity) from devices that never attack
  that were denied.
- **Containment cost**: share of legitimate flows from devices that do
  attack (their normal traffic) that were denied, i.e. what quarantining
  an attacker's machine costs. Reported separately from FRR.
- **Trust stability**: variance of trust for devices that never attack.
- Per attack: time to first anomaly, time to quarantine, share of the
  attack's flows that were blocked.

All traffic is SIMULATED and all attacks SYNTHETIC; the report says so.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from network.events.event_bus import EventBus
from network.events.event_types import EventType
from network.security.engine import ZeroTrustEngine
from network.security.records import BENIGN
from network.security.traffic import SecurityPlan, TrafficGenerator, generate_security_plan

DEFAULT_START = 1_700_000_000.0
CONFIG_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "config"))


def auroc(scores: Sequence[float], labels: Sequence[int]) -> Optional[float]:
    """Area under the ROC curve via the rank-sum (Mann-Whitney U) statistic."""
    pos = sum(1 for y in labels if y)
    neg = len(labels) - pos
    if pos == 0 or neg == 0:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    rank_sum = sum(r for r, y in zip(ranks, labels) if y)
    return (rank_sum - pos * (pos + 1) / 2.0) / (pos * neg)


@dataclass
class RunResult:
    plan: SecurityPlan
    start: float
    flows: List[tuple] = field(default_factory=list)       # (flow, decision, enforced)
    trust: Dict[str, List[Tuple[float, float]]] = field(default_factory=dict)
    anomalies: list = field(default_factory=list)
    quarantines: List[Tuple[float, str]] = field(default_factory=list)
    mismatches: int = 0


def run_scenario(plan: SecurityPlan, registry, policy, start: float = DEFAULT_START) -> RunResult:
    bus = EventBus()
    engine = ZeroTrustEngine(registry, policy, bus)
    generator = TrafficGenerator(registry, plan)
    result = RunResult(plan=plan, start=start, trust={s: [] for s in registry.names()})
    bus.subscribe(lambda e: result.quarantines.append((_ts(e.timestamp), e.data["subject"]))
                  if e.type == EventType.DEVICE_QUARANTINED else None)
    steps = int(round(plan.duration_s / plan.step_s))
    for i in range(steps):
        t = i * plan.step_s
        ts = start + t
        flows, auth = generator.step(t, ts)
        step = engine.step(ts, flows, auth)
        result.flows.extend(step.decided)
        result.anomalies.extend(step.anomalies)
        for subject in registry.names():
            result.trust[subject].append((ts, engine.trust.trust(subject, ts)))
    result.mismatches = engine.mismatches
    return result


def _ts(iso: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(iso).timestamp()


def evaluate_run(result: RunResult, registry, policy) -> dict:
    plan, start = result.plan, result.start
    attackers_at: Dict[Tuple[str, float], bool] = {}
    for attack in plan.attacks:
        for ts, _ in result.trust[attack.attacker]:
            if attack.active(ts - start):
                attackers_at[(attack.attacker, ts)] = True

    # detection AUROC over (device, step): score = trust drop from base
    scores, labels = [], []
    for subject, series in result.trust.items():
        base = registry.get(subject).effective_base_trust(policy.trust.posture_penalty)
        for ts, value in series:
            scores.append(base - value)
            labels.append(1 if attackers_at.get((subject, ts)) else 0)

    # access decisions
    attacker_names = {a.attacker for a in plan.attacks}
    legit = legit_denied = attack_flows = attack_allowed = mistakes = mistakes_denied = 0
    own = own_denied = 0
    for flow, decision, enforced in result.flows:
        if flow.is_attack:
            attack_flows += 1
            attack_allowed += enforced
            continue
        role = registry.get(flow.src).role
        if flow.label == BENIGN and policy.permits_statically(role, flow.dst, flow.dst_port):
            if flow.src in attacker_names:
                own += 1
                own_denied += not enforced
            else:
                legit += 1
                legit_denied += not enforced
        else:
            mistakes += 1
            mistakes_denied += not enforced

    benign_devices = [s for s in result.trust if s not in attacker_names]
    variances = {s: statistics.pvariance([v for _, v in result.trust[s]]) for s in benign_devices}
    collateral = sorted({s for _, s in result.quarantines if s not in attacker_names})

    attacks = []
    for attack in plan.attacks:
        a_start, a_end = start + attack.start_s, start + attack.end_s
        first = min((a.ts for a in result.anomalies
                     if a.subject == attack.attacker and a_start <= a.ts < a_end), default=None)
        quarantined = min((ts for ts, s in result.quarantines
                           if s == attack.attacker and a_start <= ts < a_end + plan.step_s), default=None)
        mine = [enf for f, _, enf in result.flows if f.attack_id == attack.attack_id]
        attacks.append({
            "attack_id": attack.attack_id, "kind": attack.kind, "attacker": attack.attacker,
            "duration_s": attack.duration_s,
            "detected": first is not None,
            "time_to_detect_s": None if first is None else first - a_start,
            "quarantined": quarantined is not None,
            "time_to_quarantine_s": None if quarantined is None else quarantined - a_start,
            "attack_flows": len(mine),
            "blocked_pct": (100.0 * sum(1 for e in mine if not e) / len(mine)) if mine else None,
        })

    return {
        "seed": plan.seed,
        "duration_s": plan.duration_s,
        "detection_auroc": auroc(scores, labels),
        "far": attack_allowed / attack_flows if attack_flows else None,
        "frr": legit_denied / legit if legit else None,
        "containment_cost": own_denied / own if own else None,
        "legitimate_flows": legit,
        "attack_flows": attack_flows,
        "benign_mistakes": mistakes,
        "benign_mistakes_denied_pct": (100.0 * mistakes_denied / mistakes) if mistakes else None,
        "trust_variance_benign_mean": statistics.mean(variances.values()) if variances else None,
        "trust_variance_benign_max": max(variances.values()) if variances else None,
        "benign_devices_quarantined": collateral,
        "enforcement_mismatches": result.mismatches,
        "attacks": attacks,
    }


def aggregate(runs: List[dict]) -> dict:
    def mean(key):
        values = [r[key] for r in runs if r[key] is not None]
        return statistics.mean(values) if values else None

    by_kind: Dict[str, List[dict]] = defaultdict(list)
    for r in runs:
        for a in r["attacks"]:
            by_kind[a["kind"]].append(a)
    kinds = {}
    for kind, items in sorted(by_kind.items()):
        det = [a["time_to_detect_s"] for a in items if a["time_to_detect_s"] is not None]
        qua = [a["time_to_quarantine_s"] for a in items if a["time_to_quarantine_s"] is not None]
        blocked = [a["blocked_pct"] for a in items if a["blocked_pct"] is not None]
        kinds[kind] = {
            "attacks": len(items),
            "detected_pct": 100.0 * sum(a["detected"] for a in items) / len(items),
            "quarantined_pct": 100.0 * sum(a["quarantined"] for a in items) / len(items),
            "median_time_to_detect_s": statistics.median(det) if det else None,
            "median_time_to_quarantine_s": statistics.median(qua) if qua else None,
            "mean_blocked_pct": statistics.mean(blocked) if blocked else None,
        }
    return {
        "runs": len(runs),
        "detection_auroc": mean("detection_auroc"),
        "far": mean("far"),
        "frr": mean("frr"),
        "containment_cost": mean("containment_cost"),
        "trust_variance_benign_mean": mean("trust_variance_benign_mean"),
        "benign_devices_quarantined": sum(len(r["benign_devices_quarantined"]) for r in runs),
        "enforcement_mismatches": sum(r["enforcement_mismatches"] for r in runs),
        "by_attack_kind": kinds,
    }


def run_evaluation(registry, policy, seeds: Sequence[int] = (1, 2, 3), duration_s: float = 10800.0,
                   n_attacks: int = 8) -> dict:
    runs = []
    for seed in seeds:
        plan = generate_security_plan(registry, duration_s, seed=seed, n_attacks=n_attacks)
        runs.append(evaluate_run(run_scenario(plan, registry, policy), registry, policy))
    return {
        "simulated": True,
        "synthetic_attacks": True,
        "warning": ("SIMULATED traffic and SYNTHETIC attacks: results show the designed attack patterns are "
                    "caught, not that real attackers would be."),
        "policy": policy.to_dict(),
        "summary": aggregate(runs),
        "runs": runs,
    }


def _fmt(v, spec="{:.2f}", none="—"):
    return none if v is None else spec.format(v)


def render_markdown(report: dict) -> str:
    s = report["summary"]
    lines = [
        "# Module 5 — Zero Trust evaluation", "", f"> {report['warning']}", "",
        f"Runs: {s['runs']} (seeds {', '.join(str(r['seed']) for r in report['runs'])}), "
        f"{report['runs'][0]['duration_s'] / 3600:.1f} h each.", "",
        "| Metric | Value |", "|---|---|",
        f"| Detection AUROC (trust drop vs. attacking) | {_fmt(s['detection_auroc'], '{:.3f}')} |",
        f"| False-accept rate (attack flows allowed) | {_fmt(s['far'], '{:.2%}')} |",
        f"| False-reject rate (legitimate flows of non-attacking devices denied) | {_fmt(s['frr'], '{:.2%}')} |",
        f"| Containment cost (attackers' own normal traffic denied) | {_fmt(s['containment_cost'], '{:.2%}')} |",
        f"| Trust variance, non-attacking devices (mean) | {_fmt(s['trust_variance_benign_mean'], '{:.5f}')} |",
        f"| Non-attacking devices quarantined | {s['benign_devices_quarantined']} |",
        f"| Policy/enforcement disagreements | {s['enforcement_mismatches']} |",
        "", "## By attack type", "",
        "| Attack | Count | Detected | Quarantined | Median time to detect | Median time to quarantine "
        "| Attack flows blocked |",
        "|---|---|---|---|---|---|---|",
    ]
    for kind, k in s["by_attack_kind"].items():
        lines.append(f"| {kind} | {k['attacks']} | {k['detected_pct']:.0f}% | {k['quarantined_pct']:.0f}% | "
                     f"{_fmt(k['median_time_to_detect_s'], '{:.0f} s')} | "
                     f"{_fmt(k['median_time_to_quarantine_s'], '{:.0f} s')} | "
                     f"{_fmt(k['mean_blocked_pct'], '{:.0f}%')} |")
    lines += ["", "Time is measured from the attack's first step; the step is 5 s, so 0 s means the "
              "first step of the attack.", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    from network.config import load_topology_config
    from network.security.identity import IdentityRegistry
    from network.security.policy import ZeroTrustPolicy

    p = argparse.ArgumentParser(prog="network.security.evaluation", description=__doc__.split("\n")[0])
    p.add_argument("--out", required=True)
    p.add_argument("--seeds", default="1,2,3")
    p.add_argument("--duration", type=float, default=10800.0)
    p.add_argument("--attacks", type=int, default=8)
    p.add_argument("--config-dir", default=CONFIG_DIR)
    args = p.parse_args(argv)
    topology = load_topology_config(os.path.join(args.config_dir, "enterprise.yaml"))
    registry = IdentityRegistry.from_config(topology, os.path.join(args.config_dir, "identities.yaml"))
    policy = ZeroTrustPolicy.from_file(os.path.join(args.config_dir, "zero_trust_policy.yaml"))
    report = run_evaluation(registry, policy, [int(s) for s in args.seeds.split(",")], args.duration,
                            args.attacks)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "report.json"), "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    with open(os.path.join(args.out, "report.md"), "w") as fh:
        fh.write(render_markdown(report))
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
