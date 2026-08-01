"""Unit tests for Submodule 1.4D — Failure Scenarios.

Tests reusable failure scenario orchestrators that use the FailureManager
exclusively. Each scenario is tested for execution, restoration, status,
and summary reporting.
"""

import pathlib

import pytest

from network.failure import (
    FailureManager,
    ScenarioRouterFailure,
    ScenarioSwitchFailure,
    ScenarioCableCut,
    ScenarioHighLatency,
    ScenarioPacketLoss,
    ScenarioBandwidthConstraint,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = ROOT / "network" / "config" / "enterprise.yaml"


@pytest.fixture()
def controller():
    from network.config import load_topology_config

    from tests.unit.test_failure_manager import MockBuilder

    builder = MockBuilder(load_topology_config(CONFIG))
    from network.control import NetworkController

    return NetworkController(builder=builder)


@pytest.fixture()
def manager(controller):
    controller.start_network()
    return FailureManager(controller)


# --------------------------------------------------------------------------
# ScenarioRouterFailure
# --------------------------------------------------------------------------


def test_scenario_router_failure_execute(manager):
    scenario = ScenarioRouterFailure(manager)
    result = scenario.execute()

    assert result["class"] == "ScenarioRouterFailure"
    assert result["state"] == "RESTORED"
    assert result["failure"]["failure_type"] == "ROUTER"
    assert result["failure"]["router"] == "rtr-core"
    assert result["start_time"] is not None

    assert scenario.status()["state"] == "RESTORED"
    assert scenario.status()["failure_id"] == result["failure"]["id"]


def test_scenario_router_failure_restore(manager):
    scenario = ScenarioRouterFailure(manager)
    scenario.execute()

    result = scenario.restore()
    assert result["state"] == "IDLE"
    assert result["failure"] is None

    assert scenario.status()["state"] == "IDLE"


def test_scenario_router_failure_twice_raises(manager):
    scenario = ScenarioRouterFailure(manager)
    scenario.execute()
    scenario.restore()

    with pytest.raises(RuntimeError, match="cannot be executed in current state"):
        scenario.execute()


def test_scenario_router_failure_wrong_state_raises(manager):
    scenario = ScenarioRouterFailure(manager)

    with pytest.raises(RuntimeError, match="cannot be restored from current state"):
        scenario.restore()


# --------------------------------------------------------------------------
# ScenarioSwitchFailure
# --------------------------------------------------------------------------


def test_scenario_switch_failure_execute(manager):
    scenario = ScenarioSwitchFailure(manager)
    result = scenario.execute()

    assert result["class"] == "ScenarioSwitchFailure"
    assert result["failure"]["failure_type"] == "SWITCH"
    assert result["failure"]["switch"] == "sw-a"


def test_scenario_switch_failure_restore(manager):
    scenario = ScenarioSwitchFailure(manager)
    scenario.execute()

    result = scenario.restore()
    assert result["state"] == "IDLE"
    assert result["failure"] is None


# --------------------------------------------------------------------------
# ScenarioCableCut
# --------------------------------------------------------------------------


def test_scenario_cable_cut_execute(manager):
    scenario = ScenarioCableCut(manager)
    result = scenario.execute()

    assert result["class"] == "ScenarioCableCut"
    assert result["failure"]["failure_type"] == "LINK"
    assert result["failure"]["src"] == "sw-a"
    assert result["failure"]["dst"] == "h1"


def test_scenario_cable_cut_restore(manager):
    scenario = ScenarioCableCut(manager)
    scenario.execute()

    result = scenario.restore()
    assert result["state"] == "IDLE"
    assert result["failure"] is None


def test_scenario_cable_cut_order_insensitive(manager):
    scenario = ScenarioCableCut(manager)
    result1 = scenario.execute()
    assert result1["failure"]["src"] == "sw-a"
    scenario.restore()


# --------------------------------------------------------------------------
# ScenarioHighLatency
# --------------------------------------------------------------------------


def test_scenario_high_latency_execute(manager):
    scenario = ScenarioHighLatency(manager)
    result = scenario.execute()

    assert result["class"] == "ScenarioHighLatency"
    assert result["failure"]["failure_type"] == "LATENCY"
    assert result["failure"]["node"] == "sw-a"
    assert result["failure"]["latency_ms"] == 100


def test_scenario_high_latency_restore(manager):
    scenario = ScenarioHighLatency(manager)
    scenario.execute()

    result = scenario.restore()
    assert result["state"] == "IDLE"
    assert result["failure"] is None


def test_scenario_high_latency_invalid_node_raises(manager):
    scenario = ScenarioHighLatency(manager)

    with pytest.raises(ValueError, match="Unknown kind"):
        scenario._select_node("unknown")


# --------------------------------------------------------------------------
# ScenarioPacketLoss
# --------------------------------------------------------------------------


def test_scenario_packet_loss_execute(manager):
    scenario = ScenarioPacketLoss(manager)
    result = scenario.execute()

    assert result["class"] == "ScenarioPacketLoss"
    assert result["failure"]["failure_type"] == "PACKET_LOSS"
    assert result["failure"]["node"] == "sw-a"
    assert result["failure"]["loss_percent"] == 10


def test_scenario_packet_loss_restore(manager):
    scenario = ScenarioPacketLoss(manager)
    scenario.execute()

    result = scenario.restore()
    assert result["state"] == "IDLE"
    assert result["failure"] is None


# --------------------------------------------------------------------------
# ScenarioBandwidthConstraint
# --------------------------------------------------------------------------


def test_scenario_bandwidth_constraint_execute(manager):
    scenario = ScenarioBandwidthConstraint(manager)
    result = scenario.execute()

    assert result["class"] == "ScenarioBandwidthConstraint"
    assert result["failure"]["failure_type"] == "BANDWIDTH"
    assert result["failure"]["node"] == "sw-a"
    assert result["failure"]["bandwidth_bps"] == 10


def test_scenario_bandwidth_constraint_restore(manager):
    scenario = ScenarioBandwidthConstraint(manager)
    scenario.execute()

    result = scenario.restore()
    assert result["state"] == "IDLE"
    assert result["failure"] is None


# --------------------------------------------------------------------------
# Scenario summary and status consistency
# --------------------------------------------------------------------------


def test_scenario_summary_consistency(manager):
    scenario = ScenarioSwitchFailure(manager)
    summary = scenario.execute()

    status = scenario.status()
    assert status["class"] == summary["class"]
    assert status["state"] == summary["state"]
    assert status["failure_id"] == summary["failure"]["id"]
    assert status["start_time"] == summary["start_time"]


def test_scenario_multiple_scenarios_independent(manager):
    scenario1 = ScenarioRouterFailure(manager)
    scenario2 = ScenarioSwitchFailure(manager)

    result1 = scenario1.execute()
    result2 = scenario2.execute()

    assert result1["failure"]["failure_type"] == "ROUTER"
    assert result2["failure"]["failure_type"] == "SWITCH"

    scenario1.restore()
    scenario2.restore()

    assert scenario1.status()["state"] == "IDLE"
    assert scenario2.status()["state"] == "IDLE"


# --------------------------------------------------------------------------
# Scenario error handling
# --------------------------------------------------------------------------


def test_scenario_execute_twice_raises(manager):
    scenario = ScenarioRouterFailure(manager)
    scenario.execute()

    with pytest.raises(RuntimeError, match="cannot be executed in current state"):
        scenario.execute()


def test_scenario_restore_twice_raises(manager):
    scenario = ScenarioRouterFailure(manager)
    scenario.execute()
    scenario.restore()

    with pytest.raises(RuntimeError, match="cannot be restored from current state"):
        scenario.restore()


def test_scenario_restore_before_execute_raises(manager):
    scenario = ScenarioRouterFailure(manager)

    with pytest.raises(RuntimeError, match="cannot be restored from current state"):
        scenario.restore()


def test_scenario_select_node_exception(manager):
    scenario = ScenarioBandwidthConstraint(manager)

    # This should work now
    node = scenario._select_node("router")
    assert node == "rtr-core"


def test_scenario_select_link_exception(manager):
    scenario = ScenarioBandwidthConstraint(manager)

    # This should work now
    link = scenario._select_link()
    assert link == ("sw-a", "h1")