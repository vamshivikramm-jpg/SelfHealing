"""Live telemetry, faults and healing actions on the real network (P3-P4).

Requires a running stack (mininet-net + ryu-controller with the
self-healing app):
    make net-test

Uses the redundant topology (sw-a <-> sw-b plus both uplinks). Checks that
measured latency/loss follow injected impairments, that physical faults
(the controller still believes the link is fine) show up as down links, and
that a reroute pinned through the controller restores delivery measured
with real pings, while a quarantine really drops a device's traffic.
"""

import pathlib
import time
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CFG = ROOT / "network" / "config"
SETTLE = 7.0  # probe window (5 s) + timeout (1 s) + margin


def _values(samples, metric):
    return {s.target: s.value for s in samples if s.metric == metric}


@pytest.fixture(scope="module")
def lab():
    from mininet.clean import cleanup

    from network.control import NetworkController
    from network.live import LiveFaults, LiveSource, RyuClient

    client = RyuClient()
    client.set_topology("enterprise-redundant.yaml")
    ctrl = NetworkController(config_path=str(CFG / "enterprise-redundant.yaml"))
    ctrl.start_network()
    deadline = time.time() + 45
    while time.time() < deadline and not all(s.connected() for s in ctrl._net.switches):
        time.sleep(0.5)
    for host in ctrl._net.hosts:  # every host talks once, so the controller locates it
        host.cmd(f"ping -c1 -W1 {host.defaultIntf().IP().rsplit('.', 1)[0]}.1")
    time.sleep(SETTLE)
    lab = {"ctrl": ctrl, "client": client, "faults": LiveFaults(ctrl), "source": LiveSource(ctrl, client)}
    yield lab
    try:
        ctrl.stop_network()
    finally:
        cleanup()
        client.set_topology("enterprise.yaml")


def collect(lab):
    from network.telemetry.metrics import Metric

    samples = lab["source"].collect(time.time())
    return {m: _values(samples, m) for m in (Metric.LINK_STATUS, Metric.LINK_LATENCY_MS,
                                             Metric.LINK_PACKET_LOSS_PCT, Metric.NODE_STATUS)}


def test_healthy_network_measures_clean(lab):
    from network.telemetry.metrics import Metric

    m = collect(lab)
    assert set(m[Metric.LINK_STATUS].values()) == {1.0}, m[Metric.LINK_STATUS]
    assert len(m[Metric.LINK_STATUS]) == 10
    assert max(m[Metric.LINK_PACKET_LOSS_PCT].values()) <= 5.0
    assert max(m[Metric.LINK_LATENCY_MS].values()) < 10.0, m[Metric.LINK_LATENCY_MS]
    assert set(m[Metric.NODE_STATUS].values()) == {1.0}


def test_measured_latency_follows_impairment(lab):
    from network.telemetry.metrics import Metric

    lab["ctrl"].set_link_latency("sw-a", "h2", 50)
    try:
        time.sleep(SETTLE)
        latency = collect(lab)[Metric.LINK_LATENCY_MS]["h2-sw-a"]
        assert 40.0 <= latency <= 65.0, latency
    finally:
        lab["ctrl"].clear_link_latency("sw-a", "h2")


def test_measured_loss_follows_impairment(lab):
    from network.telemetry.metrics import Metric

    lab["ctrl"].set_link_packet_loss("rtr-core", "sw-b", 30)
    try:
        time.sleep(SETTLE)
        m = collect(lab)
        loss = m[Metric.LINK_PACKET_LOSS_PCT]["rtr-core-sw-b"]
        assert 10.0 <= loss <= 55.0, loss
        assert m[Metric.LINK_STATUS]["rtr-core-sw-b"] == 1.0  # lossy, not down
    finally:
        lab["ctrl"].clear_link_packet_loss("rtr-core", "sw-b")
    time.sleep(SETTLE)


def test_physical_fault_seen_only_by_measurement(lab):
    from network.telemetry.metrics import Metric

    lab["faults"].break_link("sw-a", "sw-b")
    try:
        time.sleep(SETTLE)
        assert collect(lab)[Metric.LINK_STATUS]["sw-a-sw-b"] == 0.0
        assert lab["ctrl"].state.get_link("sw-a", "sw-b").is_connected
    finally:
        lab["faults"].repair_link("sw-a", "sw-b")
    time.sleep(SETTLE)
    assert collect(lab)[Metric.LINK_STATUS]["sw-a-sw-b"] == 1.0


