"""Unit tests for Module 8.1 — the live simulation runtime (driven by a fake clock)."""

import pytest

from network.simulation.runtime import RuntimeError_, SimulationRuntime
from network.telemetry import Metric


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture()
def rt():
    clock = Clock()
    runtime = SimulationRuntime(interval_s=2.0, use_predictor=False, clock=clock)
    runtime._test_clock = clock
    return runtime


def advance(rt, seconds):
    for _ in range(int(seconds / rt.interval_s)):
        rt._test_clock.t += rt.interval_s
        rt.tick()


def test_starts_healthy(rt):
    advance(rt, 10)
    assert rt.status()["ticks"] == 5 and rt.status()["simulated"]
    health = {k: r.status.value for k, r in rt.collector.health().items()}
    assert set(health.values()) == {"HEALTHY"}
    assert rt.decisions.active_intents() == []


def test_physical_core_cut_is_rerouted_and_restore_escalated(rt):
    advance(rt, 6)
    item = rt.inject_failure("link", ["sw-a", "sw-b"], duration_s=60, physical=True)
    assert item["state"] == "scheduled" and "_failure" not in item
    advance(rt, 40)
    states = {(a["type"], a["state"]) for a in rt.healing.history()}
    assert ("RESTORE", "ESCALATED") in states
    assert any(t == "REROUTE" and s == "VERIFIED" for t, s in states)
    assert rt.rerouted() and all("sw-a-sw-b" not in r["links"] for r in rt.rerouted())
    advance(rt, 40)  # planned repair at 60 s, then fail back
    assert rt.injections_view()[0]["state"] == "repaired"
    assert rt.rerouted() == []
    assert rt.collector.store.latest_value(Metric.LINK_STATUS, "sw-a-sw-b") == 1.0
    types = {e["type"] for e in rt.events}
    assert {"HEALTH_CHANGED", "DECISION_MADE", "HEALING_STARTED", "HEALING_VERIFIED", "ESCALATED"} <= types


def test_warning_ramp_then_failure(rt):
    rt.inject_failure("latency", ["sw-a", "h1"], duration_s=20, warning_s=20)
    advance(rt, 10)
    assert rt.injections_view()[0]["state"] == "warning"
    assert 1.0 < rt.collector.store.latest_value(Metric.LINK_LATENCY_MS, "h1-sw-a") < 20.0
    advance(rt, 14)
    assert rt.injections_view()[0]["state"] == "active"
    assert rt.collector.store.latest_value(Metric.LINK_LATENCY_MS, "h1-sw-a") > 100


def test_attack_is_contained(rt):
    advance(rt, 4)
    rt.launch_attack("spoofing", "h4", duration_s=30)
    advance(rt, 20)
    assert "h4" in rt.security.quarantined() or "h4" in rt.security.manual_blocks
    assert any(a["target"] == "node:h4" and a["verified_at"] for a in rt.healing.history())


def test_operator_controls_and_validation(rt):
    with pytest.raises(RuntimeError_):
        rt.inject_failure("meteor", ["sw-a"])
    with pytest.raises(RuntimeError_):
        rt.inject_failure("switch", ["rtr-core"])        # a router, not a switch
    with pytest.raises(RuntimeError_):
        rt.inject_failure("link", ["h1", "h2"])          # not a link
    with pytest.raises(RuntimeError_):
        rt.inject_failure("link", ["sw-a", "h1"], duration_s=1)
    with pytest.raises(RuntimeError_):
        rt.launch_attack("flood", "srv-web-1")
    rt.inject_failure("link", ["sw-a", "h1"], duration_s=30)
    with pytest.raises(RuntimeError_):
        rt.inject_failure("latency", ["sw-a", "h1"])     # already failing
    with pytest.raises(RuntimeError_):
        rt.inject_failure("switch", ["sw-a"])             # its link is already failing
    rt.inject_failure("latency", ["sw-a", "h2"], duration_s=10)  # another link on the same switch is fine
    assert rt.set_autonomy("L1") == "L1" and rt.status()["autonomy"] == "L1"
    with pytest.raises(RuntimeError_):
        rt.set_autonomy("L7")
    advance(rt, 6)
    pending = rt.decisions.pending()
    assert pending and pending[0]["type"] == "RESTORE"   # L1: waits for a person
    assert rt.approve(pending[0]["id"])["status"] == "APPROVED"
    advance(rt, 8)
    assert any(a["type"] == "RESTORE" and a["verified_at"] for a in rt.healing.history())


def test_demo_script_schedules_events(rt):
    steps = rt.start_demo()
    assert len(steps) == 5 and rt.status()["demo_pending"] == 5
    advance(rt, 12)
    assert rt.status()["demo_pending"] == 4
    assert rt.injections_view()[0]["kind"] == "link"


def test_background_thread():
    import time

    runtime = SimulationRuntime(interval_s=0.05, use_predictor=False)
    runtime.start()
    deadline = time.time() + 3
    while runtime.ticks < 3 and time.time() < deadline:
        time.sleep(0.02)
    runtime.stop()
    assert runtime.ticks >= 3 and not runtime.running
