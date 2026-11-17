"""Failure prediction trained on the live network itself (Module 4, live).

The simulation trains its z-score model on simulated runs. Live telemetry
has different scales (real probe latency, real counters), so the live lab
learns "normal" from its own first minutes instead:

1. during ``warmup_s`` of healthy operation, after every collection, one
   feature row per up link is built from the collector's store (the same
   :class:`FeatureExtractor` the online predictor uses);
2. a :class:`ZScoreModel` is fitted on those rows (all labelled normal);
3. the alarm threshold is set just above what normal traffic produced
   (99.5th percentile of the training scores + 0.02, between 0.8 and 0.95,
   i.e. a z-score of at least 4);
4. an :class:`OnlinePredictor` with that model is attached and handed to
   the runtime; from then on it raises ``FAILURE_PREDICTED`` events.

Operators must not inject failures during the warm-up (the runtime
refuses them), otherwise "normal" would include failures.
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional

import numpy as np

from network.analytics.features import FeatureExtractor
from network.prediction.models import ZScoreModel
from network.prediction.online import OnlinePredictor, StoreView
from network.telemetry.metrics import Metric

LIVE_METRICS = (Metric.LINK_LATENCY_MS, Metric.LINK_PACKET_LOSS_PCT, Metric.LINK_THROUGHPUT_MBPS,
                Metric.LINK_UTILIZATION_PCT, Metric.LINK_DROP_RATE_PPS)


def choose_live_threshold(scores) -> float:
    scores = np.asarray(scores, dtype=float)
    q = float(np.quantile(scores, 0.995)) if scores.size else 0.0
    return round(min(0.95, max(0.8, q + 0.02)), 4)


class LivePredictorTrainer:
    def __init__(self, collector, on_ready: Callable[[OnlinePredictor], None], warmup_s: float = 120.0,
                 windows_s=(30.0, 60.0), min_std: float = 0.5, logger=None, clock=time.time):
        self.collector = collector
        self.on_ready = on_ready
        self.warmup_s = float(warmup_s)
        self.extractor = FeatureExtractor(step_s=collector.interval_s, windows_s=windows_s)
        self.feature_names = self.extractor.feature_names(LIVE_METRICS)
        self.min_std = min_std
        self.rows: List[list] = []
        self.first_ts: Optional[float] = None
        self.collect_from: Optional[float] = None
        self.predictor: Optional[OnlinePredictor] = None
        self.threshold: Optional[float] = None
        self.error: Optional[str] = None
        self._logger = logger
        self._view = StoreView(collector.store)
        self._token = None

    def attach(self) -> "LivePredictorTrainer":
        self._token = self.collector.add_listener(self.on_collection)
        return self

    @property
    def remaining_s(self) -> Optional[float]:
        if self.predictor is not None:
            return 0.0
        if self.first_ts is None:
            return self.warmup_s + self.extractor.windows_s[-1]
        return max(0.0, self.first_ts + self.extractor.windows_s[-1] + self.warmup_s - (self._last or 0))

    _last: Optional[float] = None

    def on_collection(self, result) -> None:
        if self.predictor is not None:
            return
        ts = result.timestamp
        self._last = ts
        if self.first_ts is None:
            self.first_ts = ts
            self.collect_from = ts + self.extractor.windows_s[-1]
        if ts < self.collect_from:
            return
        status = {s.target: s.value for s in result.samples if s.metric == Metric.LINK_STATUS}
        step, longest = self.extractor.step_s, self.extractor.windows_s[-1]
        for target, up in sorted(status.items()):
            if up < 1.0:
                continue
            rows = self.extractor.extract(self._view, target, LIVE_METRICS, ts + step - longest, ts + step)
            if rows:
                self.rows.append([np.nan if rows[-1][n] is None else rows[-1][n] for n in self.feature_names])
        if ts >= self.collect_from + self.warmup_s:
            self._train()

    def _train(self) -> None:
        if self._token is not None:
            self.collector.remove_listener(self._token)
            self._token = None
        try:
            X = np.array(self.rows, dtype=float)
            if len(X) < 20:
                raise ValueError(f"only {len(X)} feature rows collected during the warm-up")
            model = ZScoreModel(min_std=self.min_std).fit(X, np.zeros(len(X), dtype=int), self.feature_names)
            self.threshold = choose_live_threshold(model.predict_proba(X))
            predictor = OnlinePredictor(
                model, self.threshold, self.collector, step_s=self.extractor.step_s,
                provenance={"simulated": False, "synthetic_precursors": False,
                            "trained_on": f"{len(X)} rows of live warm-up telemetry"},
                model_label=f"zscore (live warm-up, {len(X)} rows)", logger=self._logger)
            predictor._first_ts = self.first_ts  # the store already holds full windows
            self.predictor = predictor.attach()
            self.on_ready(self.predictor)
        except Exception as exc:  # noqa: BLE001 - the loop runs on without prediction
            self.error = f"{type(exc).__name__}: {exc}"

    def status(self) -> dict:
        return {"model": "zscore", "trained": self.predictor is not None, "rows": len(self.rows),
                "threshold": self.threshold, "remaining_s": self.remaining_s, "error": self.error,
                "trained_on": "live warm-up telemetry"}
