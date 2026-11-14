"""Telemetry collector (Submodule 2.5).

Runs the monitoring pipeline, once per interval:

    sources -> raw samples -> RateDeriver -> TelemetryStore
                                          -> HealthEvaluator -> HEALTH_CHANGED events

Every collection publishes ``TELEMETRY_COLLECTED`` (a summary, not every
sample) on the shared :class:`EventBus`. A source that raises is isolated:
its error is recorded, ``TELEMETRY_SOURCE_ERROR`` is published, and the
other sources still run. ``HEALTH_CHANGED`` is published only when a
target's status changes, so subscribers (later: the decision engine) see
transitions rather than a stream of repeated states.

``collect_once()`` is synchronous and deterministic for tests; ``start()``
runs it on a background thread every ``interval_s`` seconds.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional

from network.events.event import Event
from network.events.event_bus import EventBus
from network.events.event_types import EventType
from network.logging import get_logger
from network.telemetry.derive import RateDeriver
from network.telemetry.health import HealthEvaluator, HealthReport, HealthStatus
from network.telemetry.metrics import Sample
from network.telemetry.sources.base import TelemetrySource
from network.telemetry.store import TelemetryStore

SOURCE = "telemetry.collector"
DEFAULT_INTERVAL_S = 5.0


@dataclass(frozen=True)
class HealthChange:
    key: str
    previous: HealthStatus
    current: HealthReport

    def to_dict(self) -> dict:
        return {"key": self.key, "previous": self.previous.value, **self.current.to_dict()}


@dataclass(frozen=True)
class CollectionResult:
    sequence: int
    timestamp: float
    duration_ms: float
    samples: List[Sample] = field(default_factory=list)
    derived: List[Sample] = field(default_factory=list)
    source_errors: Dict[str, str] = field(default_factory=dict)
    health_changes: List[HealthChange] = field(default_factory=list)

    @property
    def sample_count(self) -> int:
        return len(self.samples) + len(self.derived)

    def summary(self) -> dict:
        return {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "duration_ms": round(self.duration_ms, 3),
            "samples": len(self.samples),
            "derived": len(self.derived),
            "source_errors": dict(self.source_errors),
            "health_changes": len(self.health_changes),
        }


class TelemetryCollector:
    """Collects telemetry from one or more sources on a schedule."""

    def __init__(
        self,
        sources: Iterable[TelemetrySource],
        store: Optional[TelemetryStore] = None,
        event_bus: Optional[EventBus] = None,
        evaluator: Optional[HealthEvaluator] = None,
        deriver: Optional[RateDeriver] = None,
        interval_s: float = DEFAULT_INTERVAL_S,
        clock: Callable[[], float] = time.time,
        logger=None,
    ):
        self._sources = list(sources)
        if not self._sources:
            raise ValueError("at least one telemetry source is required")
        names = [s.name for s in self._sources]
        if len(set(names)) != len(names):
            raise ValueError(f"telemetry source names must be unique: {names}")
        if interval_s <= 0:
            raise ValueError("interval_s must be > 0")
        self.store = store or TelemetryStore()
        self.event_bus = event_bus or EventBus()
        self.evaluator = evaluator or HealthEvaluator()
        self.deriver = deriver or RateDeriver()
        self.interval_s = float(interval_s)
        self._clock = clock
        self._logger = logger or get_logger("telemetry.collector")

        self._lock = threading.RLock()
        self._health: Dict[str, HealthReport] = {}
        self._sequence = 0
        self._last_result: Optional[CollectionResult] = None
        self._last_errors: Dict[str, str] = {}

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def sources(self) -> List[TelemetrySource]:
        return list(self._sources)

    @property
    def simulated(self) -> bool:
        """True if any configured source is simulated."""
        return any(s.simulated for s in self._sources)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def health(self) -> Dict[str, HealthReport]:
        with self._lock:
            return dict(self._health)

    def last_result(self) -> Optional[CollectionResult]:
        with self._lock:
            return self._last_result

    def status(self) -> dict:
        with self._lock:
            last = self._last_result
            counts: Dict[str, int] = {s.value: 0 for s in HealthStatus}
            for report in self._health.values():
                counts[report.status.value] += 1
            return {
                "running": self.is_running,
                "interval_s": self.interval_s,
                "simulated": self.simulated,
                "sources": [s.describe() for s in self._sources],
                "collections": self._sequence,
                "last_collection": last.summary() if last else None,
                "source_errors": dict(self._last_errors),
                "health_counts": counts,
                "store": self.store.stats(),
            }

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    def collect_once(self) -> CollectionResult:
        """Run one full collection cycle and return its result."""
        with self._lock:
            started = time.perf_counter()
            timestamp = float(self._clock())
            self._sequence += 1

            samples: List[Sample] = []
            errors: Dict[str, str] = {}
            for source in self._sources:
                try:
                    samples.extend(source.collect(timestamp))
                except Exception as exc:  # noqa: BLE001 - source isolation
                    errors[source.name] = f"{type(exc).__name__}: {exc}"
                    self._source_failed(source, exc)

            derived = self.deriver.process(samples)
            self.store.add(samples)
            self.store.add(derived)
            changes = self._update_health(samples + derived)
            self._last_errors = errors

            result = CollectionResult(
                sequence=self._sequence,
                timestamp=timestamp,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                samples=samples,
                derived=derived,
                source_errors=errors,
                health_changes=changes,
            )
            self._last_result = result

        for change in changes:
            self._publish(EventType.HEALTH_CHANGED, change.to_dict())
            self._logger.info(
                f"{change.key} {change.previous.value} -> {change.current.status.value}",
                extra={"event": EventType.HEALTH_CHANGED.value, "result": "success"},
            )
        self._publish(
            EventType.TELEMETRY_COLLECTED,
            {**result.summary(), "simulated": self.simulated},
            result="partial" if errors else "success",
        )
        return result

    def _update_health(self, samples: List[Sample]) -> List[HealthChange]:
        reports = self.evaluator.evaluate(samples)
        changes: List[HealthChange] = []
        for key, report in reports.items():
            previous = self._health.get(key)
            previous_status = previous.status if previous else HealthStatus.UNKNOWN
            if previous_status != report.status:
                changes.append(HealthChange(key, previous_status, report))
            # targets missing from this batch keep their previous report
            self._health[key] = report
        return changes

    def _source_failed(self, source: TelemetrySource, exc: Exception) -> None:
        self._logger.error(
            f"telemetry source {source.name!r} failed: {exc}",
            extra={"event": EventType.TELEMETRY_SOURCE_ERROR.value, "result": "failure"},
        )
        self._publish(
            EventType.TELEMETRY_SOURCE_ERROR,
            {"source": source.name, "error": f"{type(exc).__name__}: {exc}"},
            result="failure",
        )

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start collecting every ``interval_s`` seconds on a daemon thread."""
        if self.is_running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="telemetry-collector", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the background loop and wait for it to finish."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.collect_once()
            except Exception:  # noqa: BLE001 - keep the loop alive
                self._logger.exception("telemetry collection failed")
            self._stop.wait(self.interval_s)

    def _publish(self, event_type: EventType, data: dict, result: str = "success") -> None:
        self.event_bus.publish(Event(type=event_type, source=SOURCE, data=data, result=result))
