"""Custom exceptions for the Network Control Layer (Submodule 1.3).

All control-layer errors derive from :class:`NetworkControlError` so
callers can catch them uniformly. Each carries a human-readable message.
"""


class NetworkControlError(Exception):
    """Base class for all network control errors."""


class NetworkAlreadyRunningError(NetworkControlError):
    """Raised when start/reset is requested on an already running network."""


class NetworkNotRunningError(NetworkControlError):
    """Raised when an operation requires a running network but it is stopped."""


class NodeNotFoundError(NetworkControlError):
    """Raised when a node name does not exist in the topology."""


class NodeAlreadyOnlineError(NetworkControlError):
    """Raised when enabling a node that is already online."""


class NodeAlreadyOfflineError(NetworkControlError):
    """Raised when disabling a node that is already offline."""


class LinkNotFoundError(NetworkControlError):
    """Raised when a link (node pair) does not exist in the topology."""


class LinkAlreadyConnectedError(NetworkControlError):
    """Raised when connecting a link that is already connected."""


class LinkAlreadyDisconnectedError(NetworkControlError):
    """Raised when disconnecting a link that is already disconnected."""
