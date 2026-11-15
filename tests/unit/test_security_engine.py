"""Unit tests for Module 5.7 — Zero Trust engine and evaluation."""

import json
import pathlib

import pytest

from network.config import load_topology_config
from network.events import EventBus, EventType
from network.security.engine import ZeroTrustEngine
from network.security.evaluation import (
    aggregate,
    auroc,
    evaluate_run,
    main as evaluation_main,
    render_markdown,
    run_evaluation,
    run_scenario,
)
from network.security.identity import IdentityRegistry
from network.security.policy import DENY_IDENTITY, ZeroTrustPolicy
from network.security.records import FlowRecord
from network.security.traffic import AttackSpec, SecurityPlan
from network.security.trust import TrustLevel

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = ROOT / "network" / "config"
TOPOLOGY = load_topology_config(CONFIG / "enterprise.yaml")


@pytest.fixture(scope="module")
def registry():
    return IdentityRegistry.from_config(TOPOLOGY, CONFIG / "identities.yaml")


@pytest.fixture(scope="module")
def policy():
    return ZeroTrustPolicy.from_file(CONFIG / "zero_trust_policy.yaml")


def test_auroc():
    assert auroc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]) == 0.75
    assert auroc([1, 1, 1, 1], [0, 1, 0, 1]) == 0.5   # ties count half
    assert auroc([0.2, 0.3], [0, 0]) is None


def test_engine_blocks_spoofing_and_quarantines(registry, policy):
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    engine = ZeroTrustEngine(registry, policy, bus)
    h1, h4 = registry.get("h1"), registry.get("h4")
    spoof = FlowRecord(0.0, "h4", h1.ip, h1.mac, "srv-db-1", 22, packets=10, label="spoofing")
    result = engine.step(0.0, [spoof], [])
    flow, decision, enforced = result.decided[0]
    assert decision.code == DENY_IDENTITY and enforced is False
    assert result.mismatches == 0
    assert [a.kind for a in result.anomalies] == ["fingerprint_mismatch"]
    types = [e.type for e in events]
    assert EventType.SECURITY_ANOMALY in types and EventType.ACCESS_DENIED in types
    assert EventType.DEVICE_QUARANTINED in types  # h4 base 0.5 - 0.5 = 0
    assert engine.trust.level("h4") == TrustLevel.QUARANTINED
    # next step: h4's legitimate web traffic is now blocked too
    web = FlowRecord(5.0, "h4", h4.ip, h4.mac, "srv-web-1", 443)
    assert engine.step(5.0, [web], []).decided[0][2] is False
    status = engine.status()
    assert status["quarantined"] == ["h4"] and status["enforcement_mismatches"] == 0
    assert status["trust"]["h4"]["level"] == "QUARANTINED"
    recent = engine.recent_events(5)
    assert recent and recent[0]["type"] in {e.value for e in EventType}


def test_engine_releases_after_good_behaviour(registry, policy):
    engine = ZeroTrustEngine(registry, policy, EventBus())
    h1 = registry.get("h1")
    engine.step(0.0, [FlowRecord(0.0, "h4", h1.ip, h1.mac, "srv-db-1", 22)], [])
    assert "h4" in engine.quarantined()
    released = None
    for i in range(1, 400):
        result = engine.step(i * 5.0, [], [])
        if any(c[0] == "h4" and c[2] != TrustLevel.QUARANTINED for c in result.level_changes):
            released = i * 5.0
            break
    # h4's base is 0.5, so it is released at 0.45 (base - margin), not 0.5
    assert engine.trust.release_level("h4") == pytest.approx(0.45)
    assert released is not None and released > 900  # penalty 0.5 must decay to 0.05
    assert engine.trust.trust("h4", released) >= 0.45


def _plan(kind, attacker="h4", victim=None, target="srv-db-1"):
    attack = AttackSpec(kind, attacker, 900, 120, target, victim, f"{kind}-1")
    return SecurityPlan(duration_s=1800, step_s=5, attacks=(attack,), seed=4)


@pytest.mark.parametrize("kind,victim,target", [
    ("port_scan", None, "srv-db-1"), ("brute_force", None, "srv-db-1"), ("spoofing", "h1", "srv-db-1"),
    ("lateral_movement", None, "srv-db-1"), ("flood", None, "srv-web-1"),
])
def test_each_attack_is_detected_and_contained(registry, policy, kind, victim, target):
    result = run_scenario(_plan(kind, victim=victim, target=target), registry, policy)
    report = evaluate_run(result, registry, policy)
    attack = report["attacks"][0]
    assert attack["detected"] and attack["time_to_detect_s"] <= 10
    assert attack["blocked_pct"] >= 95
    assert report["frr"] == 0.0
    assert report["benign_devices_quarantined"] == []
    assert report["enforcement_mismatches"] == 0
    assert report["detection_auroc"] > 0.8


def test_run_evaluation_and_report(registry, policy):
    report = run_evaluation(registry, policy, seeds=(1, 2), duration_s=5400, n_attacks=4)
    assert report["simulated"] and report["synthetic_attacks"]
    s = report["summary"]
    assert s["runs"] == 2 and s["enforcement_mismatches"] == 0
    assert s["frr"] == 0.0 and s["detection_auroc"] > 0.85
    assert all(k["detected_pct"] == 100.0 for k in s["by_attack_kind"].values())
    text = render_markdown(report)
    assert "SYNTHETIC attacks" in text and "| Detection AUROC" in text
    assert aggregate(report["runs"])["far"] == s["far"]


def test_evaluation_cli(tmp_path, capsys):
    assert evaluation_main(["--out", str(tmp_path), "--seeds", "3", "--duration", "3600", "--attacks", "2"]) == 0
    saved = json.loads((tmp_path / "report.json").read_text())
    assert saved["summary"]["runs"] == 1
    assert (tmp_path / "report.md").exists()
    capsys.readouterr()
