"""Unit tests for the real-Mininet paths of NetworkController (live P1).

The fakes mimic the Mininet objects the controller touches: switches have a
``dpid`` and ``start(controllers)``/``stop(deleteIntfs)``; hosts have
interfaces with ``ifconfig``; links have two ``tc``-capable interfaces and
the network offers ``linksBetween``. No root or Mininet needed.
"""

import pathlib

import pytest

from network.config import load_topology_config
from network.control import NetworkController
from network.enterprise_topology import EnterpriseTopology
from network.network_builder import load_identity_macs

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = ROOT / "network" / "config" / "enterprise.yaml"
IDENTITIES = ROOT / "network" / "config" / "identities.yaml"


class Intf:
    def __init__(self, name, node):
        self.name, self.node, self.link = name, node, None
        self.state = "up"
        self.tc_cmds, self.configs = [], []

    def ifconfig(self, state):
        self.state = state

    def tc(self, cmd):
        self.tc_cmds.append(cmd)

    def config(self, **params):
        self.configs.append(params)


class Link:
    def __init__(self, a, b):
        self.intf1 = Intf(f"{a.name}-eth{len(a.intfs) + 1}", a)
        self.intf2 = Intf(f"{b.name}-eth{len(b.intfs) + 1}", b)
        self.intf1.link = self.intf2.link = self
        a.intfs.append(self.intf1)
        b.intfs.append(self.intf2)


class Switch:
    def __init__(self, name):
        self.name, self.dpid, self.intfs = name, "1", []
        self.calls = []

    def start(self, controllers):
        self.calls.append(("start", list(controllers)))

    def stop(self, deleteIntfs=True):  # noqa: N803
        self.calls.append(("stop", deleteIntfs))


class Host:
    def __init__(self, name):
        self.name, self.intfs = name, []

    def intfList(self):  # noqa: N802
        return list(self.intfs)

    def cmd(self, *_):
        return ""


class Net:
    def __init__(self, config):
        self.nodes = {}
        for dev in config.devices:
            self.nodes[dev.name] = Switch(dev.name) if dev.kind in ("router", "switch") else Host(dev.name)
        self.links = [Link(self.nodes[l.src], self.nodes[l.dst]) for l in config.links]
        self.controllers = ["ryu"]
        self.link_status = []

    def get(self, name):
        return self.nodes[name]

    @property
    def hosts(self):
        return [n for n in self.nodes.values() if isinstance(n, Host)]

    @property
    def switches(self):
        return [n for n in self.nodes.values() if isinstance(n, Switch)]

    def linksBetween(self, a, b):  # noqa: N802
        return [l for l in self.links if {l.intf1.node, l.intf2.node} == {a, b}]

    def configLinkStatus(self, a, b, status):  # noqa: N802
        self.link_status.append((a, b, status))


class Builder:
    def __init__(self):
        self.config = load_topology_config(CONFIG)
        self.net = None

    def start(self):
        self.net = Net(self.config)
        return self.net

    def stop(self):
        self.net = None

    def cleanup(self):
        pass


@pytest.fixture()
def ctrl():
    c = NetworkController(builder=Builder())
    c.start_network()
    return c


def _first_link(ctrl):
    link = ctrl.config.links[0]
    return link.src, link.dst


def test_switch_stops_keeping_interfaces_and_restarts_with_controllers(ctrl):
    ctrl.disable_node("sw-a")
    ctrl.enable_node("sw-a")
    assert ctrl._net.get("sw-a").calls == [("stop", False), ("start", ["ryu"])]


def test_switch_restart_keeps_disconnected_links_down(ctrl):
    src, dst = next((l.src, l.dst) for l in ctrl.config.links if "sw-a" in (l.src, l.dst))
    ctrl.disconnect_link(src, dst)
    ctrl.disable_node("sw-a")
    ctrl.enable_node("sw-a")
    assert ctrl._net.link_status[-1] == (src, dst, "down")


def test_host_disable_enable_toggles_interfaces(ctrl):
    host = ctrl._net.get("h1")
    ctrl.disable_node("h1")
    assert all(i.state == "down" for i in host.intfs)
    ctrl.enable_node("h1")
    assert all(i.state == "up" for i in host.intfs)


def test_host_enable_keeps_disconnected_link_down(ctrl):
    peer = next(l.src if l.dst == "h1" else l.dst for l in ctrl.config.links if "h1" in (l.src, l.dst))
    ctrl.disable_node("h1")
    ctrl.disconnect_link("h1", peer)
    ctrl.enable_node("h1")
    assert all(i.state == "down" for i in ctrl._net.get("h1").intfs)


def test_impairments_use_tc_on_both_link_ends(ctrl):
    a, b = _first_link(ctrl)
    ctrl.set_link_latency(a, b, 40)
    ctrl.set_link_packet_loss(a, b, 5)
    ctrl.set_link_bandwidth(a, b, 10)
    (link,) = ctrl._net.linksBetween(ctrl._net.get(a), ctrl._net.get(b))
    for intf in (link.intf1, link.intf2):
        assert intf.configs[-1] == {"delay": "40ms", "loss": 5.0, "bw": 10.0}
        assert intf.tc_cmds[-1] == "%s qdisc del dev %s root"


def test_clearing_last_impairment_removes_qdisc_without_reconfig(ctrl):
    a, b = _first_link(ctrl)
    ctrl.set_link_latency(a, b, 40)
    ctrl.clear_link_latency(a, b)
    (link,) = ctrl._net.linksBetween(ctrl._net.get(a), ctrl._net.get(b))
    assert len(link.intf1.configs) == 1 and len(link.intf1.tc_cmds) == 2


def test_link_keys_with_dashes_split_correctly(ctrl):
    assert ctrl._split_key("rtr-core-sw-a") == ("rtr-core", "sw-a")


def test_identity_macs_assigned_to_hosts():
    macs = load_identity_macs(str(IDENTITIES))
    assert macs["h1"] == "02:00:0a:00:01:0a"
    topo = EnterpriseTopology(load_topology_config(CONFIG), macs=macs)
    assert topo.nodeInfo("h1")["mac"] == "02:00:0a:00:01:0a"
    assert "mac" not in topo.nodeInfo("sw-a")
