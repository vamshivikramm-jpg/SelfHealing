"""Unit tests for the live back-ends (network.live) with a fake Ryu API.

The controller runs on the simulated data plane; the Ryu REST API is a
fake returning what the self-healing app would. No Mininet or Ryu needed.
"""

import pathlib

import pytest

from network.config import load_topology_config
from network.control import NetworkController
from network.decision.graph import NetworkGraph
from network.live import LiveExecutor, LiveFaults, LiveServiceModel, LiveSource, RyuError
from network.live.service import parse_ping
from network.simulation.dataplane import SimulatedNetworkBuilder
from network.telemetry.metrics import Metric

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = ROOT / "network" / "config" / "enterprise-redundant.yaml"


class FakeRyu:
    base_url = "http://fake"

    def __init__(self, config):
        self.config = config
        self.measure = {}
        self.pins = {}
        self.block_list = {}
        self.calls = []
        self.fail_pin = None

    def links(self):
        out = {}
        for link in self.config.links:
            name = "-".join(sorted((link.src, link.dst)))
            row = {"carrier": True, "probes": 25, "loss_pct": 0.0, "latency_ms": 0.4}
            row.update(self.measure.get(name, {}))
            out[name] = row
        return {"links": out}

    def topology(self):
        switches = [{"name": d.name, "connected": True} for d in self.config.devices
                    if d.kind in ("router", "switch")]
        links = [{"src": l.src, "dst": l.dst, "src_port": l.src_port or 9} for l in self.config.links
                 if all(n in {s["name"] for s in switches} for n in (l.src, l.dst))]
        return {"switches": switches, "links": links, "hosts": {"h1": {"switch": "sw-a", "port": 2}}}

    def ports(self):
        c = {"rx_bytes": 1000, "tx_bytes": 2000, "rx_packets": 10, "tx_packets": 20,
             "rx_dropped": 1, "tx_dropped": 0}
        return {"sw-a": {"ports": {"2": c}}}

    def routes(self):
        return {ip: {"pinned": self.pins.get(ip), "paths": {}} for ip in ("10.0.1.20", "10.0.2.10")}

    def pin(self, ip, path):
        if self.fail_pin == ip:
            raise RyuError(400, "path uses a link that is down")
        self.calls.append(("pin", ip, list(path)))
        self.pins[ip] = list(path)

    def unpin(self, ip):
        self.calls.append(("unpin", ip))
        self.pins.pop(ip, None)

    def block(self, match, reason=""):
        block_id = len(self.block_list) + 1
        self.block_list[block_id] = match
        return {"id": block_id}

    def unblock(self, block_id):
        self.block_list.pop(block_id)


@pytest.fixture()
def ctrl():
    c = NetworkController(builder=SimulatedNetworkBuilder(config=load_topology_config(CONFIG)))
    c.start_network()
    return c


def _values(samples, metric):
    return {s.target: s.value for s in samples if s.metric == metric}


def test_live_source_reports_measurements(ctrl):
    ryu = FakeRyu(ctrl.config)
    ryu.measure["h1-sw-a"] = {"latency_ms": 50.2, "loss_pct": 4.0}
    samples = LiveSource(ctrl, ryu).collect(1.0)
    assert all(not s.simulated for s in samples)
    status = _values(samples, Metric.LINK_STATUS)
    assert set(status.values()) == {1.0} and len(status) == len(ctrl.state.links)
    assert _values(samples, Metric.LINK_LATENCY_MS)["h1-sw-a"] == 50.2
    assert _values(samples, Metric.LINK_PACKET_LOSS_PCT)["h1-sw-a"] == 4.0
    assert _values(samples, Metric.LINK_BYTES)["h1-sw-a"] == 3000
    assert _values(samples, Metric.LINK_DROPPED_PACKETS)["h1-sw-a"] == 1
    assert not _values(samples, Metric.NODE_CPU_PCT)  # not measurable on Mininet


def test_live_source_physical_fault_marks_link_down(ctrl):
    ryu = FakeRyu(ctrl.config)
    ryu.measure["sw-a-sw-b"] = {"loss_pct": 100.0}
    samples = LiveSource(ctrl, ryu).collect(1.0)
    assert _values(samples, Metric.LINK_STATUS)["sw-a-sw-b"] == 0.0
    assert _values(samples, Metric.LINK_PACKET_LOSS_PCT)["sw-a-sw-b"] == 100.0
    assert ctrl.state.get_link("sw-a", "sw-b").is_connected  # the controller does not know


def test_live_source_few_probes_do_not_mark_down(ctrl):
    ryu = FakeRyu(ctrl.config)
    ryu.measure["sw-a-sw-b"] = {"loss_pct": 100.0, "probes": 2}
    assert _values(LiveSource(ctrl, ryu).collect(1.0), Metric.LINK_STATUS)["sw-a-sw-b"] == 1.0


def test_live_source_dead_switch_and_offline_node(ctrl):
    ryu = FakeRyu(ctrl.config)
    for link in ctrl.config.links:
        if "sw-b" in (link.src, link.dst):
            ryu.measure["-".join(sorted((link.src, link.dst)))] = {"loss_pct": 100.0}
    ctrl.disable_node("h1")
    nodes = _values(LiveSource(ctrl, ryu).collect(1.0), Metric.NODE_STATUS)
    assert nodes["sw-b"] == 0.0 and nodes["h1"] == 0.0 and nodes["sw-a"] == 1.0


