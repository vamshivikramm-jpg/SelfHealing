"""In-memory failure registry (Submodule 1.4A).

Tracks active failures, recovered failures, failure history, recovery
timestamps, and the set of affected devices.

Independent of the network layer: node/link validation is optional and is
performed only against the device/link sets supplied at construction.
"""

from __future__ import annotations

from typing import Optional

from network.failure.exceptions import (
    DuplicateFailureError,
    FailureNotFoundError,
    UnknownLinkError,
    UnknownNodeError,
)
from network.failure.failure_types import Failure, FailureState, LinkFailure, _utc_now


class FailureRegistry:
    """In-memory store of injected failures."""

    def __init__(self, valid_nodes=None, valid_links=None):
        self._valid_nodes = set(valid_nodes) if valid_nodes is not None else None
        self._valid_links = set(valid_links) if valid_links is not None else None
        self._failures: dict = {}

    def register(self, failure: Failure) -> Failure:
        """Track a failure; returns it. Raises ``DuplicateFailureError`` if
        a failure with the same id is already tracked."""
        if not isinstance(failure, Failure):
            raise TypeError("register() expects a Failure instance")
        if failure.id in self._failures:
            raise DuplicateFailureError(
                f"failure {failure.id!r} is already registered"
            )
        self._validate(failure)
        self._failures[failure.id] = failure
        return failure

    def mark_recovered(
        self, failure_id: str, recovered_at: Optional[str] = None
    ) -> Failure:
        """Mark a failure as recovered and stamp the recovery time.

        Raises :class:`FailureNotFoundError` if the id is unknown.
        Idempotent: an already-recovered failure is returned unchanged.
        """
        failure = self._failures.get(failure_id)
        if failure is None:
            raise FailureNotFoundError(f"failure {failure_id!r} not found")
        if failure.state == FailureState.RECOVERED:
            return failure
        failure.state = FailureState.RECOVERED
        failure.recovered_at = recovered_at or _utc_now()
        return failure

    def active_failures(self) -> list:
        """Failures still in :class:`FailureState.ACTIVE`."""
        return [
            f for f in self._failures.values() if f.state == FailureState.ACTIVE
        ]

    def recovered_failures(self) -> list:
        """Failures in :class:`FailureState.RECOVERED`."""
        return [
            f for f in self._failures.values() if f.state == FailureState.RECOVERED
        ]

    def history(self) -> list:
        """Every tracked failure (active + recovered), in registration order."""
        return list(self._failures.values())

    def get(self, failure_id: str) -> Optional[Failure]:
        """Return the failure with the given id, or ``None`` if unknown."""
        return self._failures.get(failure_id)

    def affected_devices(self) -> set:
        """Set of all device names ever affected by a tracked failure."""
        devices = set()
        for failure in self._failures.values():
            devices.update(failure.affected_nodes)
        return devices

    def clear(self) -> None:
        """Remove every tracked failure."""
        self._failures.clear()

    def __len__(self) -> int:
        return len(self._failures)

    def _validate(self, failure: Failure) -> None:
        if self._valid_nodes is not None:
            unknown = sorted(
                node for node in failure.affected_nodes if node not in self._valid_nodes
            )
            if unknown:
                raise UnknownNodeError(f"unknown node(s): {', '.join(unknown)}")
        if isinstance(failure, LinkFailure) and self._valid_links is not None:
            if failure.link not in self._valid_links:
                raise UnknownLinkError(f"unknown link {failure.link!r}")
