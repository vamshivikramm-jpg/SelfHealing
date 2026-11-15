"""Unit tests for Module 6.1-6.6: policy, graph/paths, risk, strategies,
safety checks and the decision engine (redundant simulation topology)."""

import dataclasses
import pathlib

import pytest

from network.config import load_topology_config
from network.decision.engine import DecisionEngine
from network.decision.graph import EdgeState, NetworkGraph, link_name, path_links
from network.decision.intents import Intent, IntentStatus, IntentType
from network.decision.policy import DecisionPolicy, RiskWeights
from network.decision.risk import Signals, assess
from network.decision.safety import SafetyGuard, resolve_conflicts
from network.events import EventBus, EventType

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = ROOT / "network" / "config"
REDUNDANT = load_topology_config(CONFIG / "enterprise-redundant.yaml")
TREE = load_topology_config(CONFIG / "enterprise.yaml")
POLICY = DecisionPolicy.from_file(CONFIG / "decision_policy.yaml")


def healthy(graph, overrides=None):
    health = {f"link:{n}": {"status": "HEALTHY", "reasons": [], "metrics": {"link.utilization_pct": 5.0}}
              for n in graph.edges}
    health.update({f"node:{n}": {"status": "HEALTHY", "reasons": [], "metrics": {}} for n in graph.kinds})
    health.update(overrides or {})
    return health


def trust_all(level="TRUSTED", value=0.9, **over):
    names = ["h1", "h2", "h3", "h4", "h5", "srv-web-1", "srv-db-1"]
    out = {n: {"trust": value, "level": level} for n in names}
    out.update(over)
    return out


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


def test_policy_file():
    assert POLICY.autonomy == "L2" and POLICY.act_above == 0.15
    assert POLICY.weights == RiskWeights(0.3, 0.15, 0.15, 0.4)
    assert POLICY.to_dict()["weights"]["fault"] == 0.4
    with pytest.raises(ValueError):
        RiskWeights(0.5, 0.5, 0.5, 0.5)
    with pytest.raises(ValueError):
        DecisionPolicy(autonomy="L9")


def test_redundant_topology_only_adds_core_link():
    assert len(REDUNDANT.links) == len(TREE.links) + 1
    assert {d.name for d in REDUNDANT.devices} == {d.name for d in TREE.devices}


# --------------------------------------------------------------------------
# 6.4 Graph and paths
# --------------------------------------------------------------------------


def test_baseline_routes_and_demands():
    g = NetworkGraph.from_topology(REDUNDANT)
    assert ("h4", "srv-web-1") in g.demands() and ("srv-db-1", "srv-web-1") in g.demands()
    routes = g.baseline_routes()
    assert routes[("h4", "srv-web-1")] == ("h4", "sw-b", "sw-a", "srv-web-1")  # direct core link
    assert routes[("h1", "srv-web-1")] == ("h1", "sw-a", "srv-web-1")
    assert path_links(("h4", "sw-b", "sw-a")) == ["h4-sw-b", "sw-a-sw-b"]
    assert link_name("sw-b", "sw-a") == "sw-a-sw-b"


def test_shortest_path_avoids_failed_links_and_never_transits_hosts():
    g = NetworkGraph.from_topology(REDUNDANT)
    path, _ = g.shortest_path("h4", "srv-web-1", avoid_links={"sw-a-sw-b"})
    assert path == ("h4", "sw-b", "rtr-core", "sw-a", "srv-web-1")
    g.edges["sw-a-sw-b"].up = False
    g.edges["rtr-core-sw-b"].up = False
    assert g.shortest_path("h4", "srv-web-1") is None
    tree = NetworkGraph.from_topology(TREE)
    assert tree.shortest_path("h1", "srv-db-1", avoid_links={"h1-sw-a"}) is None


