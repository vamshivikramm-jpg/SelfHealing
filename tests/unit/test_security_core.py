"""Unit tests for Module 5.3-5.6: detectors, trust, policy and enforcement."""

import math
import pathlib

import pytest

from network.config import load_topology_config
from network.security import enforcement
from network.security.detectors import (
    AuthFailureDetector,
    DetectorSuite,
    FingerprintDetector,
    FloodDetector,
    PolicyViolationDetector,
    PortScanDetector,
    VolumeDetector,
    normalized_entropy,
)
from network.security.enforcement import SimulatedEnforcer, compile_rules
from network.security.identity import IdentityRegistry
from network.security.policy import (
    ALLOW,
    DENY_IDENTITY,
    DENY_NO_RULE,
    DENY_QUARANTINE,
    DENY_TRUST,
    DENY_UNKNOWN,
    Decision,
    TrustSettings,
    ZeroTrustPolicy,
)
from network.security.records import Anomaly, AuthEvent, FlowRecord
from network.security.trust import TrustEngine, TrustLevel

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = ROOT / "network" / "config"
TOPOLOGY = load_topology_config(CONFIG / "enterprise.yaml")


@pytest.fixture(scope="module")
def registry():
    return IdentityRegistry.from_config(TOPOLOGY, CONFIG / "identities.yaml")


@pytest.fixture(scope="module")
def policy():
    return ZeroTrustPolicy.from_file(CONFIG / "zero_trust_policy.yaml")


def flow(registry, src, dst, port, ts=0.0, packets=10, claim=None):
    ident = registry.get(claim or src)
    return FlowRecord(ts, src, ident.ip, ident.mac, dst, port, packets=packets)


def decide(policy, registry, f, trust=0.9, quarantined=False):
    return policy.decide(registry, f.src, f.src_ip, f.src_mac, f.dst, f.dst_port, trust, quarantined)


# --------------------------------------------------------------------------
# 5.5 Policy
# --------------------------------------------------------------------------


def test_policy_file_loads(policy):
    assert [r.name for r in policy.rules] == ["web-access", "admin-ssh", "app-to-database"]
    assert policy.trust.quarantine_below == 0.3 and policy.trust.half_life_s == 300
    assert policy.anomaly_weights["fingerprint_mismatch"] == 0.5
    assert policy.to_dict()["default"] == "deny"


@pytest.mark.parametrize(
    "src,dst,port,trust,quarantined,code",
    [
        ("h2", "srv-web-1", 443, 0.8, False, ALLOW),
        ("h4", "srv-web-1", 443, 0.5, False, ALLOW),
        ("h2", "srv-db-1", 22, 0.8, False, DENY_NO_RULE),        # employees have no SSH
        ("h1", "srv-db-1", 22, 0.65, False, DENY_TRUST),         # admin SSH needs 0.7
        ("h1", "srv-db-1", 22, 0.9, False, ALLOW),
        ("srv-web-1", "srv-db-1", 5432, 0.9, False, ALLOW),
        ("h3", "srv-db-1", 5432, 0.8, False, DENY_NO_RULE),
        ("h2", "srv-web-1", 443, 0.2, True, DENY_QUARANTINE),
        ("h2", "gateway", 0, 0.2, True, ALLOW),
    ],
)
def test_policy_decisions(policy, registry, src, dst, port, trust, quarantined, code):
    d = policy.decide(registry, src, registry.get(src).ip, registry.get(src).mac, dst, port, trust, quarantined)
    assert d.code == code and d.allowed == (code == ALLOW) and d.reason


def test_identity_is_verified_before_anything_else(policy, registry):
    spoofed = flow(registry, "h4", "srv-db-1", 22, claim="h1")
    assert decide(policy, registry, spoofed).code == DENY_IDENTITY
    assert policy.decide(registry, "ghost", "1.1.1.1", "aa", "srv-web-1", 443, 1.0, False).code == DENY_UNKNOWN


def test_policy_validation(tmp_path):
    bad = tmp_path / "p.yaml"
    bad.write_text("default: allow\nrules: []\n")
    with pytest.raises(ValueError):
        ZeroTrustPolicy.from_file(bad)
    with pytest.raises(ValueError):
        TrustSettings(quarantine_below=0.6, release_above=0.5)


# --------------------------------------------------------------------------
# 5.4 Trust
# --------------------------------------------------------------------------


