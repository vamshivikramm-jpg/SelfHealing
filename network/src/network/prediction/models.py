"""Prediction models (Submodules 4.1-4.3).

Every model implements :class:`PredictionModel`: ``fit(X, y, names)``
and ``predict_proba(X)`` returning a failure score in ``[0, 1]`` per row
(higher = failure more likely within the horizon). Scores of the rule
and z-score baselines are rankings, not calibrated probabilities; the
evaluation reports calibration separately.

Non-ML baselines (4.2) — the bar the ML models must clear:
- ``ThresholdModel``: Module 2's health thresholds on the latest values
- ``ZScoreModel``: Module 3-style distance from normal, learned from
  failure-free rows

ML models (4.3), all from scikit-learn:
- ``logistic_regression``: linear, interpretable
- ``random_forest``: non-linear trees
- ``gradient_boosting``: HistGradientBoosting, handles missing values
- ``isolation_forest``: unsupervised anomaly score (trained on normal
  rows only, ignores labels)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Sequence

import numpy as np

KINDS = ("rule", "statistical", "supervised", "unsupervised")


class PredictionModel(ABC):
    name: str = "model"
    kind: str = "supervised"

    def __init__(self):
        self.feature_names: List[str] = []
        self.fitted = False

    def fit(self, X: np.ndarray, y: np.ndarray, feature_names: Sequence[str]) -> "PredictionModel":
        self.feature_names = list(feature_names)
        self._fit(np.asarray(X, dtype=float), np.asarray(y, dtype=int))
        self.fitted = True
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if not self.fitted:
            raise RuntimeError(f"model {self.name!r} is not fitted")
        X = np.asarray(X, dtype=float)
        if X.ndim != 2 or X.shape[1] != len(self.feature_names):
            raise ValueError(f"expected {len(self.feature_names)} features, got shape {X.shape}")
        return np.clip(self._score(X), 0.0, 1.0)

    def describe(self) -> dict:
        return {"name": self.name, "kind": self.kind, "features": len(self.feature_names)}

    @abstractmethod
    def _fit(self, X: np.ndarray, y: np.ndarray) -> None: ...

    @abstractmethod
    def _score(self, X: np.ndarray) -> np.ndarray: ...


def _col(names: Sequence[str], name: str) -> Optional[int]:
    try:
        return list(names).index(name)
    except ValueError:
        return None


# ----------------------------------------------------------------------
# 4.2 Non-ML baselines
# ----------------------------------------------------------------------


class ThresholdModel(PredictionModel):
    """Alarm when the latest reading crosses a Module 2 health threshold.

    Score is 1.0 when any rule fires, else 0.0. Uses the shortest window's
    ``last`` value, i.e. the most recent reading.
    """

    name = "threshold"
    kind = "rule"
    RULES = (("link.latency_ms", 50.0), ("link.packet_loss_pct", 2.0),
             ("link.utilization_pct", 85.0), ("link.drop_rate_pps", 100.0))

    def _fit(self, X, y):
        self._columns = []
        for metric, limit in self.RULES:
            last = sorted(n for n in self.feature_names if n.startswith(metric + "__") and n.endswith("__last"))
            if last:
                self._columns.append((self.feature_names.index(last[0]), limit))

    def _score(self, X):
        fired = np.zeros(len(X), dtype=bool)
        for index, limit in self._columns:
            fired |= np.nan_to_num(X[:, index], nan=-np.inf) > limit
        return fired.astype(float)


class ZScoreModel(PredictionModel):
    """Largest absolute z-score of selected features vs. failure-free rows.

    Score = z / (1 + z), so it ranks rows by how far the most unusual
    feature is from normal. Defaults to the latest value and trend of
    every metric (``__last`` and ``__slope`` features).
    """

    name = "zscore"
    kind = "statistical"

    def __init__(self, suffixes: Sequence[str] = ("__last", "__slope"), min_std: float = 1e-3):
        super().__init__()
        self.suffixes = tuple(suffixes)
        self.min_std = min_std

    def _fit(self, X, y):
        self._cols = [i for i, n in enumerate(self.feature_names) if n.endswith(self.suffixes)]
        normal = X[y == 0][:, self._cols]
        self._mean = np.nanmean(normal, axis=0)
        self._std = np.maximum(np.nanstd(normal, axis=0), self.min_std)
        self._mean = np.nan_to_num(self._mean)

    def _score(self, X):
        z = np.abs((X[:, self._cols] - self._mean) / self._std)
        z = np.nan_to_num(z, nan=0.0).max(axis=1)
        return z / (1.0 + z)


# ----------------------------------------------------------------------
# 4.3 scikit-learn models
# ----------------------------------------------------------------------


class SklearnModel(PredictionModel):
    """Wraps a supervised scikit-learn classifier with ``predict_proba``."""

    kind = "supervised"

    def __init__(self, name: str, estimator):
        super().__init__()
        self.name = name
        self.estimator = estimator

    def _fit(self, X, y):
        if y.sum() == 0:
            raise ValueError("training data has no positive rows")
        self.estimator.fit(X, y)

    def _score(self, X):
        return self.estimator.predict_proba(X)[:, 1]

    def describe(self) -> dict:
        info = super().describe()
        info["estimator"] = type(self.estimator).__name__
        return info


class IsolationForestModel(PredictionModel):
    """Unsupervised anomaly score from an Isolation Forest.

    Trained on failure-free rows only (labels are used just to pick them).
    The raw anomaly score is mapped to [0, 1] with the training
    distribution: 0 = as normal as typical training rows, 1 = more
    anomalous than any training row.
    """

    name = "isolation_forest"
    kind = "unsupervised"

    def __init__(self, n_estimators: int = 200, random_state: int = 0):
        super().__init__()
        from sklearn.ensemble import IsolationForest
        from sklearn.impute import SimpleImputer
        from sklearn.pipeline import make_pipeline

        self.estimator = make_pipeline(
            SimpleImputer(strategy="median"),
            IsolationForest(n_estimators=n_estimators, random_state=random_state, n_jobs=-1),
        )

    def _fit(self, X, y):
        normal = X[y == 0]
        self.estimator.fit(normal)
        raw = -self.estimator.score_samples(normal)
        self._lo, self._hi = float(np.median(raw)), float(raw.max())

    def _score(self, X):
        raw = -self.estimator.score_samples(X)
        span = max(self._hi - self._lo, 1e-9)
        return (raw - self._lo) / span


def make_model(name: str, random_state: int = 0) -> PredictionModel:
    """Factory for every model compared in Module 4."""
    if name == "threshold":
        return ThresholdModel()
    if name == "zscore":
        return ZScoreModel()
    if name == "isolation_forest":
        return IsolationForestModel(random_state=random_state)

    from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if name == "logistic_regression":
        return SklearnModel(name, make_pipeline(
            SimpleImputer(strategy="median"), StandardScaler(),
            LogisticRegression(max_iter=3000, class_weight="balanced", C=0.1)))
    if name == "random_forest":
        return SklearnModel(name, make_pipeline(
            SimpleImputer(strategy="median"),
            RandomForestClassifier(n_estimators=300, min_samples_leaf=3, class_weight="balanced_subsample",
                                   n_jobs=-1, random_state=random_state)))
    if name == "gradient_boosting":
        return SklearnModel(name, HistGradientBoostingClassifier(
            max_iter=300, learning_rate=0.05, class_weight="balanced", random_state=random_state))
    raise ValueError(f"unknown model: {name!r}")


MODEL_NAMES = ("threshold", "zscore", "isolation_forest", "logistic_regression",
               "random_forest", "gradient_boosting")


def feature_importance(model: PredictionModel, top: int = 10) -> Optional[List[Dict[str, float]]]:
    """Top features for tree / linear models, else None."""
    est = getattr(model, "estimator", None)
    if est is None:
        return None
    final = est.steps[-1][1] if hasattr(est, "steps") else est
    values = getattr(final, "feature_importances_", None)
    if values is None and hasattr(final, "coef_"):
        values = np.abs(final.coef_[0])
    if values is None:
        return None
    order = np.argsort(values)[::-1][:top]
    return [{"feature": model.feature_names[i], "importance": float(values[i])} for i in order]
