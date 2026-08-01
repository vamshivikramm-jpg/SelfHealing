"""Event types for the Network Control Layer (Submodule 1.3).

Every NetworkController action publishes exactly one of these event
types (restart_network publishes NETWORK_STOPPED followed by
NETWORK_STARTED). Submodule 1.4B.1 adds the FailureManager events
(FAILURE_INJECTED / FAILURE_RECOVERED); the original members are
preserved for backward compatibility. Submodule 1.4B.2 adds
NETWORK_RESTORED (emitted when FailureManager.reset_all_failures()
recovers every active failure). Submodule 1.4C.2 adds impairment-
specific events (LATENCY_INJECTED, PACKET_LOSS_INJECTED,
BANDWIDTH_LIMITED) used by FailureManager for link impairments.
"""

from enum import Enum


class EventType(str, Enum):
    NETWORK_STARTED = "NETWORK_STARTED"
    NETWORK_STOPPED = "NETWORK_STOPPED"
    NETWORK_RESET = "NETWORK_RESET"
    NODE_ENABLED = "NODE_ENABLED"
    NODE_DISABLED = "NODE_DISABLED"
    LINK_CONNECTED = "LINK_CONNECTED"
    LINK_DISCONNECTED = "LINK_DISCONNECTED"
    FAILURE_INJECTED = "FAILURE_INJECTED"
    FAILURE_RECOVERED = "FAILURE_RECOVERED"
    NETWORK_RESTORED = "NETWORK_RESTORED"
    LATENCY_INJECTED = "LATENCY_INJECTED"
    PACKET_LOSS_INJECTED = "PACKET_LOSS_INJECTED"
    BANDWIDTH_LIMITED = "BANDWIDTH_LIMITED"