def test_edge_cost_eq6_prefers_reliable_low_latency_paths():
    g = NetworkGraph.from_topology(REDUNDANT)
    g.edges["sw-a-sw-b"].latency_ms = 100.0   # 10 cost units
    path, cost = g.shortest_path("h4", "srv-web-1")
    assert "rtr-core" in path
    g.edges["sw-a-sw-b"].latency_ms = 0.0
    g.edges["sw-a-sw-b"].p_failure = 0.9      # 9 cost units
    assert "rtr-core" in g.shortest_path("h4", "srv-web-1")[0]


def test_capacity_constraint():
    g = NetworkGraph.from_topology(REDUNDANT)
    g.edges["sw-a-sw-b"].utilization_pct = 99.5
    assert "rtr-core" in g.shortest_path("h4", "srv-web-1", min_free_mbps=10)[0]


def test_apply_signals_marks_down_links_and_nodes():
    g = NetworkGraph.from_topology(REDUNDANT)
    health = healthy(g, {"node:rtr-core": {"status": "DOWN", "metrics": {}},
                         "link:h1-sw-a": {"status": "DEGRADED", "metrics": {"link.latency_ms": 120.0}}})
    g.apply_signals(health, {"h2-sw-a": 0.7})
    assert not g.edges["rtr-core-sw-a"].up and g.edges["h1-sw-a"].latency_ms == 120.0
    assert g.edges["h2-sw-a"].p_failure == 0.7


# --------------------------------------------------------------------------
# 6.1 Risk
# --------------------------------------------------------------------------


def test_risk_eq4_components():
    g = NetworkGraph.from_topology(REDUNDANT)
    sig = Signals(ts=0.0, health=healthy(g, {"link:h1-sw-a": {"status": "DOWN", "metrics": {}}}),
                  predictions={"h2-sw-a": 0.6}, trust=trust_all(h3={"trust": 0.2, "level": "QUARANTINED"}),
                  threat=0.4)
    r = assess(g, sig, POLICY)
    down = r["link:h1-sw-a"]
    assert down.components["fault"] == 1.0
    assert down.risk == pytest.approx(0.4 + 0.15 * 0.1 + 0.15 * 0.4)
    assert r["link:h2-sw-a"].risk == pytest.approx(0.3 * 0.6 + 0.15 * 0.1 + 0.06)
    assert r["node:h3"].components["distrust"] == pytest.approx(0.8)
    assert r["node:sw-a"].components["prediction"] == 0.6   # node takes its worst link
    assert r["link:rtr-core-sw-a"].components["distrust"] == 0.0  # no host involved


# --------------------------------------------------------------------------
# 6.3 + 6.5 Strategies and safety via the engine
# --------------------------------------------------------------------------


def engine(policy=POLICY, topology=REDUNDANT, bus=None):
    return DecisionEngine(topology, policy, bus)


def step(e, ts=0.0, health_over=None, **kw):
    sig = Signals(ts=ts, health=healthy(e.base_graph, health_over), trust=kw.pop("trust", trust_all()), **kw)
    return e.step(sig)


def types(result):
    return sorted((i.type.value, i.target) for i in result.new)


def test_healthy_network_needs_no_action():
    e = engine()
    assert step(e).new == []


def test_core_link_down_restore_and_reroute():
    e = engine()
    r = step(e, health_over={"link:sw-a-sw-b": {"status": "DOWN", "metrics": {}}})
    assert types(r) == [("REROUTE", "link:sw-a-sw-b"), ("RESTORE", "link:sw-a-sw-b")]
    reroute = next(i for i in r.new if i.type == IntentType.REROUTE)
    assert all("rtr-core" in m["new"] for m in reroute.params["moves"])
    assert {"check": "route_avoids", "src": "h4", "dst": "srv-web-1", "link": "sw-a-sw-b"} in reroute.postconditions
    assert all(i.status == IntentStatus.APPROVED for i in r.new)
    assert e.routes[("h4", "srv-web-1")] == ("h4", "sw-b", "rtr-core", "sw-a", "srv-web-1")
    # link recovers -> both intents resolve and traffic fails back
    r2 = step(e, ts=5.0)
    assert {i.type for i in r2.resolved} == {IntentType.RESTORE, IntentType.REROUTE}
    assert e.routes == e.baseline


