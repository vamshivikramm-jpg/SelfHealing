"""Live early-warning predictor (Submodule 4.6).

Runs a trained model against live telemetry: after every collection it
builds one feature row per link from the collector's in-memory store
(exactly the features the model was trained on), scores it, and
publishes events on the shared EventBus:

- ``FAILURE_PREDICTED``  a link's score rose to or above the threshold
- ``PREDICTION_CLEARED`` it fell back below the threshold

Links that are already down are not scored (that is detection, which the
health check already does). Nothing is scored until the store holds a
full longest feature window: partial windows look abnormal and caused
false alarms right after start-up. Every event and prediction carries the
model's provenance flags, so a model trained on simulated data is never
presented as validated on a real network.

Imports only numpy and stdlib-based project modules (no Mininet).
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from network.analytics.features import FeatureExtractor
from network.events.event import Event
from network.events.event_types import EventType
from network.logging import get_logger
from network.telemetry.metrics import Metric

SOURCE = "prediction.online"
_FEATURE = re.compile(r"^(?P<metric>[a-z_.]+)__(?P<window>\d+)s__(?P<stat>[a-z]+)$")


class StoreView:
    """Adapts a :class:`TelemetryStore` to the repository query used by features."""

    def __init__(self, store):
        self._store = store

    def query_points(self, metric, target, start=None, end=None) -> List[Tuple[float, float]]:
        return [
            (s.timestamp, s.value)
            for s in self._store.series(metric, target, since=start)
            if end is None or s.timestamp < end
        ]


def parse_feature_layout(feature_names: Sequence[str]) -> Tuple[List[Metric], List[float]]:
    """Recover the metric list and windows a model was trained with."""
    metrics: List[Metric] = []
    windows: List[float] = []
    for name in feature_names:
        match = _FEATURE.match(name)
        if not match:
            raise ValueError(f"unrecognised feature name: {name!r}")
        metric = Metric(match["metric"])
        if metric not in metrics:
            metrics.append(metric)
        window = float(match["window"])
        if window not in windows:
            windows.append(window)
    return metrics, sorted(windows)


class OnlinePredictor:
    def __init__(self, model, threshold: float, collector, event_bus=None, step_s: Optional[float] = None,
                 provenance: Optional[dict] = None, model_label: str = "", logger=None):
        self.model = model
        self.threshold = float(threshold)
        self.collector = collector
        self.event_bus = event_bus or collector.event_bus
        self.provenance = provenance or {}
        self.model_label = model_label or getattr(model, "name", "model")
        self._logger = logger or get_logger("prediction.online")
        self.metrics, windows = parse_feature_layout(model.feature_names)
        self.extractor = FeatureExtractor(step_s=step_s or collector.interval_s, windows_s=windows)
        if self.extractor.feature_names(self.metrics) != list(model.feature_names):
            raise ValueError("model features do not match the extractor layout")
        self._view = StoreView(collector.store)
        self._lock = threading.Lock()
        self._predictions: Dict[str, dict] = {}
        self._token: Optional[int] = None
        self._first_ts: Optional[float] = None
        self._last_ts: Optional[float] = None

    @property
    def warmed_up(self) -> bool:
        return self._first_ts is not None and self._last_ts is not None and \
            self._last_ts - self._first_ts >= self.extractor.windows_s[-1]

    @classmethod
    def from_registry(cls, registry, collector, event_bus=None, **kwargs) -> "OnlinePredictor":
        model, card = registry.load_champion()
        # use the time step the model was trained with, not the collector's
        trained_step = card.get("provenance", {}).get("suite", {}).get("step_s")
        if trained_step:
            kwargs.setdefault("step_s", trained_step)
        return cls(model, card["threshold"], collector, event_bus,
                   provenance=card.get("provenance", {}),
                   model_label=f"{card['name']} v{card['version']}", **kwargs)

    # -- lifecycle ----------------------------------------------------------------

    def attach(self) -> "OnlinePredictor":
        if self._token is None:
            self._token = self.collector.add_listener(self.on_collection)
        return self

    def detach(self) -> None:
        if self._token is not None:
            self.collector.remove_listener(self._token)
            self._token = None

    # -- scoring -------------------------------------------------------------------

    def on_collection(self, result) -> None:
        ts = result.timestamp
        if self._first_ts is None:
            self._first_ts = ts
        self._last_ts = ts
        if not self.warmed_up:
            return
        status = {s.target: s.value for s in result.samples if s.metric == Metric.LINK_STATUS}
        longest = self.extractor.windows_s[-1]
        step = self.extractor.step_s
        rows, targets = [], []
        for target in sorted(status):
            if status[target] < 1.0:
                self._update(target, None, ts, down=True)
                continue
            extracted = self.extractor.extract(self._view, target, self.metrics, ts + step - longest, ts + step)
            if not extracted:
                continue
            rows.append([np.nan if extracted[-1][n] is None else extracted[-1][n]
                         for n in self.model.feature_names])
            targets.append(target)
        if not rows:
            return
        scores = self.model.predict_proba(np.array(rows, dtype=float))
        for target, score in zip(targets, scores):
            self._update(target, float(score), ts)

    def _update(self, target: str, score: Optional[float], ts: float, down: bool = False) -> None:
        alert = score is not None and score >= self.threshold
        with self._lock:
            previous = self._predictions.get(target, {}).get("alert", False)
            self._predictions[target] = {
                "target": target, "score": score, "alert": alert, "down": down,
                "timestamp": ts, "threshold": self.threshold, "model": self.model_label,
            }
        if alert and not previous:
            self._publish(EventType.FAILURE_PREDICTED, target, score, ts)
        elif previous and not alert:
            self._publish(EventType.PREDICTION_CLEARED, target, score, ts)

    def _publish(self, event_type: EventType, target: str, score: Optional[float], ts: float) -> None:
        data = {
            "target": target,
            "score": score,
            "threshold": self.threshold,
            "model": self.model_label,
            "simulated_training_data": self.provenance.get("simulated", True),
            "synthetic_precursors": self.provenance.get("synthetic_precursors", False),
        }
        self.event_bus.publish(Event(
            type=event_type, source=SOURCE, data=data,
            timestamp=datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        ))
        self._logger.info(f"{event_type.value} {target} score={score}",
                          extra={"event": event_type.value, "result": "success"})

    def predictions(self) -> List[dict]:
        with self._lock:
            return [dict(v) for _, v in sorted(self._predictions.items())]

    def status(self) -> dict:
        preds = self.predictions()
        return {
            "model": self.model_label,
            "threshold": self.threshold,
            "attached": self._token is not None,
            "warmed_up": self.warmed_up,
            "step_s": self.extractor.step_s,
            "collector_interval_s": self.collector.interval_s,
            "step_matches_collector": abs(self.extractor.step_s - self.collector.interval_s) < 1e-9,
            "alerts": [p["target"] for p in preds if p["alert"]],
            "training_data": {
                "simulated": self.provenance.get("simulated", True),
                "synthetic_precursors": self.provenance.get("synthetic_precursors", False),
            },
        }
