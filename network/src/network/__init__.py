"""Virtual enterprise network package (Module 1).

Submodule 1.2: configurable enterprise topology generator built on
Mininet + Open vSwitch.
Submodule 1.3: Network Control Layer (NetworkController + state + events).
Submodule 1.4A: Failure models + in-memory registry.
Submodule 1.4B: FailureManager core (router/switch/link injection & recovery).
Submodule 1.4C: NetworkController impairment APIs + FailureManager integration.
Submodule 1.4D: Reusable failure scenarios.

No AI, telemetry, trust, or healing logic.
"""

from network.config import ConfigError, TopologyConfig, load_topology_config
from network.control import NetworkController
from network.devices import Host, Router, Server, Switch
from network.enterprise_topology import EnterpriseTopology
from network.events import Event, EventBus, EventType
from network.failure import (
    BandwidthFailure,
    DuplicateFailureError,
    Failure,
    FailureError,
    FailureManager,
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
    Scenario,
    ScenarioBandwidthConstraint,
    ScenarioCableCut,
    ScenarioHighLatency,
    ScenarioPacketLoss,
    ScenarioRouterFailure,
    ScenarioSwitchFailure,
    Severity,
    SwitchFailure,
    UnknownLinkError,
    UnknownNodeError,
)
from network.network_builder import NetworkBuilder
from network.state import LinkState, NetworkState, NodeState

__version__ = "1.0.0"

__all__ = [
    "BandwidthFailure",
    "ConfigError",
    "DuplicateFailureError",
    "EnterpriseTopology",
    "Event",
    "EventBus",
    "EventType",
    "Failure",
    "FailureError",
    "FailureManager",
    "FailureNotFoundError",
    "FailureRegistry",
    "FailureState",
    "FailureType",
    "Host",
    "InvalidBandwidthError",
    "InvalidLatencyError",
    "InvalidPacketLossError",
    "LatencyFailure",
    "LinkFailure",
    "LinkState",
    "NetworkBuilder",
    "NetworkController",
    "NetworkState",
    "NodeState",
    "PacketLossFailure",
    "Router",
    "RouterFailure",
    "Scenario",
    "ScenarioBandwidthConstraint",
    "ScenarioCableCut",
    "ScenarioHighLatency",
    "ScenarioPacketLoss",
    "ScenarioRouterFailure",
    "ScenarioSwitchFailure",
    "Server",
    "Severity",
    "Switch",
    "SwitchFailure",
    "TopologyConfig",
    "UnknownLinkError",
    "UnknownNodeError",
    "load_topology_config",
]