def test_access_link_down_can_only_be_restored():
    r = step(engine(), health_over={"link:h1-sw-a": {"status": "DOWN", "metrics": {}}})
    assert types(r) == [("RESTORE", "link:h1-sw-a")]


def test_switch_down_on_tree_topology():
    r = step(engine(topology=TREE), health_over={"node:rtr-core": {"status": "DOWN", "metrics": {}}})
    assert types(r) == [("RESTORE", "node:rtr-core")]  # no alternative path in a tree


def test_router_down_reroutes_on_redundant_topology():
    e = engine()
    e.baseline[("h4", "srv-web-1")] = ("h4", "sw-b", "rtr-core", "sw-a", "srv-web-1")  # traffic via router
    r = step(e, health_over={"node:rtr-core": {"status": "DOWN", "metrics": {}}})
    assert ("REROUTE", "node:rtr-core") in types(r) and ("RESTORE", "node:rtr-core") in types(r)


def test_degraded_core_link_reroutes_degraded_access_link_monitors():
    e = engine()
    lat = {"status": "DEGRADED", "reasons": ["latency 120.00 ms > 50 ms"], "metrics": {"link.latency_ms": 120.0}}
    r = step(e, health_over={"link:sw-a-sw-b": lat, "link:h2-sw-a": dict(lat)})
    assert ("REROUTE", "link:sw-a-sw-b") in types(r)
    monitor = next(i for i in r.new if i.target == "link:h2-sw-a")
    assert monitor.type == IntentType.MONITOR and "no better path" in monitor.reason


def test_congested_core_link_load_balances():
    e = engine()
    hot = {"status": "DEGRADED", "reasons": ["utilization 95.00 % > 85 %"],
           "metrics": {"link.utilization_pct": 95.0, "link.capacity_mbps": 100.0}}
    r = step(e, health_over={"link:sw-a-sw-b": hot})
    lb = next(i for i in r.new if i.type == IntentType.LOAD_BALANCE)
    assert lb.params["moves"] and lb.params["projected_utilization_pct"] < 95.0
    assert lb.postconditions == [{"check": "utilization_below", "link": "sw-a-sw-b", "pct": 70.0}]


def test_predicted_failure_proactive_reroute_only_above_threshold():
    e = engine()
    assert step(e, predictions={"sw-a-sw-b": 0.4}).new == []
    r = step(e, ts=5.0, predictions={"sw-a-sw-b": 0.8})
    assert types(r) == [("REROUTE", "link:sw-a-sw-b")] and r.new[0].params["proactive"]
    r2 = step(e, ts=10.0, predictions={"h3-sw-a": 0.8})
    assert ("MONITOR", "link:h3-sw-a") in types(r2)


def test_security_intents_and_human_gate():
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append)
    e = engine(bus=bus)
    r = step(e, trust=trust_all(h4={"trust": 0.1, "level": "QUARANTINED"},
                                **{"srv-web-1": {"trust": 0.2, "level": "QUARANTINED"}}),
             anomalies={"h5": ["flood"], "h2": ["fingerprint_mismatch"]})
    by = {(i.type.value, i.target): i for i in r.new}
    assert by[("QUARANTINE", "node:h4")].status == IntentStatus.APPROVED
    assert by[("QUARANTINE", "node:srv-web-1")].status == IntentStatus.PENDING_APPROVAL  # protected server
    assert by[("BLOCK", "node:h5")].status == IntentStatus.APPROVED
    assert by[("ISOLATE", "node:h2")].status == IntentStatus.PENDING_APPROVAL  # always needs approval
    assert {e.type for e in seen} >= {EventType.DECISION_MADE, EventType.APPROVAL_REQUIRED}
    pending = e.pending()
    assert {p["type"] for p in pending} == {"QUARANTINE", "ISOLATE"}
    approved = e.approve(by[("ISOLATE", "node:h2")].id)
    assert approved.status == IntentStatus.APPROVED
    rejected = e.reject(by[("QUARANTINE", "node:srv-web-1")].id)
    assert rejected.status == IntentStatus.REJECTED
    with pytest.raises(ValueError):
        e.approve(approved.id)
    with pytest.raises(KeyError):
        e.approve("intent-nope")


