"""Unit tests for Submodule 1.4A — Failure Models & Registry.

Cover failure creation, registry behaviour and exception handling. The
failure package itself depends only on the standard library; run with the
rest of the suite in the mininet-net container (`make net-test`).
"""

from datetime import datetime

import pytest

from network.failure import (
    BandwidthFailure,
    DuplicateFailureError,
    FailureError,
    FailureNotFoundError,
    FailureRegistry,
    FailureState,
    FailureType,
    InvalidBandwidthError,
    InvalidLatencyError,
    InvalidPacketLossError,
    LatencyFailure,
    LinkFailure,
    PacketLossFailure,
    RouterFailure,
    Severity,
    SwitchFailure,
    UnknownLinkError,
    UnknownNodeError,
)


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


def test_failure_state_values():
    assert FailureState.ACTIVE.value == "ACTIVE"
    assert FailureState.RECOVERED.value == "RECOVERED"
    assert len(FailureState) == 2


def test_severity_values():
    assert [s.value for s in Severity] == ["LOW", "MEDIUM", "HIGH", "CRITICAL"]


def test_failure_type_values():
    assert [t.value for t in FailureType] == [
        "ROUTER",
        "SWITCH",
        "LINK",
        "LATENCY",
        "PACKET_LOSS",
        "BANDWIDTH",
    ]


# --------------------------------------------------------------------------
# Failure creation
# --------------------------------------------------------------------------


def test_router_failure_defaults():
    failure = RouterFailure(router="rtr-core")
    assert failure.failure_type == FailureType.ROUTER
    assert failure.affected_nodes == ["rtr-core"]
    assert failure.state == FailureState.ACTIVE
    assert failure.recovered_at is None
    assert failure.id
    assert failure.timestamp
    parsed = datetime.fromisoformat(failure.timestamp)
    assert parsed.tzinfo is not None


def test_failure_ids_are_unique():
    a = RouterFailure(router="rtr-core")
    b = RouterFailure(router="rtr-core")
    assert a.id != b.id


def test_switch_failure():
    failure = SwitchFailure(switch="sw-a")
    assert failure.failure_type == FailureType.SWITCH
    assert failure.affected_nodes == ["sw-a"]


def test_link_failure():
    failure = LinkFailure(src="sw-a", dst="h1")
    assert failure.failure_type == FailureType.LINK
    assert failure.affected_nodes == ["sw-a", "h1"]
    assert failure.link == "h1-sw-a"  # canonical (sorted) name


def test_latency_failure():
    failure = LatencyFailure(node="h1", latency_ms=150)
    assert failure.failure_type == FailureType.LATENCY
    assert failure.affected_nodes == ["h1"]
    assert failure.latency_ms == 150


def test_packet_loss_failure():
    failure = PacketLossFailure(node="h1", loss_percent=25)
    assert failure.failure_type == FailureType.PACKET_LOSS
    assert failure.affected_nodes == ["h1"]
    assert failure.loss_percent == 25


def test_bandwidth_failure():
    failure = BandwidthFailure(node="h1", bandwidth_bps=1_000_000)
    assert failure.failure_type == FailureType.BANDWIDTH
    assert failure.affected_nodes == ["h1"]
    assert failure.bandwidth_bps == 1_000_000


def test_severity_injected_by_and_metadata():
    failure = RouterFailure(
        router="rtr-core",
        severity=Severity.CRITICAL,
        injected_by="tester",
        metadata={"scenario": "crash"},
    )
    assert failure.severity == Severity.CRITICAL
    assert failure.injected_by == "tester"
    assert failure.metadata == {"scenario": "crash"}


def test_explicit_affected_nodes_respected():
    failure = RouterFailure(router="rtr-core", affected_nodes=["rtr-core", "sw-a"])
    assert failure.affected_nodes == ["rtr-core", "sw-a"]


def test_to_dict_merges_type_specific_fields():
    failure = LatencyFailure(node="h1", latency_ms=150, severity=Severity.HIGH)
    data = failure.to_dict()
    assert data["failure_type"] == "LATENCY"
    assert data["id"] == failure.id
    assert data["timestamp"] == failure.timestamp
    assert data["severity"] == "HIGH"
    assert data["affected_nodes"] == ["h1"]
    assert data["state"] == "ACTIVE"
    assert data["recovered_at"] is None
    assert data["latency_ms"] == 150


def test_link_failure_to_dict_has_endpoints():
    data = LinkFailure(src="sw-a", dst="h1").to_dict()
    assert data["src"] == "sw-a"
    assert data["dst"] == "h1"


# --------------------------------------------------------------------------
# Registry behaviour
# --------------------------------------------------------------------------


def test_register_and_get():
    registry = FailureRegistry()
    failure = RouterFailure(router="rtr-core")
    assert registry.register(failure) is failure
    assert registry.get(failure.id) is failure


def test_register_duplicate_id_raises():
    registry = FailureRegistry()
    registry.register(RouterFailure(router="rtr-core", id="dup"))
    with pytest.raises(DuplicateFailureError):
        registry.register(SwitchFailure(switch="sw-a", id="dup"))


def test_register_requires_failure_instance():
    registry = FailureRegistry()
    with pytest.raises(TypeError):
        registry.register({"not": "a failure"})


