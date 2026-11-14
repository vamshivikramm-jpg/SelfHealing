"""Telemetry recorder (Submodule 3.2).

Persists everything the monitoring pipeline produces into a
:class:`TelemetryRepository`:

- every :class:`CollectionResult` from a :class:`TelemetryCollector`
  (raw and derived samples), via the collector's listener hook;
- network events from an :class:`EventBus` (failures, link/node changes,
  health transitions). ``TELEMETRY_COLLECTED`` is skipped by default: it
  only summarises samples that are already stored.

Write errors are counted and logged; they never stop collection.
"""

from __future__ import annotations

import threading
from typing import Iterable, Optional

from network.logging import get_logger

DEFAULT_EXCLUDED_EVENTS = frozenset({"TELEMETRY_COLLECTED"})


class TelemetryRecorder:
    """Connects a collector and/or event bus to a repository."""

    def __init__(
        self,
        repository,
        collector=None,
        event_bus=None,
        excluded_events: Iterable[str] = DEFAULT_EXCLUDED_EVENTS,
        logger=None,
    ):
        self.repository = repository
        self._collector = collector
        self._event_bus = event_bus
        self._excluded = {getattr(e, "value", e) for e in excluded_events}
        self._logger = logger or get_logger("analytics.recorder")
        self._lock = threading.Lock()
        self._listener_token: Optional[int] = None
        self._event_token: Optional[int] = None
        self.samples_written = 0
        self.events_written = 0
        self.errors = 0

    @property
    def attached(self) -> bool:
        return self._listener_token is not None or self._event_token is not None

    def attach(self) -> "TelemetryRecorder":
        """Start recording. Idempotent."""
        if self._collector is not None and self._listener_token is None:
            self._listener_token = self._collector.add_listener(self.record_collection)
        if self._event_bus is not None and self._event_token is None:
            self._event_token = self._event_bus.subscribe(self.record_event)
        return self

    def detach(self) -> None:
        if self._collector is not None and self._listener_token is not None:
            self._collector.remove_listener(self._listener_token)
        if self._event_bus is not None and self._event_token is not None:
            self._event_bus.unsubscribe(self._event_token)
        self._listener_token = None
        self._event_token = None

    def record_collection(self, result) -> None:
        try:
            written = self.repository.write_samples(list(result.samples) + list(result.derived))
        except Exception:  # noqa: BLE001
            self._failed("could not persist telemetry samples")
            return
        with self._lock:
            self.samples_written += written

    def record_event(self, event) -> None:
        if event.type.value in self._excluded:
            return
        try:
            self.repository.write_event(event)
        except Exception:  # noqa: BLE001
            self._failed(f"could not persist event {event.type.value}")
            return
        with self._lock:
            self.events_written += 1

    def stats(self) -> dict:
        with self._lock:
            return {
                "attached": self.attached,
                "samples_written": self.samples_written,
                "events_written": self.events_written,
                "errors": self.errors,
            }

    def _failed(self, message: str) -> None:
        with self._lock:
            self.errors += 1
        self._logger.exception(message)
