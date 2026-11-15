"""Unit tests for Module 7.1 — physical faults and honest simulated telemetry."""

import pathlib

import pytest

from network.analytics.scenarios import PlannedFailure, ScenarioPlan, ScenarioRunner, generate_plan
from network.config import load_topology_config
from network.control import NetworkController
from network.failure import FailureManager
from network.simulation.dataplane import SimulatedNetworkBuilder
from network.telemetry import Metric, SimulatedSource, TelemetryCollector

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = load_topology_config(ROOT / "network" / "config" / "enterprise-redundant.yaml")


def setup():
    builder = SimulatedNetworkBuilder(config=CONFIG)
    controller = NetworkController(builder=builder)
    controller.start_network()
    clock = iter(float(t) for t in range(0, 10000, 5))
    collector = TelemetryCollector([SimulatedSource(controller, physical=lambda: builder.net)],
                                   clock=lambda: next(clock))
    return builder, controller, FailureManager(controller), collector


def status(collector, metric, target):
    return collector.store.latest_value(metric, target)


def test_physical_link_stays_down_after_software_reconnect():
    builder, controller, manager, collector = setup()
    manager.disconnect_link("sw-a", "h1")
    builder.net.break_link("sw-a", "h1")
    manager.reconnect_link("sw-a", "h1")          # the command "succeeds"...
    assert controller.state.get_link("sw-a", "h1").is_connected
    collector.collect_once()
    assert status(collector, Metric.LINK_STATUS, "h1-sw-a") == 0.0  # ...but nothing works
    builder.net.repair_link("sw-a", "h1")
    collector.collect_once()
    assert status(collector, Metric.LINK_STATUS, "h1-sw-a") == 1.0


def test_physical_node_and_its_links_stay_down():
    builder, controller, manager, collector = setup()
    manager.inject_switch_failure("sw-b")
    builder.net.break_node("sw-b")
    manager.recover_switch("sw-b")
    collector.collect_once()
    assert status(collector, Metric.NODE_STATUS, "sw-b") == 0.0
    assert status(collector, Metric.LINK_STATUS, "h4-sw-b") == 0.0
    assert status(collector, Metric.LINK_STATUS, "h1-sw-a") == 1.0
    assert not builder.net.node_physically_up("sw-b") and builder.net.node_physically_up("sw-a")


def test_source_without_physical_unchanged():
    builder = SimulatedNetworkBuilder(config=CONFIG)
    controller = NetworkController(builder=builder)
    controller.start_network()
    builder.net.break_link("sw-a", "h1")  # ignored without the physical view
    collector = TelemetryCollector([SimulatedSource(controller)], clock=lambda: 1.0)
    collector.collect_once()
    assert status(collector, Metric.LINK_STATUS, "h1-sw-a") == 1.0


def test_generate_plan_physical_flags_do_not_change_the_plan():
    plain = generate_plan(CONFIG, 20000, seed=3, n_failures=15)
    phys = generate_plan(CONFIG, 20000, seed=3, n_failures=15,
                         physical_probability={"link": 1.0, "switch": 1.0, "router": 1.0})
    strip = lambda p: [(f.kind, f.target, f.start_s, f.duration_s, f.magnitude, f.precursor) for f in p.failures]
    assert strip(plain) == strip(phys)
    assert not any(f.physical for f in plain.failures)
    assert all(f.physical == (f.kind in ("link", "switch", "router")) for f in phys.failures)
    with pytest.raises(ValueError):
        PlannedFailure("latency", ("sw-a", "h1"), 10, 10, 50, None, True)


def test_scenario_physical_failure_and_tolerant_recovery():
    plan = ScenarioPlan(duration_s=400, step_s=5, failures=(
        PlannedFailure("link", ("sw-a", "h1"), 100, 60, physical=True),))
    runner = ScenarioRunner(plan, config=CONFIG)
    # self-healing "restores" it via the controller mid-failure: still down physically
    runner.collector.add_listener(
        lambda r: runner.manager.reconnect_link("sw-a", "h1") if r.timestamp == runner.start_time + 120 else None)
    result = runner.run()  # the planned recovery at 160 s must not crash
    points = dict(result.repository.query_points(Metric.LINK_STATUS, "h1-sw-a"))
    t0 = runner.start_time
    assert points[t0 + 125] == 0.0 and points[t0 + 160] == 1.0
    assert result.records[0].physical is True
