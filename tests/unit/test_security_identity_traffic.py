"""Unit tests for Module 5.1 (identities) and 5.2 (traffic and attacks)."""

import pathlib

import pytest

from network.config import load_topology_config
from network.security.identity import DeviceIdentity, IdentityError, IdentityRegistry
from network.security.records import MISTAKE
from network.security.traffic import (
    ATTACK_KINDS,
    AttackSpec,
    SecurityPlan,
    TrafficGenerator,
    generate_security_plan,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOPOLOGY = load_topology_config(ROOT / "network" / "config" / "enterprise.yaml")
IDENTITIES = ROOT / "network" / "config" / "identities.yaml"


@pytest.fixture(scope="module")
def registry():
    return IdentityRegistry.from_config(TOPOLOGY, IDENTITIES)


# --------------------------------------------------------------------------
# 5.1 Identities
# --------------------------------------------------------------------------


def test_registry_covers_hosts_and_servers(registry):
    assert registry.names() == ["h1", "h2", "h3", "h4", "h5", "srv-db-1", "srv-web-1"]
    h1 = registry.get("h1")
    assert (h1.role, h1.ip, h1.subnet, h1.gateway) == ("admin", "10.0.1.10", "subnet-a", "10.0.1.1")
    assert registry.by_ip("10.0.1.21").name == "srv-db-1"
    assert registry.by_mac("02:00:0A:00:01:0A").name == "h1"
    assert [i.name for i in registry.with_role("contractor")] == ["h4", "h5"]
    assert registry.get("sw-a") is None  # infrastructure is not a subject


def test_effective_base_trust_applies_posture(registry):
    assert registry.get("h1").effective_base_trust(0.1) == pytest.approx(0.9)
    assert registry.get("h4").effective_base_trust(0.1) == pytest.approx(0.5)  # unmanaged


def test_verify_identity_claims(registry):
    h1 = registry.get("h1")
    assert registry.verify("h1", h1.ip, h1.mac.upper()) is None
    assert "IP 10.0.1.10 is not h4's" in registry.verify("h4", h1.ip, registry.get("h4").mac)
    assert "MAC" in registry.verify("h4", registry.get("h4").ip, h1.mac)
    assert registry.verify("ghost", "1.2.3.4", "00:00:00:00:00:00") == "unknown device 'ghost'"


def test_identity_file_must_match_topology(tmp_path):
    bad = tmp_path / "ids.yaml"
    bad.write_text("devices:\n  h1: {role: admin, owner: x, mac: '02:00:00:00:00:01', base_trust: 0.9}\n")
    with pytest.raises(IdentityError, match="missing"):
        IdentityRegistry.from_config(TOPOLOGY, bad)


def test_duplicate_attributes_rejected():
    a = DeviceIdentity("a", "host", "r", "o", "10.0.0.1", "02:00:00:00:00:01", "s", "g", 0.5)
    b = DeviceIdentity("b", "host", "r", "o", "10.0.0.1", "02:00:00:00:00:02", "s", "g", 0.5)
    with pytest.raises(IdentityError):
        IdentityRegistry([a, b])


# --------------------------------------------------------------------------
# 5.2 Traffic and attacks
# --------------------------------------------------------------------------


def test_attack_spec_validation():
    with pytest.raises(ValueError):
        AttackSpec("teleport", "h2", 0, 10, "srv-web-1")
    with pytest.raises(ValueError):
        AttackSpec("spoofing", "h2", 0, 10, "srv-db-1")  # no victim


def test_generate_plan_reproducible_and_sequential(registry):
    a = generate_security_plan(registry, 10800, seed=3, n_attacks=8)
    assert a == generate_security_plan(registry, 10800, seed=3, n_attacks=8)
    assert a.attacks[0].start_s == 600
    for prev, nxt in zip(a.attacks, a.attacks[1:]):
        assert nxt.start_s >= prev.end_s + 300
    assert all(x.attacker not in ("h1", "srv-web-1", "srv-db-1") for x in a.attacks)
    spoof = [x for x in generate_security_plan(registry, 20000, seed=1, n_attacks=12).attacks
             if x.kind == "spoofing"]
    assert all(x.victim == "h1" and x.target == "srv-db-1" for x in spoof)
    with pytest.raises(ValueError):
        generate_security_plan(registry, 1000, seed=1, n_attacks=10)


def test_benign_traffic_follows_role_profiles(registry):
    gen = TrafficGenerator(registry, SecurityPlan(duration_s=3600, seed=1, mistake_rate=0.0))
    flows = []
    for i in range(200):
        f, _ = gen.step(i * 5.0, 1000.0 + i * 5)
        flows.extend(f)
    pairs = {(f.src, f.dst, f.dst_port) for f in flows}
    assert ("srv-web-1", "srv-db-1", 5432) in pairs
    assert all(f.dst_port in (80, 443) for f in flows if f.src in ("h2", "h3", "h4", "h5"))
    assert not any(f.src == "srv-db-1" for f in flows)
    assert all(f.label == "benign" and f.src_ip == registry.get(f.src).ip for f in flows)


def test_benign_mistakes_are_labelled(registry):
    gen = TrafficGenerator(registry, SecurityPlan(duration_s=3600, seed=2, mistake_rate=1.0))
    flows, _ = gen.step(0.0, 0.0)
    mistakes = [f for f in flows if f.label == MISTAKE]
    assert {f.src for f in mistakes} == {"h1", "h2", "h3", "h4", "h5"}
    assert not any(f.is_attack for f in mistakes)


@pytest.mark.parametrize("kind", ATTACK_KINDS)
def test_attack_traffic(registry, kind):
    victim = "h1" if kind == "spoofing" else None
    target = "srv-db-1" if kind != "flood" else "srv-web-1"
    attack = AttackSpec(kind, "h4", 0, 60, target, victim, f"{kind}-1")
    gen = TrafficGenerator(registry, SecurityPlan(duration_s=600, seed=1, attacks=(attack,)))
    flows, auth = gen.step(10.0, 10.0)
    bad = [f for f in flows if f.attack_id == f"{kind}-1"]
    assert bad and all(f.is_attack and f.src == "h4" for f in bad)
    if kind == "port_scan":
        assert len({f.dst_port for f in bad}) == 25
    if kind == "brute_force":
        assert len([a for a in auth if not a.success and a.attack_id]) == 8
    if kind == "spoofing":
        assert all(f.src_ip == registry.get("h1").ip and f.src_mac == registry.get("h1").mac for f in bad)
    if kind == "flood":
        assert sum(f.packets for f in bad) == 80000
    assert not [f for f in gen.step(70.0, 70.0)[0] if f.attack_id]  # attack over
