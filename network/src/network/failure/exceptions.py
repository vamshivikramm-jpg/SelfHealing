"""Failure injection framework exceptions (Submodule 1.4A).

All failure-framework errors derive from :class:`FailureError` so callers
can catch them uniformly. Each carries a human-readable message.
"""


class FailureError(Exception):
    """Base class for all failure injection framework errors."""


class DuplicateFailureError(FailureError):
    """Raised when registering a failure whose id is already tracked."""


class FailureNotFoundError(FailureError):
    """Raised when a failure id is not present in the registry."""


class UnknownNodeError(FailureError):
    """Raised when a failure references a node outside the known topology."""


class UnknownLinkError(FailureError):
    """Raised when a failure references a link outside the known topology."""


class InvalidLatencyError(FailureError):
    """Raised when a latency value is invalid (e.g. negative)."""


class InvalidPacketLossError(FailureError):
    """Raised when a packet-loss percentage is outside [0, 100]."""


class InvalidBandwidthError(FailureError):
    """Raised when a bandwidth value is invalid (e.g. zero or negative)."""
