"""Unit tests for Module 2 — SimulatedSource (2.3) and TelemetryCollector (2.5).

The Mininet data plane is mocked (FakeNet/MockBuilder), but the real
NetworkController and FailureManager are used, so these tests check
that injected failures show up in telemetry and health exactly as
designed. Telemetry here is SIMULATED: it proves the pipeline logic,
not live packet behaviour.
"""

import pathlib
from types import SimpleNamespace

import pytest

from network.control import NetworkController
from network.events import EventType
from network.failure import FailureManager
from network.telemetry import (
    HealthStatus,
    Metric,
    SimulatedSource,
    SimulationProfile,
    TelemetryCollector,
    TelemetrySource,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = ROOT / "network" / "config" / "enterprise.yaml"
INTERVAL = 5.0


class FakeNode:
    def __init__(self, name, kind):
        self.name = name
        self.kind = kind

    def start(self):
        pass

    def stop(self):
        pass


class FakeNet:
    def __init__(self, nodes):
        self._nodes = {n.name: n for n in nodes}

    def get(self, name):
        return self._nodes[name]

    def configLinkStatus(self, src, dst, status):
        pass

    def configLinkImpairment(self, key, params):
        pass


class MockBuilder:
    def __init__(self, config):
        self.config = config

    def start(self):
        return FakeNet([FakeNode(d.name, d.kind) for d in self.config.devices])

    def stop(self):
        pass

    def cleanup(self):
        pass


class FakeClock:
    def __init__(self, start=1_700_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds=INTERVAL):
        self.now += seconds


class BrokenSource(TelemetrySource):
    name = "broken"

    def collect(self, timestamp):
        raise RuntimeError("probe unreachable")


@pytest.fixture()
def env():
    from network.config import load_topology_config

    controller = NetworkController(builder=MockBuilder(load_topology_config(CONFIG)))
    controller.start_network()
    manager = FailureManager(controller)
    clock = FakeClock()
    source = SimulatedSource(controller, seed=7)
    collector = TelemetryCollector([source], event_bus=controller.event_bus,
                                   interval_s=INTERVAL, clock=clock)
    events = []
    controller.event_bus.subscribe(events.append)

    def tick(n=1):
        result = None
        for _ in range(n):
            clock.advance()
            result = collector.collect_once()
        return result

    return SimpleNamespace(controller=controller, manager=manager, collector=collector,
                           source=source, clock=clock, events=events, tick=tick)


def _status(env, key):
    return env.collector.health()[key].status


def _latest(env, metric, target):
    return env.collector.store.latest_value(metric, target)


# --------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------


def test_baseline_everything_healthy(env):
    env.tick(3)
    health = env.collector.health()
    assert len(health) == 9 + 10  # 9 links + 10 nodes
    assert {r.status for r in health.values()} == {HealthStatus.HEALTHY}


def test_samples_cover_all_targets_and_are_marked_simulated(env):
    result = env.tick(2)
    targets = {s.target for s in result.samples}
    assert {"h1-sw-a", "rtr-core-sw-a", "h4-sw-b", "rtr-core", "srv-db-1"} <= targets
    assert all(s.simulated and s.source == "simulated" for s in result.samples + result.derived)
    assert env.collector.simulated is True


def test_derived_rates_appear_from_second_collection(env):
    first = env.tick()
    assert first.derived == []
    second = env.tick()
    metrics = {s.metric for s in second.derived}
    assert {Metric.LINK_THROUGHPUT_MBPS, Metric.LINK_UTILIZATION_PCT, Metric.LINK_DROP_RATE_PPS} <= metrics


def test_baseline_values_are_plausible(env):
    env.tick(3)
    assert _latest(env, Metric.LINK_STATUS, "h1-sw-a") == 1.0
    assert 0 < _latest(env, Metric.LINK_LATENCY_MS, "h1-sw-a") < 1.0
    assert _latest(env, Metric.LINK_PACKET_LOSS_PCT, "h1-sw-a") == 0.0
    assert _latest(env, Metric.LINK_CAPACITY_MBPS, "h1-sw-a") == 1000.0
    # uplinks carry more traffic than access links
    assert _latest(env, Metric.LINK_THROUGHPUT_MBPS, "rtr-core-sw-a") > \
        _latest(env, Metric.LINK_THROUGHPUT_MBPS, "h1-sw-a")
    assert 0 < _latest(env, Metric.NODE_CPU_PCT, "rtr-core") < 100


def test_counters_are_monotonic(env):
    env.tick()
    values = []
    for _ in range(5):
        env.tick()
        values.append(_latest(env, Metric.LINK_BYTES, "rtr-core-sw-a"))
    assert values == sorted(values) and values[-1] > values[0]


def test_same_seed_is_deterministic():
    from network.config import load_topology_config

    def run():
        controller = NetworkController(builder=MockBuilder(load_topology_config(CONFIG)))
        controller.start_network()
        clock = FakeClock()
        collector = TelemetryCollector([SimulatedSource(controller, seed=3)], clock=clock)
        out = []
        for _ in range(3):
            clock.advance()
            out.append([(s.metric, s.target, s.value) for s in collector.collect_once().samples])
        return out

    assert run() == run()


# --------------------------------------------------------------------------
# Failures injected through FailureManager show up in telemetry
# --------------------------------------------------------------------------


def test_stopped_network_reports_everything_down():
    from network.config import load_topology_config

    controller = NetworkController(builder=MockBuilder(load_topology_config(CONFIG)))
    clock = FakeClock()
    collector = TelemetryCollector([SimulatedSource(controller)], clock=clock)
    collector.collect_once()
    assert {r.status for r in collector.health().values()} == {HealthStatus.DOWN}


def test_cable_cut_detected_and_recovered(env):
    env.tick(2)
    env.manager.disconnect_link("sw-a", "h1")
    env.tick()
    assert _status(env, "link:h1-sw-a") is HealthStatus.DOWN
    assert _latest(env, Metric.LINK_PACKET_LOSS_PCT, "h1-sw-a") == 100.0
    assert _latest(env, Metric.LINK_THROUGHPUT_MBPS, "h1-sw-a") == 0.0
    assert _status(env, "link:h2-sw-a") is HealthStatus.HEALTHY

    env.manager.reconnect_link("sw-a", "h1")
    env.tick()
    assert _status(env, "link:h1-sw-a") is HealthStatus.HEALTHY


def test_high_latency_degrades_link(env):
    env.tick(2)
    env.manager.inject_latency("sw-a", "h1", 100)
    env.tick()
    assert _status(env, "link:h1-sw-a") is HealthStatus.DEGRADED
    assert _latest(env, Metric.LINK_LATENCY_MS, "h1-sw-a") == pytest.approx(100.05, abs=0.2)
    assert any("latency" in r for r in env.collector.health()["link:h1-sw-a"].reasons)

    env.manager.remove_latency("sw-a", "h1")
    env.tick()
    assert _status(env, "link:h1-sw-a") is HealthStatus.HEALTHY


def test_packet_loss_is_measured_with_noise(env):
    env.tick()
    env.manager.inject_packet_loss("sw-a", "h1", 10)
    readings = []
    for _ in range(40):
        env.tick()
        readings.append(_latest(env, Metric.LINK_PACKET_LOSS_PCT, "h1-sw-a"))
    mean = sum(readings) / len(readings)
    assert 7.0 < mean < 13.0           # centred on the configured 10 %
    assert len(set(readings)) > 1      # measured, not copied
    assert _status(env, "link:h1-sw-a") is HealthStatus.DEGRADED


def test_bandwidth_limit_caps_throughput(env):
    env.tick(2)
    env.manager.limit_bandwidth("sw-a", "h1", 10)
    env.tick(2)
    assert _latest(env, Metric.LINK_CAPACITY_MBPS, "h1-sw-a") == 10.0
    assert _latest(env, Metric.LINK_THROUGHPUT_MBPS, "h1-sw-a") <= 10.0 + 1e-6
    assert _latest(env, Metric.LINK_UTILIZATION_PCT, "h1-sw-a") > 85.0
    assert _status(env, "link:h1-sw-a") is HealthStatus.DEGRADED

    env.manager.remove_bandwidth_limit("sw-a", "h1")
    env.tick(2)
    assert _status(env, "link:h1-sw-a") is HealthStatus.HEALTHY


def test_switch_failure_takes_down_its_links_only(env):
    env.tick(2)
    env.manager.inject_switch_failure("sw-a")
    env.tick()
    health = env.collector.health()
    assert health["node:sw-a"].status is HealthStatus.DOWN
    for link in ("h1-sw-a", "h2-sw-a", "h3-sw-a", "srv-web-1-sw-a", "srv-db-1-sw-a", "rtr-core-sw-a"):
        assert health[f"link:{link}"].status is HealthStatus.DOWN
    assert health["link:h4-sw-b"].status is HealthStatus.HEALTHY
    assert health["link:rtr-core-sw-b"].status is HealthStatus.HEALTHY


def test_switch_restart_resets_counters_without_bogus_rates(env):
    env.tick(3)
    before = _latest(env, Metric.LINK_BYTES, "h1-sw-a")
    env.manager.inject_switch_failure("sw-a")
    env.tick()
    env.manager.recover_switch("sw-a")
    restarted = env.tick()
    assert _latest(env, Metric.LINK_BYTES, "h1-sw-a") < before
    assert env.collector.deriver.resets_detected >= 1
    rates = [s for s in restarted.derived if s.target == "h1-sw-a" and s.metric == Metric.LINK_THROUGHPUT_MBPS]
    assert rates == []  # no negative/garbage rate across the reset
    env.tick()
    assert _status(env, "link:h1-sw-a") is HealthStatus.HEALTHY


def test_router_failure_takes_down_uplinks(env):
    env.tick(2)
    env.manager.inject_router_failure("rtr-core")
    env.tick()
    assert _status(env, "node:rtr-core") is HealthStatus.DOWN
    assert _status(env, "link:rtr-core-sw-a") is HealthStatus.DOWN
    assert _status(env, "link:rtr-core-sw-b") is HealthStatus.DOWN
    assert _status(env, "link:h1-sw-a") is HealthStatus.HEALTHY


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


def _of(env, event_type):
    return [e for e in env.events if e.type == event_type]


def test_telemetry_collected_event_each_cycle(env):
    env.tick(3)
    collected = _of(env, EventType.TELEMETRY_COLLECTED)
    assert len(collected) == 3
    assert collected[-1].data["sequence"] == 3
    assert collected[-1].data["simulated"] is True
    assert collected[-1].source == "telemetry.collector"


def test_health_changed_only_on_transitions(env):
    env.tick(3)
    initial = len(_of(env, EventType.HEALTH_CHANGED))
    assert initial == 19  # UNKNOWN -> HEALTHY once per target
    env.tick(3)
    assert len(_of(env, EventType.HEALTH_CHANGED)) == initial

    env.manager.disconnect_link("sw-a", "h1")
    env.tick(2)
    changes = _of(env, EventType.HEALTH_CHANGED)[initial:]
    assert len(changes) == 1
    assert changes[0].data["key"] == "link:h1-sw-a"
    assert changes[0].data["previous"] == "HEALTHY"
    assert changes[0].data["status"] == "DOWN"


def test_broken_source_is_isolated(env):
    clock = FakeClock()
    collector = TelemetryCollector([env.source, BrokenSource()], event_bus=env.controller.event_bus, clock=clock)
    result = collector.collect_once()
    assert "broken" in result.source_errors
    assert "probe unreachable" in result.source_errors["broken"]
    assert result.samples  # the simulated source still delivered
    errors = _of(env, EventType.TELEMETRY_SOURCE_ERROR)
    assert errors and errors[0].data["source"] == "broken" and errors[0].result == "failure"
    assert _of(env, EventType.TELEMETRY_COLLECTED)[-1].result == "partial"
    assert collector.status()["source_errors"] == result.source_errors


# --------------------------------------------------------------------------
# Collector configuration and background loop
# --------------------------------------------------------------------------


def test_collector_validation(env):
    with pytest.raises(ValueError):
        TelemetryCollector([])
    with pytest.raises(ValueError):
        TelemetryCollector([env.source], interval_s=0)
    with pytest.raises(ValueError):
        TelemetryCollector([env.source, SimulatedSource(env.controller)])  # duplicate names


def test_status_summary(env):
    env.tick(2)
    status = env.collector.status()
    assert status["collections"] == 2
    assert status["simulated"] is True
    assert status["running"] is False
    assert status["health_counts"]["HEALTHY"] == 19
    assert status["sources"][0]["name"] == "simulated"
    assert status["last_collection"]["sequence"] == 2


def test_background_loop_starts_and_stops(env):
    import time

    collector = TelemetryCollector([SimulatedSource(env.controller)], interval_s=0.01)
    collector.start()
    collector.start()  # idempotent
    deadline = time.time() + 2.0
    while collector.status()["collections"] < 3 and time.time() < deadline:
        time.sleep(0.01)
    collector.stop()
    assert collector.status()["collections"] >= 3
    assert collector.is_running is False


def test_custom_profile(env):
    source = SimulatedSource(env.controller, profile=SimulationProfile(line_rate_mbps=100.0), name="sim2")
    collector = TelemetryCollector([source], clock=env.clock)
    collector.collect_once()
    assert collector.store.latest_value(Metric.LINK_CAPACITY_MBPS, "h1-sw-a") == 100.0