def test_disconnected_link_and_failed_switch(lab):
    from network.telemetry.metrics import Metric

    lab["ctrl"].disconnect_link("rtr-core", "sw-a")
    try:
        time.sleep(1.0)
        assert collect(lab)[Metric.LINK_STATUS]["rtr-core-sw-a"] == 0.0
    finally:
        lab["ctrl"].connect_link("rtr-core", "sw-a")
    lab["ctrl"].disable_node("sw-b")
    try:
        time.sleep(2.0)
        assert collect(lab)[Metric.NODE_STATUS]["sw-b"] == 0.0
    finally:
        lab["ctrl"].enable_node("sw-b")
    deadline = time.time() + 30
    while time.time() < deadline and not lab["ctrl"]._net.get("sw-b").connected():
        time.sleep(0.5)
    for host in ("h4", "h5"):  # re-locate after the switch restart
        lab["ctrl"]._net.get(host).cmd("ping -c1 -W1 10.0.2.1")
    time.sleep(SETTLE)
    m = collect(lab)
    assert m[Metric.NODE_STATUS]["sw-b"] == 1.0 and set(m[Metric.LINK_STATUS].values()) == {1.0}


def _service(lab):
    from network.decision.graph import NetworkGraph
    from network.live import LiveServiceModel

    return LiveServiceModel(NetworkGraph.from_topology(lab["ctrl"].config), lab["ctrl"], lab["client"])


def _measure(service, steps=2):
    for _ in range(steps):
        service.step(time.time())
        time.sleep(2.0)
    service.step(time.time())


def test_reroute_around_physical_fault_restores_delivery(lab):
    from network.live import LiveExecutor, LiveVerifier

    service = _service(lab)
    executor = LiveExecutor(lab["ctrl"], None, service, lab["client"])
    verifier = LiveVerifier(SimpleNamespace(store=None), service, lab["ctrl"])
    pair = ("h4", "srv-web-1")
    _measure(service)
    assert service.latest(pair) == 1.0 and service.routes[pair] == ("h4", "sw-b", "sw-a", "srv-web-1")

    lab["faults"].break_link("sw-a", "sw-b")
    try:
        _measure(service)
        assert service.latest(pair) == 0.0, "the cut link should stop delivery"
        intent = {"type": "REROUTE", "target": "link:sw-a-sw-b", "params": {"moves": [
            {"src": "h4", "dst": "srv-web-1", "old": list(service.routes[pair]),
             "new": ["h4", "sw-b", "rtr-core", "sw-a", "srv-web-1"]}]}}
        result = executor.execute(intent)
        assert result.ok, result.detail
        _measure(service)
        assert service.routes[pair] == ("h4", "sw-b", "rtr-core", "sw-a", "srv-web-1")
        ok, detail = verifier.check({"check": "route_avoids", "src": "h4", "dst": "srv-web-1",
                                     "link": "sw-a-sw-b"}, time.time())
        assert ok, detail
        executor.undo(intent, result.undo)
        assert lab["client"].routes()["10.0.1.20"]["pinned"] is None
    finally:
        lab["faults"].repair_link("sw-a", "sw-b")


def test_quarantine_drops_device_traffic(lab):
    from network.live import LiveExecutor, LiveVerifier

    service = _service(lab)
    executor = LiveExecutor(lab["ctrl"], None, service, lab["client"])
    verifier = LiveVerifier(SimpleNamespace(store=None), service, lab["ctrl"])
    intent = {"type": "QUARANTINE", "target": "device:h4", "params": {"subject": "h4"}}
    assert not verifier.check({"check": "access_blocked", "subject": "h4"}, 0)[0]
    result = executor.execute(intent)
    assert result.ok, result.detail
    try:
        ok, detail = verifier.check({"check": "access_blocked", "subject": "h4"}, 0)
        assert ok, detail
        assert not verifier.check({"check": "access_blocked", "subject": "h5"}, 0)[0], "h5 must still work"
    finally:
        executor.undo(intent, result.undo)
    assert not verifier.check({"check": "access_blocked", "subject": "h4"}, 0)[0]
