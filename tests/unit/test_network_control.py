"""Unit tests for Submodule 1.3 — NetworkController.

Runs anywhere (no root/Mininet required): the Mininet network is mocked
via FakeNet/MockBuilder; the controller logic, safety rules, state
synchronization and event publication are exercised for real.
"""

import pathlib

import pytest

from network.control import (
    LinkAlreadyConnectedError,
    LinkAlreadyDisconnectedError,
    LinkNotFoundError,
    NetworkAlreadyRunningError,
    NetworkControlError,
    NetworkNotRunningError,
    NetworkController,
    NodeAlreadyOfflineError,
    NodeAlreadyOnlineError,
    NodeNotFoundError,
)
from network.events import Event, EventType
from network.state.network_state import (
    LINK_CONNECTED,
    LINK_DISCONNECTED,
    NETWORK_RUNNING,
    NETWORK_STOPPED,
    NODE_OFFLINE,
    NODE_ONLINE,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = ROOT / "network" / "config" / "enterprise.yaml"


class FakeNode:
    def __init__(self, name, kind):
        self.name = name
        self.kind = kind
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class FakeNet:
    def __init__(self, nodes):
        self._nodes = {n.name: n for n in nodes}
        self.link_status = []

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


class MockBuilder:
    """Fake NetworkBuilder: builds the same nodes as the real config."""

    def __init__(self, config):
        self.config = config
        self.net = None
        self.cleaned = 0
        self.stopped = 0

    def start(self):
        nodes = [FakeNode(dev.name, dev.kind) for dev in self.config.devices]
        self.net = FakeNet(nodes)
        return self.net

    def stop(self):
        self.stopped += 1
        self.net = None

    def cleanup(self):
        self.cleaned += 1


@pytest.fixture()
def controller():
    from network.config import load_topology_config

    builder = MockBuilder(load_topology_config(CONFIG))
    ctrl = NetworkController(builder=builder)
    events = []
    ctrl.event_bus.subscribe(events.append)
    ctrl._received = events
    ctrl._mock_builder = builder
    return ctrl


def _types(ctrl):
    return [e.type for e in ctrl._received]


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def test_start_network(controller):
    ctrl = controller
    result = ctrl.start_network()

    assert ctrl.state.is_running
    assert result["status"] == NETWORK_RUNNING
    assert all(n["state"] == NODE_ONLINE for n in result["nodes"])
    assert all(l["state"] == LINK_CONNECTED for l in result["links"])
    assert _types(ctrl) == [EventType.NETWORK_STARTED]
    event = ctrl._received[0]
    assert event.source == "network.controller"
    assert event.result == "success"
    assert event.data == {"name": "enterprise-demo"}
    assert event.timestamp


def test_start_twice_raises(controller):
    ctrl = controller
    ctrl.start_network()
    with pytest.raises(NetworkAlreadyRunningError):
        ctrl.start_network()


def test_stop_network(controller):
    ctrl = controller
    ctrl.start_network()
    result = ctrl.stop_network()

    assert ctrl.state.is_stopped
    assert result["status"] == NETWORK_STOPPED
    assert all(n["state"] == NODE_OFFLINE for n in result["nodes"])
    assert all(l["state"] == LINK_DISCONNECTED for l in result["links"])
    assert _types(ctrl) == [EventType.NETWORK_STARTED, EventType.NETWORK_STOPPED]


def test_stop_when_not_running_raises(controller):
    with pytest.raises(NetworkNotRunningError):
        controller.stop_network()


def test_restart_network(controller):
    ctrl = controller
    ctrl.start_network()
    result = ctrl.restart_network()

    assert ctrl.state.is_running
    assert _types(ctrl) == [
        EventType.NETWORK_STARTED,
        EventType.NETWORK_STOPPED,
        EventType.NETWORK_STARTED,
    ]
    assert result["status"] == NETWORK_RUNNING


def test_restart_network_from_stopped(controller):
    result = controller.restart_network()
    assert controller.state.is_running
    assert _types(controller) == [EventType.NETWORK_STARTED]
    assert result["status"] == NETWORK_RUNNING


def test_reset_network(controller):
    ctrl = controller
    ctrl.start_network()
    result = ctrl.reset_network()

    assert ctrl.state.is_running
    assert ctrl._mock_builder.cleaned == 1
    assert ctrl._mock_builder.stopped == 1
    # reset = internal stop + start (STARTED) followed by NETWORK_RESET
    assert _types(ctrl) == [
        EventType.NETWORK_STARTED,
        EventType.NETWORK_STARTED,
        EventType.NETWORK_RESET,
    ]
    assert result["status"] == NETWORK_RUNNING


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------


def test_disable_node(controller):
    ctrl = controller
    ctrl.start_network()
    result = ctrl.disable_node("h1")

    assert result["state"] == NODE_OFFLINE
    assert ctrl.state.get_node("h1").is_offline
    assert ctrl._mock_builder.net.get("h1").stopped == 1
    assert _types(ctrl) == [
        EventType.NETWORK_STARTED,
        EventType.NODE_DISABLED,
    ]
    assert ctrl._received[-1].data == {"node": "h1"}


def test_enable_node(controller):
    ctrl = controller
    ctrl.start_network()
    ctrl.disable_node("h1")
    result = ctrl.enable_node("h1")

    assert result["state"] == NODE_ONLINE
    assert ctrl.state.get_node("h1").is_online
    assert ctrl._mock_builder.net.get("h1").started == 1
    assert _types(ctrl) == [
        EventType.NETWORK_STARTED,
        EventType.NODE_DISABLED,
        EventType.NODE_ENABLED,
    ]
    assert ctrl._received[-1].data == {"node": "h1"}


def test_disable_already_disabled_raises(controller):
    ctrl = controller
    ctrl.start_network()
    ctrl.disable_node("h1")
    with pytest.raises(NodeAlreadyOfflineError):
        ctrl.disable_node("h1")


def test_enable_already_enabled_raises(controller):
    controller.start_network()
    with pytest.raises(NodeAlreadyOnlineError):
        controller.enable_node("h1")


def test_enable_unknown_node_raises(controller):
    controller.start_network()
    with pytest.raises(NodeNotFoundError):
        controller.enable_node("ghost")


def test_disable_unknown_node_raises(controller):
    controller.start_network()
    with pytest.raises(NodeNotFoundError):
        controller.disable_node("ghost")


def test_router_and_switch_nodes_controllable(controller):
    ctrl = controller
    ctrl.start_network()
    ctrl.disable_node("rtr-core")
    ctrl.disable_node("sw-a")
    assert ctrl.state.get_node("rtr-core").is_offline
    assert ctrl.state.get_node("sw-a").is_offline
    ctrl.enable_node("rtr-core")
    assert ctrl.state.get_node("rtr-core").is_online


def test_node_operation_when_stopped_raises(controller):
    with pytest.raises(NetworkNotRunningError):
        controller.disable_node("h1")
    with pytest.raises(NetworkNotRunningError):
        controller.enable_node("h1")


# --------------------------------------------------------------------------
# Links
# --------------------------------------------------------------------------


def test_disconnect_link(controller):
    ctrl = controller
    ctrl.start_network()
    result = ctrl.disconnect_link("sw-a", "h1")

    assert result["state"] == LINK_DISCONNECTED
    assert ctrl.state.get_link("sw-a", "h1").is_disconnected
    assert ctrl._mock_builder.net.link_status == [("sw-a", "h1", "down")]
    assert _types(ctrl) == [
        EventType.NETWORK_STARTED,
        EventType.LINK_DISCONNECTED,
    ]
    assert ctrl._received[-1].data == {"src": "sw-a", "dst": "h1"}


def test_connect_link(controller):
    ctrl = controller
    ctrl.start_network()
    ctrl.disconnect_link("sw-a", "h1")
    result = ctrl.connect_link("sw-a", "h1")

    assert result["state"] == LINK_CONNECTED
    assert ctrl.state.get_link("sw-a", "h1").is_connected
    assert ctrl._mock_builder.net.link_status == [
        ("sw-a", "h1", "down"),
        ("sw-a", "h1", "up"),
    ]
    assert _types(ctrl) == [
        EventType.NETWORK_STARTED,
        EventType.LINK_DISCONNECTED,
        EventType.LINK_CONNECTED,
    ]


def test_disconnect_already_disconnected_raises(controller):
    ctrl = controller
    ctrl.start_network()
    ctrl.disconnect_link("sw-a", "h1")
    with pytest.raises(LinkAlreadyDisconnectedError):
        ctrl.disconnect_link("sw-a", "h1")


def test_connect_already_connected_raises(controller):
    controller.start_network()
    with pytest.raises(LinkAlreadyConnectedError):
        controller.connect_link("sw-a", "h1")


def test_link_order_insensitive(controller):
    ctrl = controller
    ctrl.start_network()
    ctrl.disconnect_link("h1", "sw-a")  # reversed order
    assert ctrl.state.get_link("h1", "sw-a").is_disconnected


def test_link_unknown_pair_raises(controller):
    controller.start_network()
    with pytest.raises(LinkNotFoundError):
        controller.disconnect_link("h1", "h5")
    with pytest.raises(LinkNotFoundError):
        controller.connect_link("h1", "h5")


def test_link_operation_when_stopped_raises(controller):
    with pytest.raises(NetworkNotRunningError):
        controller.disconnect_link("sw-a", "h1")
    with pytest.raises(NetworkNotRunningError):
        controller.connect_link("sw-a", "h1")


# --------------------------------------------------------------------------
# Introspection
# --------------------------------------------------------------------------


def test_network_status_structure(controller):
    ctrl = controller
    ctrl.start_network()
    status = ctrl.network_status()

    assert status["status"] == NETWORK_RUNNING
    assert status["timestamp"]
    assert len(status["nodes"]) == 10
    assert len(status["links"]) == 9
    assert all(n["state"] == NODE_ONLINE for n in status["nodes"])


def test_topology_snapshot_structure(controller):
    ctrl = controller
    ctrl.start_network()
    snap = ctrl.topology_snapshot()

    assert snap["name"] == "enterprise-demo"
    assert snap["status"] == NETWORK_RUNNING
    assert len(snap["segments"]) == 1
    assert len(snap["segments"][0]["subnets"]) == 2
    assert len(snap["devices"]) == 10
    assert len(snap["links"]) == 9
    router = next(d for d in snap["devices"] if d["name"] == "rtr-core")
    assert router["kind"] == "router"
    assert router["datapath_id"] == "0000000000000001"
    assert router["state"] == NODE_ONLINE
    assert len(router["interfaces"]) == 2


def test_topology_snapshot_when_stopped(controller):
    snap = controller.topology_snapshot()
    assert snap["status"] == NETWORK_STOPPED
    assert all(d["state"] == NODE_OFFLINE for d in snap["devices"])


def test_state_sync_after_mixed_operations(controller):
    ctrl = controller
    ctrl.start_network()
    ctrl.disable_node("h1")
    ctrl.disconnect_link("sw-a", "h1")

    status = ctrl.network_status()
    node = next(n for n in status["nodes"] if n["name"] == "h1")
    link = next(l for l in status["links"] if l["src"] == "h1" or l["dst"] == "h1")
    assert node["state"] == NODE_OFFLINE
    assert link["state"] == LINK_DISCONNECTED
    assert ctrl.state.get_node("h2").is_online
    assert ctrl.state.get_link("sw-a", "h2").is_connected


def test_safety_exceptions_share_base_class(controller):
    controller.start_network()
    with pytest.raises(NetworkControlError):
        controller.enable_node("h1")  # already online


# --------------------------------------------------------------------------
# Event system
# --------------------------------------------------------------------------


def test_multiple_subscribers_receive_events(controller):
    ctrl = controller
    other = []
    ctrl.event_bus.subscribe(other.append)
    ctrl.start_network()
    assert len(ctrl._received) == 1
    assert other == ctrl._received


def test_events_are_event_instances(controller):
    ctrl = controller
    ctrl.start_network()
    event = ctrl._received[0]
    assert isinstance(event, Event)
    assert event.type == EventType.NETWORK_STARTED
    assert event.to_dict()["type"] == "NETWORK_STARTED"


def test_unsubscribe_stops_delivery(controller):
    ctrl = controller
    before = ctrl.event_bus.subscriber_count  # fixture subscriber
    token = ctrl.event_bus.subscribe(lambda e: None)
    assert ctrl.event_bus.subscriber_count == before + 1
    assert ctrl.event_bus.unsubscribe(token)
    assert ctrl.event_bus.subscriber_count == before


# --------------------------------------------------------------------------
# Backward compatibility (Module 1.1 / 1.2)
# --------------------------------------------------------------------------


def test_package_exports_preserved():
    import network

    for name in (
        "ConfigError",
        "EnterpriseTopology",
        "Host",
        "NetworkBuilder",
        "Router",
        "Server",
        "Switch",
        "TopologyConfig",
        "load_topology_config",
    ):
        assert hasattr(network, name), f"missing export: {name}"


def test_builder_cleanup_method_exists():
    from network import NetworkBuilder

    assert callable(getattr(NetworkBuilder, "cleanup"))
    assert callable(getattr(NetworkBuilder, "build"))
    assert callable(getattr(NetworkBuilder, "start"))
    assert callable(getattr(NetworkBuilder, "stop"))