def test_trust_formula_decay_and_levels(registry, policy):
    engine = TrustEngine(registry, policy.trust, policy.anomaly_weights)
    assert engine.trust("h2", 0) == pytest.approx(0.8)
    assert engine.level("h2") == TrustLevel.TRUSTED
    assert engine.level("h4") == TrustLevel.RESTRICTED  # 0.6 - 0.1 posture
    engine.add([Anomaly(100.0, "h2", "port_scan", 1.0)])
    assert engine.trust("h2", 99.0) == pytest.approx(0.8)          # not yet happened
    assert engine.trust("h2", 100.0) == pytest.approx(0.4)          # 0.8 - 0.4
    assert engine.trust("h2", 400.0) == pytest.approx(0.6)          # one half-life later
    assert engine.kappa == pytest.approx(0.5 ** (1 / 300))
    changes = engine.update_levels(100.0)
    assert changes == [("h2", TrustLevel.TRUSTED, TrustLevel.RESTRICTED, pytest.approx(0.4))]


def test_quarantine_hysteresis(registry, policy):
    engine = TrustEngine(registry, policy.trust, policy.anomaly_weights)
    engine.add([Anomaly(0.0, "h2", "fingerprint_mismatch", 1.0)])  # 0.8 - 0.5 = 0.3 -> not below
    engine.add([Anomaly(0.0, "h2", "policy_violation", 1.0)])      # -0.15 -> 0.15
    engine.update_levels(0.0)
    assert engine.level("h2") == TrustLevel.QUARANTINED
    # penalty 0.65 decays; trust 0.4 (> quarantine_below) at t ~ 381 s but release needs 0.5
    t_04 = 300 * math.log2(0.65 / 0.4)
    engine.update_levels(t_04 + 1)
    assert engine.level("h2") == TrustLevel.QUARANTINED
    t_05 = 300 * math.log2(0.65 / 0.3)
    engine.update_levels(t_05 + 1)
    assert engine.level("h2") == TrustLevel.RESTRICTED
    snap = engine.snapshot(t_05 + 1)
    assert snap["h2"]["level"] == "RESTRICTED" and 0.5 < snap["h2"]["trust"] < 0.51


def test_trust_is_clamped_and_unknown_kinds_ignored(registry, policy):
    engine = TrustEngine(registry, policy.trust, policy.anomaly_weights)
    engine.add([Anomaly(0.0, "h2", "fingerprint_mismatch", 1.0)] * 5 + [Anomaly(0.0, "h3", "mystery", 1.0),
                                                                         Anomaly(0.0, "nobody", "flood", 1.0)])
    assert engine.trust("h2", 0.0) == 0.0
    assert engine.trust("h3", 0.0) == pytest.approx(0.8)


# --------------------------------------------------------------------------
# 5.3 Detectors
# --------------------------------------------------------------------------


def test_fingerprint_and_policy_violation_detectors(policy, registry):
    spoof = flow(registry, "h4", "srv-db-1", 22, claim="h1")
    wrong = flow(registry, "h2", "srv-db-1", 22)
    decided = [(spoof, decide(policy, registry, spoof)), (wrong, decide(policy, registry, wrong))]
    fp = FingerprintDetector().process(0.0, decided, [])
    assert [(a.subject, a.kind, a.magnitude) for a in fp] == [("h4", "fingerprint_mismatch", 1.0)]
    pv = PolicyViolationDetector().process(0.0, decided, [])
    assert [(a.subject, a.magnitude) for a in pv] == [("h2", 0.2)]


def test_auth_failure_window():
    det = AuthFailureDetector(window_s=60, threshold=5)
    fails = [AuthEvent(float(t), "h3", "srv-web-1", "x", False) for t in range(0, 40, 10)]
    assert det.process(40.0, [], fails) == []          # 4 failures
    out = det.process(45.0, [], [AuthEvent(45.0, "h3", "srv-web-1", "x", False)])
    assert out[0].subject == "h3" and out[0].magnitude == 0.5
    assert det.process(105.0, [], []) == []             # old failures left the window


def test_port_scan_detector(registry, policy):
    det = PortScanDetector(window_s=30, threshold=10)
    flows = [flow(registry, "h5", "srv-db-1", p, ts=0.0) for p in range(9)]
    assert det.process(0.0, [(f, None) for f in flows], []) == []
    more = [flow(registry, "h5", "srv-db-1", 100, ts=5.0)]
    assert det.process(5.0, [(f, None) for f in more], [])[0].subject == "h5"


def test_volume_detector_learns_normal_first(registry):
    det = VolumeDetector(warmup_steps=5, z_threshold=6, min_packets=2000)
    normal = [(flow(registry, "h2", "srv-web-1", 443, packets=50), None)]
    for i in range(10):
        assert det.process(float(i), normal, [], ["h2"]) == []
    burst = [(flow(registry, "h2", "srv-web-1", 443, packets=50000), None)]
    out = det.process(11.0, burst, [], ["h2"])
    assert out and out[0].kind == "volume_anomaly"
    assert det.process(12.0, normal, [], ["h2"]) == []  # baseline not polluted by the burst


