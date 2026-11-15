"""Unit tests for Submodule 1.3 — EventBus, Event and NetworkState models."""

from datetime import datetime

import pytest

from network.events import Event, EventBus, EventType
from network.state.network_state import (
    LINK_CONNECTED,
    LINK_DISCONNECTED,
    NETWORK_RUNNING,
    NETWORK_STOPPED,
    NODE_OFFLINE,
    NODE_ONLINE,
    LinkState,
    NetworkState,
    NodeState,
)


# --------------------------------------------------------------------------
# Event / EventType
# --------------------------------------------------------------------------


def test_event_type_values():
    assert EventType.NETWORK_STARTED.value == "NETWORK_STARTED"
    assert EventType.NETWORK_STOPPED.value == "NETWORK_STOPPED"
    assert EventType.NETWORK_RESET.value == "NETWORK_RESET"
    assert EventType.NODE_ENABLED.value == "NODE_ENABLED"
    assert EventType.NODE_DISABLED.value == "NODE_DISABLED"
    assert EventType.LINK_CONNECTED.value == "LINK_CONNECTED"
    assert EventType.LINK_DISCONNECTED.value == "LINK_DISCONNECTED"
    assert EventType.FAILURE_INJECTED.value == "FAILURE_INJECTED"
    assert EventType.FAILURE_RECOVERED.value == "FAILURE_RECOVERED"
    assert EventType.NETWORK_RESTORED.value == "NETWORK_RESTORED"
    assert EventType.LATENCY_INJECTED.value == "LATENCY_INJECTED"
    assert EventType.PACKET_LOSS_INJECTED.value == "PACKET_LOSS_INJECTED"
    assert EventType.BANDWIDTH_LIMITED.value == "BANDWIDTH_LIMITED"
    assert EventType.TELEMETRY_COLLECTED.value == "TELEMETRY_COLLECTED"
    assert EventType.TELEMETRY_SOURCE_ERROR.value == "TELEMETRY_SOURCE_ERROR"
    assert EventType.HEALTH_CHANGED.value == "HEALTH_CHANGED"
    assert EventType.FAILURE_PREDICTED.value == "FAILURE_PREDICTED"
    assert EventType.PREDICTION_CLEARED.value == "PREDICTION_CLEARED"
    for name in ("SECURITY_ANOMALY", "ACCESS_DENIED", "TRUST_CHANGED", "DEVICE_QUARANTINED", "DEVICE_RELEASED"):
        assert EventType(name).value == name
    assert len(EventType) == 23


def test_event_defaults_and_to_dict():
    event = Event(type=EventType.NODE_ENABLED, source="network.controller")
    assert event.result == "success"
    assert event.data == {}
    assert event.timestamp
    parsed = datetime.fromisoformat(event.timestamp)
    assert parsed.tzinfo is not None

    data = event.to_dict()
    assert data["type"] == "NODE_ENABLED"
    assert data["source"] == "network.controller"
    assert data["result"] == "success"
    assert data["timestamp"] == event.timestamp


def test_event_is_immutable():
    event = Event(type=EventType.NETWORK_STARTED, source="test")
    with pytest.raises(Exception):
        event.type = EventType.NETWORK_STOPPED


# --------------------------------------------------------------------------
# EventBus
# --------------------------------------------------------------------------


def test_subscribe_publish_delivers():
    bus = EventBus()
    received = []
    token = bus.subscribe(received.append)
    assert token >= 1
    event = Event(type=EventType.NETWORK_STARTED, source="test")
    assert bus.publish(event) == 1
    assert received == [event]


def test_subscribe_requires_callable():
    bus = EventBus()
    with pytest.raises(TypeError):
        bus.subscribe("not callable")


def test_unsubscribe():
    bus = EventBus()
    received = []
    token = bus.subscribe(received.append)
    assert bus.unsubscribe(token)
    assert not bus.unsubscribe(token)
    bus.publish(Event(type=EventType.NETWORK_STARTED, source="test"))
    assert received == []
    assert bus.subscriber_count == 0


def test_clear():
    bus = EventBus()
    bus.subscribe(lambda e: None)
    bus.subscribe(lambda e: None)
    bus.clear()
    assert bus.subscriber_count == 0


def test_failing_handler_does_not_block_others():
    bus = EventBus()
    received = []

    def broken(event):
        raise RuntimeError("boom")

    bus.subscribe(broken)
    bus.subscribe(received.append)
    bus.publish(Event(type=EventType.NETWORK_STARTED, source="test"))
    assert len(received) == 1


# --------------------------------------------------------------------------
# NetworkState model
# --------------------------------------------------------------------------


def test_node_state_transitions():
    node = NodeState(name="h1", kind="host")
    assert node.is_offline
    node.set_online()
    assert node.is_online
    assert node.state == NODE_ONLINE
    node.set_offline()
    assert node.state == NODE_OFFLINE
    assert node.to_dict() == {"name": "h1", "kind": "host", "state": "offline"}


def test_link_state_transitions_and_canonical_name():
    link = LinkState(src="sw-a", dst="h1")
    assert link.name == "h1-sw-a"  # canonical (sorted) pair
    assert link.is_disconnected
    link.set_connected()
    assert link.is_connected
    assert link.state == LINK_CONNECTED
    link.set_disconnected()
    assert link.state == LINK_DISCONNECTED
    assert link.to_dict() == {"src": "h1", "dst": "sw-a", "state": "disconnected"}


def test_network_state_registration_and_lookup():
    state = NetworkState()
    state.register_device("h1", "host")
    state.register_link("sw-a", "h1")

    assert state.get_node("h1").kind == "host"
    assert state.get_link("h1", "sw-a") is not None  # order-insensitive
    assert state.get_link("sw-a", "h1").name == "h1-sw-a"
    assert state.get_node("missing") is None
    assert state.get_link("a", "b") is None


def test_network_state_status_and_bulk_mark():
    state = NetworkState()
    assert state.is_stopped
    assert state.status == NETWORK_STOPPED
    state.set_running()
    assert state.is_running
    assert state.status == NETWORK_RUNNING
    state.set_stopped()
    assert state.is_stopped

    state.register_device("h1", "host")
    state.register_link("sw-a", "h1")
    state.mark_all_nodes(NODE_ONLINE)
    state.mark_all_links(LINK_CONNECTED)
    assert state.get_node("h1").is_online
    assert state.get_link("sw-a", "h1").is_connected
    state.mark_all_nodes(NODE_OFFLINE)
    state.mark_all_links(LINK_DISCONNECTED)
    assert state.get_node("h1").is_offline
    assert state.get_link("sw-a", "h1").is_disconnected


def test_network_state_unknown_lookups_raise_keyerror():
    state = NetworkState()
    with pytest.raises(KeyError):
        state.set_node_online("ghost")
    with pytest.raises(KeyError):
        state.set_link_connected("a", "b")


def test_network_state_to_dict():
    state = NetworkState()
    state.register_device("h1", "host")
    state.register_link("sw-a", "h1")
    state.set_running()
    state.mark_all_nodes(NODE_ONLINE)
    state.mark_all_links(LINK_CONNECTED)

    data = state.to_dict()
    assert data["status"] == NETWORK_RUNNING
    assert data["nodes"] == [{"name": "h1", "kind": "host", "state": "online"}]
    assert data["links"] == [{"src": "h1", "dst": "sw-a", "state": "connected"}]