def test_quarantine_beyond_limit_needs_approval():
    trust = trust_all(**{h: {"trust": 0.1, "level": "QUARANTINED"} for h in ("h2", "h3", "h4")})
    r = step(engine(), trust=trust)
    statuses = [i.status for i in sorted(r.new, key=lambda i: i.target)]
    assert statuses.count(IntentStatus.PENDING_APPROVAL) == 1  # third quarantine waits


def test_autonomy_levels():
    down = {"link:h1-sw-a": {"status": "DOWN", "metrics": {}}}
    l1 = step(engine(dataclasses.replace(POLICY, autonomy="L1")), health_over=down)
    assert all(i.status == IntentStatus.PENDING_APPROVAL for i in l1.new)
    l3 = step(engine(dataclasses.replace(POLICY, autonomy="L3")),
              anomalies={"h2": ["fingerprint_mismatch"]})
    assert all(i.status == IntentStatus.APPROVED for i in l3.new)


def test_no_duplicates_and_cooldown():
    e = engine()
    lat = {"link:sw-a-sw-b": {"status": "DEGRADED", "reasons": ["latency 120 ms"],
                              "metrics": {"link.latency_ms": 120.0}}}
    assert len(step(e, 0.0, lat).new) == 1
    r = step(e, 5.0, lat)
    assert r.new == [] and r.resolved == []           # still active: not re-issued, not cancelled
    assert e.routes != e.baseline                     # traffic stays off the degraded link
    assert len(step(e, 10.0).resolved) == 1           # healthy again
    r = step(e, 20.0, lat)                             # flaps back within the 60 s cooldown
    assert r.new == [] and r.suppressed == 1
    assert len(step(e, 75.0, lat).new) == 1            # after cooldown
    status = e.status()
    assert status["suppressed_by_cooldown"] == 1 and status["decision_latency_ms_p95"] is not None


def test_resolve_conflicts_rules():
    mk = lambda t, target="node:h4": Intent(t, target, "", 0.5, 0.0)
    kept = resolve_conflicts([mk(IntentType.BLOCK), mk(IntentType.QUARANTINE), mk(IntentType.MONITOR)])
    assert [i.type for i in kept] == [IntentType.QUARANTINE]
    both = resolve_conflicts([mk(IntentType.RESTORE, "link:x"), mk(IntentType.REROUTE, "link:x")])
    assert {i.type for i in both} == {IntentType.RESTORE, IntentType.REROUTE}
    lb = resolve_conflicts([mk(IntentType.REROUTE, "link:x"), mk(IntentType.LOAD_BALANCE, "link:x")])
    assert [i.type for i in lb] == [IntentType.REROUTE]


def test_engine_introspection():
    e = engine()
    step(e, health_over={"link:sw-a-sw-b": {"status": "DOWN", "metrics": {}}})
    assert e.risk_table(1)[0]["target"] == "sw-a-sw-b"
    assert {i["type"] for i in e.active_intents()} == {"RESTORE", "REROUTE"}
    assert e.log[0]["event"] == "DECISION_MADE"
    assert e.status()["rerouted_demands"] > 0


# --------------------------------------------------------------------------
# 6.7 End-to-end evaluation (no predictor: fast, no numpy needed)
# --------------------------------------------------------------------------


def test_expected_intents_rules():
    from network.decision.evaluation import expected_intents

    g = NetworkGraph.from_topology(REDUNDANT)
    base = g.baseline_routes()
    core = {"kind": "link", "target": ["sw-a", "sw-b"], "affected": ["link:sw-a-sw-b"]}
    assert expected_intents(core, base, g) == [{("RESTORE", "link:sw-a-sw-b")}, {("REROUTE", "link:sw-a-sw-b")}]
    access = {"kind": "latency", "target": ["sw-a", "h1"], "affected": ["link:h1-sw-a"]}
    assert expected_intents(access, base, g) == [{("MONITOR", "link:h1-sw-a")}]
    bw = {"kind": "bandwidth", "target": ["sw-a", "sw-b"], "affected": ["link:sw-a-sw-b"]}
    assert expected_intents(bw, base, g) == [{("LOAD_BALANCE", "link:sw-a-sw-b"), ("REROUTE", "link:sw-a-sw-b")}]
    sw = {"kind": "switch", "target": ["sw-b"], "affected": ["node:sw-b"]}
    assert expected_intents(sw, base, g) == [{("RESTORE", "node:sw-b")}]