def test_live_source_disconnected_switch(ctrl):
    ryu = FakeRyu(ctrl.config)
    topo = ryu.topology()
    ryu.topology = lambda: dict(topo, switches=[dict(s, connected=s["name"] != "rtr-core")
                                                for s in topo["switches"]])
    samples = LiveSource(ctrl, ryu).collect(1.0)
    assert _values(samples, Metric.NODE_STATUS)["rtr-core"] == 0.0
    assert _values(samples, Metric.LINK_STATUS)["rtr-core-sw-a"] == 0.0


def test_parse_ping():
    assert parse_ping("3 packets transmitted, 2 received, 33% packet loss") == pytest.approx(2 / 3)
    assert parse_ping("3 packets transmitted, 0 received, +3 errors") == 0.0
    assert parse_ping("garbage") is None


def test_service_model_reads_real_routes(ctrl):
    ryu = FakeRyu(ctrl.config)
    ryu.routes = lambda: {"10.0.1.20": {"paths": {"sw-b": ["sw-b", "rtr-core", "sw-a"]}}}
    service = LiveServiceModel(NetworkGraph.from_topology(ctrl.config), ctrl, ryu)
    service.refresh_routes()
    assert service.routes[("h4", "srv-web-1")] == ("h4", "sw-b", "rtr-core", "sw-a", "srv-web-1")


def _executor(ctrl, ryu):
    service = LiveServiceModel(NetworkGraph.from_topology(ctrl.config), ctrl, ryu)
    return LiveExecutor(ctrl, None, service, ryu), service


def _reroute():
    return {"type": "REROUTE", "target": "link:sw-a-sw-b",
            "params": {"moves": [{"src": "h4", "dst": "srv-web-1", "old": ["h4", "sw-b", "sw-a", "srv-web-1"],
                                  "new": ["h4", "sw-b", "rtr-core", "sw-a", "srv-web-1"]}]}}


def test_executor_pins_both_directions_and_undoes(ctrl):
    ryu = FakeRyu(ctrl.config)
    executor, service = _executor(ctrl, ryu)
    result = executor.execute(_reroute())
    assert result.ok, result.detail
    assert ryu.pins == {"10.0.1.20": ["sw-b", "rtr-core", "sw-a"], "10.0.2.10": ["sw-a", "rtr-core", "sw-b"]}
    assert service.routes[("h4", "srv-web-1")] == ("h4", "sw-b", "rtr-core", "sw-a", "srv-web-1")
    executor.undo(_reroute(), result.undo)
    assert ryu.pins == {}
    assert service.routes[("h4", "srv-web-1")] == ("h4", "sw-b", "sw-a", "srv-web-1")


def test_executor_rolls_back_partial_pins(ctrl):
    ryu = FakeRyu(ctrl.config)
    ryu.fail_pin = "10.0.2.10"
    executor, service = _executor(ctrl, ryu)
    result = executor.execute(_reroute())
    assert not result.ok and "pinning failed" in result.detail
    assert ryu.pins == {}
    assert service.routes[("h4", "srv-web-1")] == ("h4", "sw-b", "sw-a", "srv-web-1")


def test_executor_blocks_by_ip_and_undoes(ctrl):
    ryu = FakeRyu(ctrl.config)
    executor, _ = _executor(ctrl, ryu)
    intent = {"type": "QUARANTINE", "target": "device:h4", "params": {"subject": "h4"}}
    result = executor.execute(intent)
    assert result.ok and list(ryu.block_list.values()) == [{"ipv4_src": "10.0.2.10"}]
    executor.undo(intent, result.undo)
    assert ryu.block_list == {}


class _Intf:
    def __init__(self):
        self.cmds = []

    def tc(self, cmd):
        self.cmds.append(cmd)

    def config(self, **params):
        self.cmds.append(("config", params))


class _Link:
    def __init__(self):
        self.intf1, self.intf2 = _Intf(), _Intf()


class _Net:
    def __init__(self):
        self.link = _Link()

    def get(self, name):
        return name

    def linksBetween(self, a, b):  # noqa: N802
        return [self.link]


class _Ctrl:
    def __init__(self, config):
        self.config = config
        self._net = _Net()
        self.applied = []
        self._impairments = {"sw-a-sw-b": {"latency_ms": 20.0}}

    def impairments(self):
        return dict(self._impairments)

    def _apply_impairment(self, key, params):
        self.applied.append((key, params))


def test_faults_drop_everything_and_restore_impairments():
    ctrl = _Ctrl(load_topology_config(CONFIG))
    faults = LiveFaults(ctrl)
    assert ctrl.physical_faults is faults
    faults.break_link("sw-b", "sw-a")
    assert not faults.link_physically_up("sw-a", "sw-b")
    assert ctrl._net.link.intf1.cmds[-1] == "%s qdisc replace dev %s root netem loss 100%%"
    faults.repair_link("sw-a", "sw-b")
    assert faults.link_physically_up("sw-a", "sw-b")
    assert ctrl.applied == [("sw-a-sw-b", {"latency_ms": 20.0})]


def test_faults_node_break_keeps_links_down_until_both_repaired():
    ctrl = _Ctrl(load_topology_config(CONFIG))
    faults = LiveFaults(ctrl)
    faults.break_node("sw-b")
    faults.break_link("sw-a", "sw-b")
    assert not faults.node_physically_up("sw-b") and faults.link_physically_up("sw-a", "h1")
    faults.repair_node("sw-b")
    assert not faults.link_physically_up("sw-a", "sw-b")  # still cut
    assert ("sw-a-sw-b", {"latency_ms": 20.0}) not in ctrl.applied

