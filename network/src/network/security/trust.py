"""Dynamic trust scores (Submodule 5.4).

Implements the research document's Eq. (2):

    T(s, t) = T_base(s) - sum_j  w_j * a_j(s, t) * kappa^(t - t_j)

- ``T_base``: the device's posture-adjusted base trust
- ``a_j``: an anomaly of type j (its magnitude in (0, 1])
- ``w_j``: the anomaly type's severity weight (policy file)
- ``kappa``: decay per second, set by a half-life (an anomaly's effect
  halves every ``half_life_s`` seconds), so trust recovers when the
  behaviour stops.

Trust is clamped to [0, 1] and mapped to a level:

- ``QUARANTINED`` below ``quarantine_below``; released only once trust
  rises above ``release_above`` (hysteresis prevents flapping)
- ``TRUSTED``     at or above ``trusted_at``
- ``RESTRICTED``  in between
"""

from __future__ import annotations

import threading
from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple

from network.security.records import Anomaly

PRUNE_HALF_LIVES = 12  # contributions below 2^-12 of their weight are dropped


class TrustLevel(str, Enum):
    TRUSTED = "TRUSTED"
    RESTRICTED = "RESTRICTED"
    QUARANTINED = "QUARANTINED"


class TrustEngine:
    def __init__(self, registry, settings, weights: Dict[str, float]):
        self.registry = registry
        self.settings = settings
        self.weights = dict(weights)
        self._kappa = 0.5 ** (1.0 / settings.half_life_s)
        self._anomalies: Dict[str, List[Anomaly]] = {name: [] for name in registry.names()}
        self._levels: Dict[str, TrustLevel] = {}
        self._lock = threading.RLock()
        for name in registry.names():
            self._levels[name] = self._fresh_level(self.base(name))

    @property
    def kappa(self) -> float:
        return self._kappa

    def base(self, subject: str) -> float:
        return self.registry.get(subject).effective_base_trust(self.settings.posture_penalty)

    def add(self, anomalies: Iterable[Anomaly]) -> None:
        with self._lock:
            for anomaly in anomalies:
                if anomaly.subject in self._anomalies:
                    self._anomalies[anomaly.subject].append(anomaly)

    def trust(self, subject: str, ts: float) -> float:
        with self._lock:
            penalty = 0.0
            for a in self._anomalies.get(subject, ()):
                if a.ts <= ts:
                    penalty += self.weights.get(a.kind, 0.0) * a.magnitude * self._kappa ** (ts - a.ts)
        return max(0.0, min(1.0, self.base(subject) - penalty))

    def level(self, subject: str) -> TrustLevel:
        with self._lock:
            return self._levels[subject]

    def update_levels(self, ts: float) -> List[Tuple[str, TrustLevel, TrustLevel, float]]:
        """Recompute every subject's level at ``ts``.

        Returns ``(subject, previous, current, trust)`` for each change.
        """
        changes = []
        with self._lock:
            self._prune(ts)
            for subject in self.registry.names():
                value = self.trust(subject, ts)
                previous = self._levels[subject]
                current = self._next_level(previous, value)
                if current != previous:
                    self._levels[subject] = current
                    changes.append((subject, previous, current, value))
        return changes

    def snapshot(self, ts: float) -> Dict[str, dict]:
        with self._lock:
            return {
                s: {"trust": self.trust(s, ts), "level": self._levels[s].value, "base": self.base(s),
                    "recent_anomalies": len(self._anomalies[s])}
                for s in self.registry.names()
            }

    # -- internals -----------------------------------------------------------

    def _fresh_level(self, value: float) -> TrustLevel:
        s = self.settings
        if value < s.quarantine_below:
            return TrustLevel.QUARANTINED
        return TrustLevel.TRUSTED if value >= s.trusted_at else TrustLevel.RESTRICTED

    def _next_level(self, previous: TrustLevel, value: float) -> TrustLevel:
        s = self.settings
        if previous == TrustLevel.QUARANTINED and value < s.release_above:
            return TrustLevel.QUARANTINED
        return self._fresh_level(value)

    def _prune(self, ts: float) -> None:
        horizon = PRUNE_HALF_LIVES * self.settings.half_life_s
        for subject, items in self._anomalies.items():
            self._anomalies[subject] = [a for a in items if ts - a.ts <= horizon]