def test_evaluation_end_to_end(tmp_path):
    from network.decision.evaluation import render_markdown, run_evaluation

    report = run_evaluation(seeds=(5,), duration_s=5400, use_predictor=False, n_failures=5, n_attacks=2)
    s = report["summary"]
    assert report["simulated"] and report["synthetic"] and report["predictor"] is None
    assert s["incidents"] == 7 and s["correct_action_rate"] >= 0.85
    assert s["false_negative_rate"] == 0.0
    assert s["decision_latency_ms_p95"] < 50
    assert "written rules" in render_markdown(report)


def test_evaluation_catches_bad_decisions(tmp_path):
    """A policy that ignores degradation must score worse: the metric is not
    rigged to always pass."""
    import shutil

    from network.decision.evaluation import run_evaluation

    cfg = tmp_path / "cfg"
    shutil.copytree(CONFIG, cfg)
    text = (cfg / "decision_policy.yaml").read_text().replace("act_above: 0.15", "act_above: 0.45")
    (cfg / "decision_policy.yaml").write_text(text)
    good = run_evaluation(seeds=(5,), duration_s=5400, use_predictor=False, n_failures=5, n_attacks=2)
    bad = run_evaluation(seeds=(5,), duration_s=5400, use_predictor=False, n_failures=5, n_attacks=2,
                         config_dir=str(cfg))
    assert bad["summary"]["correct_action_rate"] < good["summary"]["correct_action_rate"]


# --------------------------------------------------------------------------
# 6.8 Live decisions (viewer)
# --------------------------------------------------------------------------


def test_live_decisions_follow_collector_and_security():
    from network.control import NetworkController
    from network.decision.live import LiveDecisions
    from network.failure import FailureManager
    from network.security.engine import ZeroTrustEngine
    from network.security.identity import IdentityRegistry
    from network.security.policy import ZeroTrustPolicy
    from network.security.records import FlowRecord
    from network.simulation.dataplane import SimulatedNetworkBuilder
    from network.telemetry import SimulatedSource, TelemetryCollector

    controller = NetworkController(builder=SimulatedNetworkBuilder(config=REDUNDANT))
    controller.start_network()
    clock = iter(float(t) for t in range(1000, 100000, 5))
    collector = TelemetryCollector([SimulatedSource(controller)], clock=lambda: next(clock))
    registry = IdentityRegistry.from_config(REDUNDANT, CONFIG / "identities.yaml")
    security = ZeroTrustEngine(registry, ZeroTrustPolicy.from_file(CONFIG / "zero_trust_policy.yaml"))
    live = LiveDecisions(REDUNDANT, POLICY, collector, security=security).attach()
    collector.collect_once()
    assert live.engine.active_intents() == []
    FailureManager(controller).disconnect_link("sw-a", "sw-b")
    h1 = registry.get("h1")
    security.step(1005.0, [FlowRecord(1005.0, "h5", h1.ip, h1.mac, "srv-db-1", 22)], [])
    collector.collect_once()
    active = {(i["type"], i["target"]) for i in live.engine.active_intents()}
    # one security action per device: QUARANTINE (automatic) is kept over ISOLATE
    # (equal priority, but would wait for approval)
    assert {("RESTORE", "link:sw-a-sw-b"), ("REROUTE", "link:sw-a-sw-b"), ("QUARANTINE", "node:h5")} <= active
    assert ("ISOLATE", "node:h5") not in active
    assert live.status()["inputs"] == {"prediction": False, "zero_trust": True}
    live.detach()
    assert live.status()["attached"] is False
