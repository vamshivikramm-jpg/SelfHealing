"""Event types for the Network Control Layer (Submodule 1.3).

Every NetworkController action publishes exactly one of these event
types (restart_network publishes NETWORK_STOPPED followed by
NETWORK_STARTED).
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
