"""Unit tests for Submodule 1.4B.1 — FailureManager (router/switch core).

Runs anywhere (no root/Mininet required): the Mininet network is mocked
via FakeNet/MockBuilder; the FailureManager, NetworkController, registry
and event publication are exercised for real.
"""

import logging
import pathlib
from types import SimpleNamespace

import pytest

from network.control import NetworkController, NetworkNotRunningError
from network.events import EventType
from network.failure import (
    BandwidthFailure,
    DuplicateFailureError,
    FailureError,
    FailureManager,
    FailureNotFoundError,
    FailureState,
    FailureType,
    InvalidBandwidthError,
    InvalidLatencyError,
    InvalidPacketLossError,
    LatencyFailure,
    LinkFailure,
    PacketLossFailure,
    RouterFailure,
    SwitchFailure,
    UnknownLinkError,
    UnknownNodeError,
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
    """Fake NetworkBuilder: builds the same nodes as the real config."""

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
    return NetworkController(builder=builder)


@pytest.fixture()
def env(controller):
    """Running network + FailureManager + event collector."""
    controller.start_network()
    manager = FailureManager(controller)
    events = []
    controller.event_bus.subscribe(events.append)
    return SimpleNamespace(controller=controller, manager=manager, events=events)


def _types(env):
    return [e.type for e in env.events]


# --------------------------------------------------------------------------
# Router failure injection
# --------------------------------------------------------------------------


def test_inject_router_failure(env):
    failure = env.manager.inject_router_failure("rtr-core")

    assert isinstance(failure, RouterFailure)
    assert failure.failure_type == FailureType.ROUTER
    assert failure.state == FailureState.ACTIVE
    assert failure.router == "rtr-core"
    assert failure.affected_nodes == ["rtr-core"]
    assert failure.injected_by == "FailureManager"
    assert env.controller.state.get_node("rtr-core").is_offline
    assert env.manager.active_failures() == [failure]


def test_inject_router_unknown_node_raises(env):
    with pytest.raises(UnknownNodeError):
        env.manager.inject_router_failure("ghost")


def test_inject_router_on_non_router_raises(env):
    with pytest.raises(FailureError):
        env.manager.inject_router_failure("h1")
    with pytest.raises(FailureError):
        env.manager.inject_router_failure("sw-a")


def test_inject_router_requires_running_network(controller):
    manager = FailureManager(controller)
    with pytest.raises(NetworkNotRunningError):
        manager.inject_router_failure("rtr-core")


# --------------------------------------------------------------------------
# Router recovery
# --------------------------------------------------------------------------


def test_recover_router(env):
    env.manager.inject_router_failure("rtr-core")
    failure = env.manager.recover_router("rtr-core")

    assert failure.state == FailureState.RECOVERED
    assert failure.recovered_at is not None
    assert env.controller.state.get_node("rtr-core").is_online
    assert env.manager.active_failures() == []


def test_recover_healthy_router_raises(env):
    with pytest.raises(FailureNotFoundError):
        env.manager.recover_router("rtr-core")


def test_recover_router_unknown_node_raises(env):
    with pytest.raises(UnknownNodeError):
        env.manager.recover_router("ghost")


def test_recover_router_on_non_router_raises(env):
    with pytest.raises(FailureError):
        env.manager.recover_router("sw-a")


# --------------------------------------------------------------------------
# Switch failure injection
# --------------------------------------------------------------------------


def test_inject_switch_failure(env):
    failure = env.manager.inject_switch_failure("sw-a")

    assert isinstance(failure, SwitchFailure)
    assert failure.failure_type == FailureType.SWITCH
    assert failure.state == FailureState.ACTIVE
    assert failure.switch == "sw-a"
    assert failure.affected_nodes == ["sw-a"]
    assert env.controller.state.get_node("sw-a").is_offline
    assert env.manager.active_failures() == [failure]


def test_inject_switch_unknown_node_raises(env):
    with pytest.raises(UnknownNodeError):
        env.manager.inject_switch_failure("ghost")


def test_inject_switch_on_non_switch_raises(env):
    with pytest.raises(FailureError):
        env.manager.inject_switch_failure("rtr-core")
    with pytest.raises(FailureError):
        env.manager.inject_switch_failure("h1")


# --------------------------------------------------------------------------
# Switch recovery
# --------------------------------------------------------------------------


def test_recover_switch(env):
    env.manager.inject_switch_failure("sw-a")
    failure = env.manager.recover_switch("sw-a")

    assert failure.state == FailureState.RECOVERED
    assert failure.recovered_at is not None
    assert env.controller.state.get_node("sw-a").is_online
    assert env.manager.active_failures() == []


def test_recover_healthy_switch_raises(env):
    with pytest.raises(FailureNotFoundError):
        env.manager.recover_switch("sw-a")


def test_recover_switch_on_non_switch_raises(env):
    with pytest.raises(FailureError):
        env.manager.recover_switch("rtr-core")


# --------------------------------------------------------------------------
# Duplicate failure prevention
# --------------------------------------------------------------------------


def test_duplicate_router_failure_prevented(env):
    env.manager.inject_router_failure("rtr-core")
    with pytest.raises(DuplicateFailureError):
        env.manager.inject_router_failure("rtr-core")


def test_duplicate_switch_failure_prevented(env):
    env.manager.inject_switch_failure("sw-a")
    with pytest.raises(DuplicateFailureError):
        env.manager.inject_switch_failure("sw-a")


def test_duplicate_allowed_after_recovery(env):
    env.manager.inject_router_failure("rtr-core")
    env.manager.recover_router("rtr-core")
    failure = env.manager.inject_router_failure("rtr-core")
    assert failure.state == FailureState.ACTIVE


def test_independent_devices_not_duplicates(env):
    env.manager.inject_router_failure("rtr-core")
    env.manager.inject_switch_failure("sw-a")
    env.manager.inject_switch_failure("sw-b")
    assert len(env.manager.active_failures()) == 3


# --------------------------------------------------------------------------
# Registry integration
# --------------------------------------------------------------------------


def test_registry_integration(env):
    failure = env.manager.inject_router_failure("rtr-core")

    assert env.manager._registry.get(failure.id) is failure
    assert len(env.manager._registry.history()) == 1
    assert env.manager._registry.affected_devices() == {"rtr-core"}

    env.manager.recover_router("rtr-core")
    assert env.manager._registry.recovered_failures() == [failure]
    assert env.manager.active_failures() == []
    assert len(env.manager._registry.history()) == 1


def test_registry_tracks_affected_devices(env):
    env.manager.inject_router_failure("rtr-core")
    env.manager.inject_switch_failure("sw-a")
    env.manager.inject_switch_failure("sw-b")
    assert env.manager._registry.affected_devices() == {"rtr-core", "sw-a", "sw-b"}


# --------------------------------------------------------------------------
# Event publication
# --------------------------------------------------------------------------


def test_events_on_inject_router(env):
    env.manager.inject_router_failure("rtr-core")
    assert _types(env) == [
        EventType.NODE_DISABLED,
        EventType.FAILURE_INJECTED,
    ]


def test_events_on_recover_router(env):
    env.manager.inject_router_failure("rtr-core")
    env.manager.recover_router("rtr-core")
    assert _types(env) == [
        EventType.NODE_DISABLED,
        EventType.FAILURE_INJECTED,
        EventType.NODE_ENABLED,
        EventType.FAILURE_RECOVERED,
    ]


def test_events_on_inject_switch(env):
    env.manager.inject_switch_failure("sw-a")
    assert _types(env) == [
        EventType.NODE_DISABLED,
        EventType.FAILURE_INJECTED,
    ]


def test_events_on_recover_switch(env):
    env.manager.inject_switch_failure("sw-a")
    env.manager.recover_switch("sw-a")
    assert _types(env) == [
        EventType.NODE_DISABLED,
        EventType.FAILURE_INJECTED,
        EventType.NODE_ENABLED,
        EventType.FAILURE_RECOVERED,
    ]


def test_inject_event_payload(env):
    env.manager.inject_switch_failure("sw-a")
    event = env.events[-1]
    assert event.type == EventType.FAILURE_INJECTED
    assert event.source == "failure.manager"
    assert event.result == "success"
    assert event.data["node"] == "sw-a"
    assert "failure_id" in event.data


def test_recover_event_payload(env):
    env.manager.inject_switch_failure("sw-a")
    env.manager.recover_switch("sw-a")
    event = env.events[-1]
    assert event.type == EventType.FAILURE_RECOVERED
    assert event.source == "failure.manager"
    assert event.result == "success"
    assert event.data["node"] == "sw-a"
    assert "failure_id" in event.data


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------


def test_injection_and_recovery_are_logged(controller):
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("failure.manager.test")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    controller.start_network()
    manager = FailureManager(controller, logger=logger)

    manager.inject_router_failure("rtr-core")
    assert len(records) == 1
    injected = records[0]
    assert injected.getMessage() == "Router failure injected on rtr-core"
    assert injected.event == EventType.FAILURE_INJECTED.value
    assert injected.result == "success"

    manager.recover_router("rtr-core")
    assert len(records) == 2
    recovered = records[1]
    assert recovered.getMessage() == "Router failure recovered on rtr-core"
    assert recovered.event == EventType.FAILURE_RECOVERED.value
    assert recovered.result == "success"


# --------------------------------------------------------------------------
# Link failure injection
# --------------------------------------------------------------------------


def test_disconnect_link(env):
    failure = env.manager.disconnect_link("sw-a", "h1")

    assert isinstance(failure, LinkFailure)
    assert failure.failure_type == FailureType.LINK
    assert failure.state == FailureState.ACTIVE
    assert {failure.src, failure.dst} == {"sw-a", "h1"}
    assert failure.injected_by == "FailureManager"
    assert env.controller.state.get_link("sw-a", "h1").is_disconnected
    assert env.manager.active_failures() == [failure]


def test_disconnect_link_order_insensitive(env):
    failure = env.manager.disconnect_link("h1", "sw-a")
    assert failure.link == "h1-sw-a"
    assert env.controller.state.get_link("h1", "sw-a").is_disconnected


def test_disconnect_unknown_link_raises(env):
    with pytest.raises(UnknownLinkError):
        env.manager.disconnect_link("h1", "h5")


def test_disconnect_self_link_raises(env):
    with pytest.raises(UnknownLinkError):
        env.manager.disconnect_link("h1", "h1")


def test_disconnect_requires_running_network(controller):
    manager = FailureManager(controller)
    with pytest.raises(NetworkNotRunningError):
        manager.disconnect_link("sw-a", "h1")


# --------------------------------------------------------------------------
# Link recovery
# --------------------------------------------------------------------------


def test_reconnect_link(env):
    env.manager.disconnect_link("sw-a", "h1")
    failure = env.manager.reconnect_link("sw-a", "h1")

    assert failure.state == FailureState.RECOVERED
    assert failure.recovered_at is not None
    assert env.controller.state.get_link("sw-a", "h1").is_connected
    assert env.manager.active_failures() == []


def test_reconnect_healthy_link_raises(env):
    with pytest.raises(FailureNotFoundError):
        env.manager.reconnect_link("sw-a", "h1")


def test_reconnect_unknown_link_raises(env):
    with pytest.raises(UnknownLinkError):
        env.manager.reconnect_link("h1", "h5")


# --------------------------------------------------------------------------
# Duplicate prevention
# --------------------------------------------------------------------------


def test_duplicate_link_failure_prevented(env):
    env.manager.disconnect_link("sw-a", "h1")
    with pytest.raises(DuplicateFailureError):
        env.manager.disconnect_link("h1", "sw-a")  # reversed order is duplicate


def test_duplicate_link_allowed_after_reconnect(env):
    env.manager.disconnect_link("sw-a", "h1")
    env.manager.reconnect_link("sw-a", "h1")
    failure = env.manager.disconnect_link("sw-a", "h1")
    assert failure.state == FailureState.ACTIVE


# --------------------------------------------------------------------------
# Registry integration
# --------------------------------------------------------------------------


def test_link_registry_integration(env):
    failure = env.manager.disconnect_link("sw-a", "h1")

    assert env.manager._registry.get(failure.id) is failure
    assert env.manager._registry.affected_devices() == {"sw-a", "h1"}
    assert len(env.manager._registry.history()) == 1

    env.manager.reconnect_link("sw-a", "h1")
    assert env.manager._registry.recovered_failures() == [failure]
    assert env.manager.active_failures() == []


def test_mixed_failures_tracked(env):
    env.manager.inject_router_failure("rtr-core")
    env.manager.inject_switch_failure("sw-a")
    env.manager.disconnect_link("sw-b", "h4")

    assert len(env.manager.active_failures()) == 3
    assert len(env.manager._registry.history()) == 3
    assert env.manager._registry.affected_devices() == {"rtr-core", "sw-a", "sw-b", "h4"}


# --------------------------------------------------------------------------
# reset_all_failures
# --------------------------------------------------------------------------


def test_reset_all_failures(env):
    env.manager.inject_router_failure("rtr-core")
    env.manager.inject_switch_failure("sw-a")
    env.manager.disconnect_link("sw-b", "h4")

    result = env.manager.reset_all_failures()

    assert all(f.state == FailureState.RECOVERED for f in result)
    assert env.manager.active_failures() == []
    assert env.manager._registry.recovered_failures() == result
    assert env.controller.state.get_node("rtr-core").is_online
    assert env.controller.state.get_node("sw-a").is_online
    assert env.controller.state.get_link("sw-b", "h4").is_connected
    assert len(env.manager._registry.history()) == 3


def test_reset_all_with_no_failures(env):
    result = env.manager.reset_all_failures()
    assert result == []
    assert env.events[-1].type == EventType.NETWORK_RESTORED


# --------------------------------------------------------------------------
# Event publication (links + restore)
# --------------------------------------------------------------------------


def test_events_on_disconnect_link(env):
    env.manager.disconnect_link("sw-a", "h1")
    assert _types(env) == [EventType.LINK_DISCONNECTED]


def test_events_on_reconnect_link(env):
    env.manager.disconnect_link("sw-a", "h1")
    env.manager.reconnect_link("sw-a", "h1")
    assert _types(env) == [
        EventType.LINK_DISCONNECTED,
        EventType.LINK_CONNECTED,
    ]


def test_link_ops_do_not_emit_failure_events(env):
    env.manager.disconnect_link("sw-a", "h1")
    assert not any(e.type == EventType.FAILURE_INJECTED for e in env.events)


def test_events_on_reset_all(env):
    env.manager.inject_router_failure("rtr-core")
    env.manager.disconnect_link("sw-a", "h1")
    env.manager.reset_all_failures()

    assert _types(env)[-1] == EventType.NETWORK_RESTORED
    assert EventType.FAILURE_RECOVERED in _types(env)
    assert EventType.NODE_ENABLED in _types(env)
    assert EventType.LINK_CONNECTED in _types(env)


def test_restore_event_payload(env):
    env.manager.inject_router_failure("rtr-core")
    env.manager.disconnect_link("sw-a", "h1")
    env.manager.reset_all_failures()
    event = env.events[-1]
    assert event.type == EventType.NETWORK_RESTORED
    assert event.source == "failure.manager"
    assert event.data == {"recovered": 2}


# --------------------------------------------------------------------------
# Logging (links + restore)
# --------------------------------------------------------------------------


def test_link_ops_are_logged(controller):
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("failure.manager.linktest")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    controller.start_network()
    manager = FailureManager(controller, logger=logger)

    manager.disconnect_link("sw-a", "h1")
    assert records[0].getMessage() == "Link h1-sw-a disconnected"
    assert records[0].event == EventType.LINK_DISCONNECTED.value
    assert records[0].result == "success"

    manager.reconnect_link("sw-a", "h1")
    assert records[1].getMessage() == "Link h1-sw-a reconnected"
    assert records[1].event == EventType.LINK_CONNECTED.value

    manager.inject_router_failure("rtr-core")
    manager.reset_all_failures()
    assert records[-1].getMessage() == "All 1 failures recovered"
    assert records[-1].event == EventType.NETWORK_RESTORED.value


# --------------------------------------------------------------------------
# Latency impairment
# --------------------------------------------------------------------------


def test_inject_latency(env):
    failure = env.manager.inject_latency("sw-a", "h1", 50)
    assert isinstance(failure, LatencyFailure)
    assert failure.failure_type == FailureType.LATENCY
    assert failure.state == FailureState.ACTIVE
    assert failure.latency_ms == 50
    assert failure.node in ("sw-a", "h1")
    assert env.controller._impairments["h1-sw-a"] == {"latency_ms": 50.0}
    assert env.manager.active_failures() == [failure]


def test_inject_latency_order_insensitive(env):
    env.manager.inject_latency("h1", "sw-a", 10)
    assert "h1-sw-a" in env.controller._impairments


def test_remove_latency(env):
    env.manager.inject_latency("sw-a", "h1", 50)
    failure = env.manager.remove_latency("sw-a", "h1")
    assert failure.state == FailureState.RECOVERED
    assert failure.recovered_at is not None
    assert env.controller._impairments == {}
    assert env.manager.active_failures() == []


def test_remove_latency_not_active_raises(env):
    with pytest.raises(FailureNotFoundError):
        env.manager.remove_latency("sw-a", "h1")


def test_remove_unknown_link_latency_raises(env):
    with pytest.raises(UnknownLinkError):
        env.manager.remove_latency("h1", "h5")


def test_negative_latency_rejected(env):
    with pytest.raises(InvalidLatencyError):
        env.manager.inject_latency("sw-a", "h1", -1)


def test_duplicate_latency_prevented(env):
    env.manager.inject_latency("sw-a", "h1", 50)
    with pytest.raises(DuplicateFailureError):
        env.manager.inject_latency("sw-a", "h1", 100)


def test_duplicate_latency_allowed_after_removal(env):
    env.manager.inject_latency("sw-a", "h1", 50)
    env.manager.remove_latency("sw-a", "h1")
    failure = env.manager.inject_latency("sw-a", "h1", 100)
    assert failure.state == FailureState.ACTIVE


# --------------------------------------------------------------------------
# Packet loss impairment
# --------------------------------------------------------------------------


def test_inject_packet_loss(env):
    env.manager.inject_packet_loss("sw-a", "h1", 25)
    assert env.controller._impairments["h1-sw-a"] == {"loss_percent": 25.0}
    assert env.manager.active_failures()[0].failure_type == FailureType.PACKET_LOSS


def test_remove_packet_loss(env):
    env.manager.inject_packet_loss("sw-a", "h1", 25)
    failure = env.manager.remove_packet_loss("sw-a", "h1")
    assert failure.state == FailureState.RECOVERED
    assert env.controller._impairments == {}


def test_remove_packet_loss_not_active_raises(env):
    with pytest.raises(FailureNotFoundError):
        env.manager.remove_packet_loss("sw-a", "h1")


def test_invalid_packet_loss_rejected(env):
    with pytest.raises(InvalidPacketLossError):
        env.manager.inject_packet_loss("sw-a", "h1", -1)
    with pytest.raises(InvalidPacketLossError):
        env.manager.inject_packet_loss("sw-a", "h1", 101)


def test_duplicate_packet_loss_prevented(env):
    env.manager.inject_packet_loss("sw-a", "h1", 25)
    with pytest.raises(DuplicateFailureError):
        env.manager.inject_packet_loss("sw-a", "h1", 50)


# --------------------------------------------------------------------------
# Bandwidth impairment
# --------------------------------------------------------------------------


def test_limit_bandwidth(env):
    env.manager.limit_bandwidth("sw-a", "h1", 100)
    assert env.controller._impairments["h1-sw-a"] == {"bandwidth_mbps": 100.0}
    assert (
        env.manager.active_failures()[0].failure_type == FailureType.BANDWIDTH
    )


def test_remove_bandwidth(env):
    env.manager.limit_bandwidth("sw-a", "h1", 100)
    failure = env.manager.remove_bandwidth_limit("sw-a", "h1")
    assert failure.state == FailureState.RECOVERED
    assert env.controller._impairments == {}


def test_remove_bandwidth_not_active_raises(env):
    with pytest.raises(FailureNotFoundError):
        env.manager.remove_bandwidth_limit("sw-a", "h1")


def test_invalid_bandwidth_rejected(env):
    with pytest.raises(InvalidBandwidthError):
        env.manager.limit_bandwidth("sw-a", "h1", 0)
    with pytest.raises(InvalidBandwidthError):
        env.manager.limit_bandwidth("sw-a", "h1", -10)


def test_duplicate_bandwidth_prevented(env):
    env.manager.limit_bandwidth("sw-a", "h1", 100)
    with pytest.raises(DuplicateFailureError):
        env.manager.limit_bandwidth("sw-a", "h1", 50)


# --------------------------------------------------------------------------
# Registry integration
# --------------------------------------------------------------------------


def test_impairment_registry_integration(env):
    failure = env.manager.inject_latency("sw-a", "h1", 50)
    assert env.manager._registry.get(failure.id) is failure
    assert env.manager._registry.affected_devices() == {"sw-a"}
    env.manager.remove_latency("sw-a", "h1")
    assert env.manager._registry.recovered_failures() == [failure]


def test_mixed_impairments_tracked(env):
    env.manager.inject_latency("sw-a", "h1", 50)
    env.manager.inject_packet_loss("sw-b", "h4", 10)
    env.manager.limit_bandwidth("sw-a", "h1", 100)
    assert len(env.manager.active_failures()) == 3


# --------------------------------------------------------------------------
# Event publication
# --------------------------------------------------------------------------


def test_latency_events(env):
    env.manager.inject_latency("sw-a", "h1", 50)
    assert _types(env)[-1] == EventType.LATENCY_INJECTED
    env.manager.remove_latency("sw-a", "h1")
    assert _types(env)[-1] == EventType.FAILURE_RECOVERED


def test_packet_loss_events(env):
    env.manager.inject_packet_loss("sw-a", "h1", 25)
    assert _types(env)[-1] == EventType.PACKET_LOSS_INJECTED
    env.manager.remove_packet_loss("sw-a", "h1")
    assert _types(env)[-1] == EventType.FAILURE_RECOVERED


def test_bandwidth_events(env):
    env.manager.limit_bandwidth("sw-a", "h1", 100)
    assert _types(env)[-1] == EventType.BANDWIDTH_LIMITED
    env.manager.remove_bandwidth_limit("sw-a", "h1")
    assert _types(env)[-1] == EventType.FAILURE_RECOVERED


def test_latency_event_payload(env):
    env.manager.inject_latency("sw-a", "h1", 50)
    event = env.events[-1]
    assert event.type == EventType.LATENCY_INJECTED
    assert event.source == "failure.manager"
    assert event.result == "success"
    assert "failure_id" in event.data


def test_bandwidth_event_payload(env):
    env.manager.limit_bandwidth("sw-a", "h1", 100)
    event = env.events[-1]
    assert event.type == EventType.BANDWIDTH_LIMITED
    assert event.source == "failure.manager"


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------


def test_latency_logging(controller):
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("failure.manager.latency")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    controller.start_network()
    manager = FailureManager(controller, logger=logger)

    manager.inject_latency("sw-a", "h1", 50)
    assert records[0].getMessage() == "Latency 50 ms injected on sw-a-h1"
    assert records[0].event == EventType.LATENCY_INJECTED.value
    assert records[0].result == "success"

    manager.remove_latency("sw-a", "h1")
    assert records[1].getMessage() == "Latency impairment removed on sw-a-h1"
    assert records[1].event == EventType.FAILURE_RECOVERED.value


def test_packet_loss_logging(controller):
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("failure.manager.pl")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    controller.start_network()
    manager = FailureManager(controller, logger=logger)

    manager.inject_packet_loss("sw-a", "h1", 25)
    assert records[0].getMessage() == "Packet loss 25% injected on sw-a-h1"
    assert records[0].event == EventType.PACKET_LOSS_INJECTED.value

    manager.remove_packet_loss("sw-a", "h1")
    assert records[1].getMessage() == "Packet-loss impairment removed on sw-a-h1"
    assert records[1].event == EventType.FAILURE_RECOVERED.value


def test_bandwidth_logging(controller):
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("failure.manager.bw")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    controller.start_network()
    manager = FailureManager(controller, logger=logger)

    manager.limit_bandwidth("sw-a", "h1", 100)
    assert records[0].getMessage() == (
        "Bandwidth limited to 100 Mbps on sw-a-h1"
    )
    assert records[0].event == EventType.BANDWIDTH_LIMITED.value

    manager.remove_bandwidth_limit("sw-a", "h1")
    assert records[1].getMessage() == (
        "Bandwidth impairment removed on sw-a-h1"
    )
    assert records[1].event == EventType.FAILURE_RECOVERED.value
