"""Unit tests for Module 7.2-7.6: delivery model, executor, verification,
undo/retry/escalation and the closed loop (redundant simulation topology)."""

import pathlib

import pytest

from network.analytics.scenarios import PlannedFailure, ScenarioPlan, ScenarioRunner
from network.config import load_topology_config
from network.decision.graph import NetworkGraph
from network.decision.policy import DecisionPolicy
from network.healing.engine import ESCALATED, FAILED, HealingEngine, HealingPolicy
from network.healing.executor import ExecutionResult
from network.healing.loop import ClosedLoop
from network.healing.service import ServiceModel
from network.security.identity import IdentityRegistry
from network.security.policy import ZeroTrustPolicy
from network.security.traffic import AttackSpec, SecurityPlan

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = ROOT / "network" / "config"
TOPO = load_topology_config(CONFIG / "enterprise-redundant.yaml")
REGISTRY = IdentityRegistry.from_config(TOPO, CONFIG / "identities.yaml")
ZT = ZeroTrustPolicy.from_file(CONFIG / "zero_trust_policy.yaml")
DP = DecisionPolicy.from_file(CONFIG / "decision_policy.yaml")


def run(failures, attacks=(), duration=600, policy=HealingPolicy(), heal=True):
    plan = ScenarioPlan(duration_s=duration, step_s=5, failures=tuple(failures), seed=2)
    runner = ScenarioRunner(plan, config=TOPO)
    sec = SecurityPlan(duration_s=duration, step_s=5, attacks=tuple(attacks), seed=2, mistake_rate=0.0,
                       login_failure_rate=0.0)
    loop = ClosedLoop(runner, REGISTRY, ZT, DP, TOPO, sec, healing_policy=policy, heal=heal)
    result = runner.run()
    return result, loop


def actions(loop, kind=None):
    return [a for a in loop.healing.history() if kind is None or a["type"] == kind]


# --------------------------------------------------------------------------
# 7.2 Delivery model
# --------------------------------------------------------------------------


class Net:
    def __init__(self, down_links=(), down_nodes=()):
        self.down_links = {tuple(sorted(l)) for l in down_links}
        self.down_nodes = set(down_nodes)

    def node_physically_up(self, n):
        return n not in self.down_nodes

    def link_physically_up(self, a, b):
        return tuple(sorted((a, b))) not in self.down_links


def test_service_model_delivery_and_restore_time():
    net = Net()
    svc = ServiceModel(NetworkGraph.from_topology(TOPO), lambda: net)
    pair = ("h4", "srv-web-1")
    assert svc.delivered(pair) == 1.0
    assert svc.demands_using(link="sw-a-sw-b") == sorted(p for p, path in svc.baseline.items()
                                                         if "sw-b" in path and "sw-a" in path)
    svc.step(0.0)
    net.down_links.add(("sw-a", "sw-b"))
    svc.step(5.0)
    assert svc.latest(pair) == 0.0
    svc.routes[pair] = ("h4", "sw-b", "rtr-core", "sw-a", "srv-web-1")
    svc.step(10.0)
    svc.step(15.0)
    assert svc.latest(pair) == 1.0
    assert svc.lost_fraction([pair], 0, 15) == pytest.approx(0.25)
    assert svc.restored_at([pair], 5.0, 15.0) == 10.0
    net.down_nodes.add("rtr-core")
    assert svc.delivered(pair) == 0.0


# --------------------------------------------------------------------------
# 7.3-7.6 Closed loop
# --------------------------------------------------------------------------


def test_software_switch_failure_is_restored_and_verified():
    result, loop = run([PlannedFailure("switch", ("sw-b",), 100, 200)])
    restore = actions(loop, "RESTORE")
    assert len(restore) == 1 and restore[0]["state"] in ("VERIFIED", "FAILED_BACK")
    assert restore[0]["verified_at"] is not None
    t0 = loop.start
    healed_at = restore[0]["verified_at"] - t0
    assert 100 <= healed_at <= 125            # long before the planned end at 300 s
    pairs = loop.service.demands_using(node="sw-b")
    assert loop.service.restored_at(pairs, t0 + 100, t0 + 300) <= t0 + 115
    assert loop.healing.stats()["success_rate"] == 1.0


def test_physical_core_cut_restore_escalates_reroute_keeps_traffic_flowing():
    result, loop = run([PlannedFailure("link", ("sw-a", "sw-b"), 100, 200, physical=True)])
    restore = actions(loop, "RESTORE")[0]
    reroute = actions(loop, "REROUTE")[0]
    assert restore["state"] == ESCALATED and restore["attempts"] == 2
    assert "status 0.0" in restore["checks"][-1]          # telemetry, not the command, said no
    assert reroute["verified_at"] is not None
    t0 = loop.start
    pairs = loop.service.demands_using(link="sw-a-sw-b")
    assert loop.service.restored_at(pairs, t0 + 100, t0 + 290) <= t0 + 110
    assert loop.service.lost_fraction(pairs, t0 + 100, t0 + 300) < 0.1
    # after the technician repairs the cable, traffic fails back to the direct link
    assert loop.service.routes == loop.service.baseline
    stats = loop.healing.stats()
    assert stats["escalated"] == 1 and stats["rollbacks"] == 0


