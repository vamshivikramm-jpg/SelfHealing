"""Virtual enterprise network package (Module 1).

Submodule 1.2: configurable enterprise topology generator built on
Mininet + Open vSwitch.
Submodule 1.3: Network Control Layer (NetworkController + state + events).

No AI, telemetry, trust, or healing logic.
"""

from network.config import ConfigError, TopologyConfig, load_topology_config
from network.control import NetworkController
from network.devices import Host, Router, Server, Switch
from network.enterprise_topology import EnterpriseTopology
from network.events import Event, EventBus, EventType
from network.network_builder import NetworkBuilder
from network.state import LinkState, NetworkState, NodeState

__version__ = "0.3.0"

__all__ = [
    "ConfigError",
    "EnterpriseTopology",
    "Event",
    "EventBus",
    "EventType",
    "Host",
    "LinkState",
    "NetworkBuilder",
    "NetworkController",
    "NetworkState",
    "NodeState",
    "Router",
    "Server",
    "Switch",
    "TopologyConfig",
    "load_topology_config",
]
