"""Unit tests for Submodule 1.4C.1 — NetworkController impairment APIs.

Runs anywhere (no root/Mininet required): the Mininet network is mocked
via FakeNet/MockBuilder; the controller's impairment validation, state
tracking and application are exercised for real.
"""

import pathlib

import pytest

from network.control import (
    LinkNotFoundError,
    NetworkController,
    NetworkNotRunningError,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = ROOT / "network" / "config" / "enterprise.yaml"


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
        self.link_status = []
        self.impairments = []

    @property
    def hosts(self):
        return [n for n in self._nodes.values() if n.kind in ("host", "server")]

    @property
    def switches(self):
        return [n for n in self._nodes.values() if n.kind in ("router", "switch")]

    def get(self, name):
        return self._nodes[name]

    def configLinkStatus(self, src, dst, status):
        self.link_status.append((src, dst, status))

    def configLinkImpairment(self, key, params):
        self.impairments.append((key, dict(params)))


class MockBuilder:
    def __init__(self, config):
        self.config = config
        self.net = None

    def start(self):
        nodes = [FakeNode(dev.name, dev.kind) for dev in self.config.devices]
        self.net = FakeNet(nodes)
        return self.net

    def stop(self):
        self.net = None

    def cleanup(self):
        pass


@pytest.fixture()
def controller():
    from network.config import load_topology_config

    builder = MockBuilder(load_topology_config(CONFIG))
    ctrl = NetworkController(builder=builder)
    ctrl.start_network()
    return ctrl


@pytest.fixture()
def net(controller):
    return controller._net


# --------------------------------------------------------------------------
# Latency
# --------------------------------------------------------------------------


def test_set_link_latency(controller, net):
    result = controller.set_link_latency("sw-a", "h1", 20)
    assert result == {"latency_ms": 20.0}
    assert controller._impairments["h1-sw-a"] == {"latency_ms": 20.0}
    assert net.impairments[-1] == ("h1-sw-a", {"latency_ms": 20.0})


def test_set_link_latency_order_insensitive(controller, net):
    controller.set_link_latency("h1", "sw-a", 10)
    assert controller._impairments["h1-sw-a"] == {"latency_ms": 10.0}


def test_clear_link_latency(controller, net):
    controller.set_link_latency("sw-a", "h1", 20)
    result = controller.clear_link_latency("sw-a", "h1")
    assert result == {}
    assert controller._impairments == {}
    assert net.impairments[-1] == ("h1-sw-a", {})


def test_set_negative_latency_rejected(controller):
    with pytest.raises(ValueError, match="latency_ms"):
        controller.set_link_latency("sw-a", "h1", -1)


# --------------------------------------------------------------------------
# Packet loss
# --------------------------------------------------------------------------


def test_set_link_packet_loss(controller, net):
    controller.set_link_packet_loss("sw-a", "h1", 30)
    assert controller._impairments["h1-sw-a"] == {"loss_percent": 30.0}
    assert net.impairments[-1] == ("h1-sw-a", {"loss_percent": 30.0})


def test_set_link_packet_loss_boundaries(controller):
    controller.set_link_packet_loss("sw-a", "h1", 0)
    controller.clear_link_packet_loss("sw-a", "h1")
    controller.set_link_packet_loss("sw-a", "h1", 100)


def test_clear_link_packet_loss(controller):
    controller.set_link_packet_loss("sw-a", "h1", 30)
    controller.clear_link_packet_loss("sw-a", "h1")
    assert controller._impairments == {}


def test_invalid_packet_loss_rejected(controller):
    with pytest.raises(ValueError, match="loss_percent"):
        controller.set_link_packet_loss("sw-a", "h1", -1)
    with pytest.raises(ValueError, match="loss_percent"):
        controller.set_link_packet_loss("sw-a", "h1", 101)


# --------------------------------------------------------------------------
# Bandwidth
# --------------------------------------------------------------------------


def test_set_link_bandwidth(controller, net):
    controller.set_link_bandwidth("sw-a", "h1", 100)
    assert controller._impairments["h1-sw-a"] == {"bandwidth_mbps": 100.0}
    assert net.impairments[-1] == ("h1-sw-a", {"bandwidth_mbps": 100.0})


def test_clear_link_bandwidth(controller):
    controller.set_link_bandwidth("sw-a", "h1", 100)
    controller.clear_link_bandwidth("sw-a", "h1")
    assert controller._impairments == {}


def test_invalid_bandwidth_rejected(controller):
    with pytest.raises(ValueError, match="bandwidth_mbps"):
        controller.set_link_bandwidth("sw-a", "h1", 0)
    with pytest.raises(ValueError, match="bandwidth_mbps"):
        controller.set_link_bandwidth("sw-a", "h1", -10)


# --------------------------------------------------------------------------
# Unknown link / network-state validation
# --------------------------------------------------------------------------


def test_unknown_link_rejected(controller):
    with pytest.raises(LinkNotFoundError):
        controller.set_link_latency("h1", "h5", 20)
    with pytest.raises(LinkNotFoundError):
        controller.set_link_packet_loss("h1", "h5", 20)
    with pytest.raises(LinkNotFoundError):
        controller.set_link_bandwidth("h1", "h5", 20)
    with pytest.raises(LinkNotFoundError):
        controller.clear_link_latency("h1", "h5")


def test_unknown_node_rejected_as_unknown_link(controller):
    with pytest.raises(LinkNotFoundError):
        controller.set_link_latency("ghost", "h1", 20)


def test_impairment_requires_running_network(controller):
    controller.stop_network()
    with pytest.raises(NetworkNotRunningError):
        controller.set_link_latency("sw-a", "h1", 20)
    with pytest.raises(NetworkNotRunningError):
        controller.clear_link_bandwidth("sw-a", "h1")


# --------------------------------------------------------------------------
# Controller state consistency
# --------------------------------------------------------------------------


def test_multiple_impairments_on_same_link(controller, net):
    controller.set_link_latency("sw-a", "h1", 20)
    controller.set_link_packet_loss("sw-a", "h1", 5)
    assert controller._impairments["h1-sw-a"] == {
        "latency_ms": 20.0,
        "loss_percent": 5.0,
    }
    controller.clear_link_latency("sw-a", "h1")
    assert controller._impairments["h1-sw-a"] == {"loss_percent": 5.0}
    controller.clear_link_packet_loss("sw-a", "h1")
    assert controller._impairments == {}


def test_impairments_per_link_isolation(controller):
    controller.set_link_latency("sw-a", "h1", 20)
    controller.set_link_bandwidth("sw-b", "h4", 50)
    assert controller.impairments() == {
        "h1-sw-a": {"latency_ms": 20.0},
        "h4-sw-b": {"bandwidth_mbps": 50.0},
    }


def test_impairments_returns_copy(controller):
    controller.set_link_latency("sw-a", "h1", 20)
    snap = controller.impairments()
    snap["h1-sw-a"]["latency_ms"] = 999
    assert controller._impairments["h1-sw-a"] == {"latency_ms": 20.0}