def test_without_verification_a_physical_restore_is_falsely_claimed():
    _, loop = run([PlannedFailure("link", ("sw-a", "h1"), 100, 200, physical=True)],
                  policy=HealingPolicy(verify=False))
    restore = actions(loop, "RESTORE")[0]
    assert restore["verified_at"] is not None and "claimed" in restore["detail"]
    t0 = loop.start
    assert loop.service.lost_fraction([("h1", "srv-web-1")], t0 + 110, t0 + 290) == 1.0  # nothing works


def test_no_healing_baseline_waits_for_manual_repair():
    _, loop = run([PlannedFailure("switch", ("sw-b",), 100, 200)], heal=False)
    t0 = loop.start
    pairs = loop.service.demands_using(node="sw-b")
    assert loop.service.restored_at(pairs, t0 + 100, t0 + 400) >= t0 + 300


def test_attack_is_blocked_and_verified():
    attack = AttackSpec("flood", "h5", 200, 60, "srv-web-1", attack_id="flood-1")
    _, loop = run([], attacks=[attack])
    security = [a for a in loop.healing.history() if a["type"] in ("QUARANTINE", "BLOCK", "ISOLATE")]
    assert security and all(a["target"] == "node:h5" for a in security)
    assert any(a["verified_at"] is not None for a in security)
    assert "dropped" in next(a for a in security if a["verified_at"])["checks"][-1]


# --------------------------------------------------------------------------
# Undo and retry (stubbed decisions / executor / verifier)
# --------------------------------------------------------------------------


class StubDecisions:
    def __init__(self, intents):
        self.intents = intents

    def active_intents(self):
        return list(self.intents)


class StubExecutor:
    def __init__(self, ok=True):
        self.ok, self.executed, self.undone = ok, 0, 0

    def execute(self, intent):
        self.executed += 1
        return ExecutionResult(self.ok, "stub", {"routes": {"x": 1}} if self.ok else {})

    def undo(self, intent, undo):
        self.undone += 1
        return "undone"


class StubVerifier:
    def __init__(self, ok):
        self.ok = ok

    def check(self, post, ts):
        return self.ok, "stub"


def _intent(kind="REROUTE"):
    return {"id": "i1", "type": kind, "target": "link:x", "status": "APPROVED", "reason": "",
            "postconditions": [{"check": "link_up", "link": "x"}], "params": {}}


def test_failed_verification_rolls_back_retries_then_fails():
    ex = StubExecutor()
    eng = HealingEngine(StubDecisions([_intent()]), ex, StubVerifier(False),
                        HealingPolicy(verify_collections=2, max_attempts=2, retry_backoff_s=10))
    for ts in range(0, 60, 5):
        eng.step(float(ts))
    a = eng.history()[0]
    assert a["state"] == FAILED and a["attempts"] == 2
    assert ex.executed == 2 and ex.undone == 2
    s = eng.stats()
    assert s["executed"] == 2 and s["rollbacks"] == 2
    assert s["success_rate"] == 0.0 and s["rollback_rate"] == 1.0


def test_confirmation_needs_consecutive_passes_and_fail_back():
    decisions = StubDecisions([_intent()])
    ex = StubExecutor()
    eng = HealingEngine(decisions, ex, StubVerifier(True), HealingPolicy(confirmations=2))
    eng.step(0.0)
    eng.step(5.0)
    assert eng.history()[0]["state"] == "VERIFYING"
    eng.step(10.0)
    assert eng.history()[0]["state"] == "VERIFIED"
    decisions.intents = []          # problem resolved -> routes fail back
    eng.step(15.0)
    assert eng.history()[0]["state"] == "FAILED_BACK" and ex.undone == 1


def test_execution_error_and_pending_and_monitor_intents():
    pending = dict(_intent(), id="i2", status="PENDING_APPROVAL")
    monitor = dict(_intent("MONITOR"), id="i3")
    ex = StubExecutor(ok=False)
    eng = HealingEngine(StubDecisions([_intent("RESTORE"), pending, monitor]), ex, StubVerifier(True),
                        HealingPolicy(max_attempts=1))
    eng.step(0.0)
    hist = eng.history()
    assert [h["intent_id"] for h in hist] == ["i1"]       # pending and monitor are not executed
    assert hist[0]["state"] == ESCALATED                  # a RESTORE that cannot run goes to a person


# --------------------------------------------------------------------------
# 7.7 Evaluation (small, no predictor)
# --------------------------------------------------------------------------


def test_evaluation_modes_compare_as_expected():
    from network.healing.evaluation import render_markdown, run_evaluation

    report = run_evaluation(seeds=(2,), duration_s=7200, use_predictor=False, n_failures=8, n_attacks=2)
    s = report["summary"]
    assert set(s) == {"no_healing", "restore_only", "full", "full_no_prediction", "full_no_verify",
                      "random_reroute", "full_no_trust"}
    assert s["no_healing"]["healed_before_manual_repair_pct"] == 0.0
    assert s["full"]["mttr_s"] < s["no_healing"]["mttr_s"]
    assert s["full"]["user_loss_pct"] <= s["restore_only"]["user_loss_pct"] < s["no_healing"]["user_loss_pct"]
    assert s["full"]["false_success_claims"] == 0
    assert s["full_no_verify"]["success_rate"] == 1.0                  # claims everything worked...
    assert s["full_no_verify"]["false_success_claims"] == s["full"]["escalated"]  # ...including physical faults
    assert s["full_no_verify"]["escalated"] == 0
    assert s["restore_only"]["attacks_contained_pct"] == 0.0
    text = render_markdown(report)
    assert "| full |" in text and "SIMULATED" in text