def test_flood_detector_and_entropy(registry):
    assert normalized_entropy({"a": 1, "b": 1}) == pytest.approx(1.0)
    assert normalized_entropy({"a": 1}) == 1.0
    assert normalized_entropy({"a": 99, "b": 1}) < 0.2
    det = FloodDetector(warmup_steps=3)
    spread = [(flow(registry, s, "srv-web-1", 443, packets=30), None) for s in ("h1", "h2", "h3", "h4")]
    for i in range(5):
        assert det.process(float(i), spread, []) == []
    flood = spread + [(flow(registry, "h5", "srv-web-1", 80, packets=80000), None)]
    out = det.process(6.0, flood, [])
    assert out[0].subject == "h5" and out[0].kind == "flood"


def test_detector_suite_threat_index(registry, policy):
    suite = DetectorSuite(registry.names())
    spoof = flow(registry, "h4", "srv-db-1", 22, claim="h1")
    anomalies, threat = suite.process(0.0, [(spoof, decide(policy, registry, spoof))],
                                      [AuthEvent(0.0, "h4", "srv-db-1", "root", False)])
    assert {a.kind for a in anomalies} == {"fingerprint_mismatch"}
    assert threat.r_auth == 1.0 and threat.f_anom == pytest.approx(1 / 7)
    assert 0 <= threat.value <= 1 and set(threat.to_dict()) == {"h_flow", "r_auth", "f_anom", "value"}


# --------------------------------------------------------------------------
# 5.6 Enforcement
# --------------------------------------------------------------------------


def test_compiled_rules_match_policy_decisions(policy, registry):
    trust = {n: 0.9 for n in registry.names()}
    trust["h4"] = trust["h5"] = 0.5
    trust["h1"] = 0.65   # admin below the SSH threshold
    enforcer = SimulatedEnforcer()
    enforcer.apply(compile_rules(policy, registry, trust, quarantined={"h3"}))
    cases = [
        flow(registry, "h2", "srv-web-1", 443), flow(registry, "h1", "srv-db-1", 22),
        flow(registry, "h1", "srv-web-1", 443), flow(registry, "h4", "srv-web-1", 80),
        flow(registry, "h3", "srv-web-1", 443), flow(registry, "h3", "gateway", 0),
        flow(registry, "srv-web-1", "srv-db-1", 5432), flow(registry, "h2", "srv-db-1", 5432),
        flow(registry, "h4", "srv-db-1", 22, claim="h1"),
    ]
    for f in cases:
        expected = policy.decide(registry, f.src, f.src_ip, f.src_mac, f.dst, f.dst_port,
                                 trust[f.src], f.src == "h3").allowed
        assert enforcer.admit(f)[0] == expected, f
    assert enforcer.blocked == 4 and enforcer.admitted == 5


def test_apply_reports_changes(policy, registry):
    trust = {n: 0.9 for n in registry.names()}
    enforcer = SimulatedEnforcer()
    first = enforcer.apply(compile_rules(policy, registry, trust, set()))
    assert first["removed"] == [] and len(first["added"]) == len(enforcer.rules)
    diff = enforcer.apply(compile_rules(policy, registry, trust, {"h2"}))
    assert {r.reason for r in diff["added"]} == {"h2 is quarantined", "h2 may reach its gateway"}
    assert all(r.subject == "h2" for r in diff["removed"])


def test_openflow_rendering(policy, registry):
    rules = compile_rules(policy, registry, {n: 0.9 for n in registry.names()}, {"h2"})
    bind = next(r for r in rules if r.table == 0 and r.subject == "h1").as_openflow(registry)
    assert bind["match"] == {"eth_type": 0x0800, "ipv4_src": "10.0.1.10", "eth_src": "02:00:0a:00:01:0a"}
    assert bind["actions"] == [{"type": "GOTO_TABLE", "table_id": 1}]
    ssh = next(r for r in rules if r.table == 1 and r.match_dict().get("port") == 22).as_openflow(registry)
    assert ssh["match"]["tcp_dst"] == 22 and ssh["actions"] == [{"type": "OUTPUT", "port": "NORMAL"}]
    gw = next(r for r in rules if r.match_dict().get("dst") == "gateway").as_openflow(registry)
    assert gw["match"]["ipv4_dst"] == "10.0.1.1"
    drop = next(r for r in rules if r.reason == "h2 is quarantined").as_openflow(registry)
    assert drop["actions"] == [] and drop["priority"] == 300