def test_get_missing_returns_none():
    registry = FailureRegistry()
    assert registry.get("ghost") is None


def test_mark_recovered():
    registry = FailureRegistry()
    failure = registry.register(RouterFailure(router="rtr-core"))
    result = registry.mark_recovered(failure.id)
    assert result.state == FailureState.RECOVERED
    assert result.recovered_at is not None


def test_mark_recovered_unknown_raises():
    registry = FailureRegistry()
    with pytest.raises(FailureNotFoundError):
        registry.mark_recovered("missing")


def test_mark_recovered_custom_timestamp():
    registry = FailureRegistry()
    failure = registry.register(RouterFailure(router="rtr-core"))
    registry.mark_recovered(failure.id, recovered_at="2024-01-01T00:00:00+00:00")
    assert failure.recovered_at == "2024-01-01T00:00:00+00:00"


def test_mark_recovered_idempotent():
    registry = FailureRegistry()
    failure = registry.register(RouterFailure(router="rtr-core"))
    first = registry.mark_recovered(failure.id).recovered_at
    second = registry.mark_recovered(failure.id).recovered_at
    assert first == second
    assert failure.state == FailureState.RECOVERED


def test_active_and_recovered_partitioning():
    registry = FailureRegistry()
    f1 = registry.register(RouterFailure(router="rtr-core"))
    f2 = registry.register(SwitchFailure(switch="sw-a"))
    registry.mark_recovered(f1.id)
    assert registry.active_failures() == [f2]
    assert registry.recovered_failures() == [f1]
    assert len(registry.history()) == 2
    assert len(registry) == 2


def test_history_includes_all_states():
    registry = FailureRegistry()
    f1 = registry.register(RouterFailure(router="rtr-core"))
    f2 = registry.register(SwitchFailure(switch="sw-a"))
    registry.mark_recovered(f1.id)
    assert {id(f) for f in registry.history()} == {id(f1), id(f2)}


def test_affected_devices_union():
    registry = FailureRegistry()
    registry.register(LinkFailure(src="sw-a", dst="h1"))
    registry.register(RouterFailure(router="rtr-core"))
    assert registry.affected_devices() == {"sw-a", "h1", "rtr-core"}


def test_clear():
    registry = FailureRegistry()
    registry.register(RouterFailure(router="rtr-core"))
    registry.clear()
    assert registry.history() == []
    assert registry.active_failures() == []
    assert registry.get("anything") is None
    assert len(registry) == 0


def test_known_nodes_accepted():
    registry = FailureRegistry(valid_nodes={"rtr-core", "sw-a"})
    registry.register(RouterFailure(router="rtr-core"))
    registry.register(SwitchFailure(switch="sw-a"))


def test_unknown_node_rejected():
    registry = FailureRegistry(valid_nodes={"rtr-core", "sw-a"})
    with pytest.raises(UnknownNodeError):
        registry.register(RouterFailure(router="ghost"))


def test_known_link_accepted():
    registry = FailureRegistry(valid_links={"h1-sw-a"})
    registry.register(LinkFailure(src="sw-a", dst="h1"))


def test_unknown_link_rejected():
    registry = FailureRegistry(valid_links={"h1-sw-a"})
    with pytest.raises(UnknownLinkError):
        registry.register(LinkFailure(src="sw-a", dst="h5"))


# --------------------------------------------------------------------------
# Exception handling
# --------------------------------------------------------------------------


def test_exception_hierarchy():
    for exc in (
        DuplicateFailureError,
        FailureNotFoundError,
        UnknownNodeError,
        UnknownLinkError,
        InvalidLatencyError,
        InvalidPacketLossError,
        InvalidBandwidthError,
    ):
        assert issubclass(exc, FailureError)
    assert issubclass(FailureError, Exception)


def test_invalid_latency_raises():
    with pytest.raises(InvalidLatencyError):
        LatencyFailure(node="h1", latency_ms=0)
    with pytest.raises(InvalidLatencyError):
        LatencyFailure(node="h1", latency_ms=-5)


def test_invalid_packet_loss_raises():
    with pytest.raises(InvalidPacketLossError):
        PacketLossFailure(node="h1", loss_percent=-1)
    with pytest.raises(InvalidPacketLossError):
        PacketLossFailure(node="h1", loss_percent=101)


def test_invalid_bandwidth_raises():
    with pytest.raises(InvalidBandwidthError):
        BandwidthFailure(node="h1", bandwidth_bps=0)
    with pytest.raises(InvalidBandwidthError):
        BandwidthFailure(node="h1", bandwidth_bps=-100)


def test_required_fields_raise_value_error():
    with pytest.raises(ValueError):
        RouterFailure(router="")
    with pytest.raises(ValueError):
        SwitchFailure(switch="")
    with pytest.raises(ValueError):
        LinkFailure(src="", dst="h1")
    with pytest.raises(ValueError):
        LinkFailure(src="sw-a", dst="sw-a")
    with pytest.raises(ValueError):
        LatencyFailure(node="", latency_ms=100)


def test_errors_are_catchable_via_base():
    registry = FailureRegistry()
    with pytest.raises(FailureError):
        registry.mark_recovered("missing")
