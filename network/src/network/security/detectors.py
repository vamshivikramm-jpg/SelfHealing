"""Behaviour detectors (Submodule 5.3).

Each detector looks at one step of observations (flows with their policy
decisions, and login attempts) and emits at most one
:class:`~network.security.records.Anomaly` per subject per step, with a
magnitude in (0, 1]. Detectors never read ground-truth labels.

| Kind                   | Signal |
|------------------------|--------|
| fingerprint_mismatch   | a flow claimed an IP/MAC that is not the sender's |
| auth_failures          | >= 5 failed logins within 60 s |
| port_scan              | >= 10 distinct destination ports within 30 s |
| policy_violation       | requests denied by policy (no rule / low trust) |
| volume_anomaly         | packet rate far above the subject's own learned normal |
| flood                  | network-wide source entropy drops: one sender dominates
                           traffic (the H_flow term of Eq. 3) |

``ThreatIndex`` combines the network-level signals of Eq. (3) for Module 6:
H_flow (entropy drop), R_auth (failed-login rate) and F_anom (anomalous
subjects), each normalised to [0, 1] and averaged.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Sequence, Tuple

from network.security.policy import DENY_IDENTITY, DENY_NO_RULE, DENY_TRUST
from network.security.records import Anomaly


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


class FingerprintDetector:
    kind = "fingerprint_mismatch"

    def process(self, ts, decided, auth) -> List[Anomaly]:
        senders = defaultdict(int)
        for flow, decision in decided:
            if decision.code == DENY_IDENTITY:
                senders[flow.src] += 1
        return [Anomaly(ts, s, self.kind, 1.0, f"{n} flow(s) with another device's IP/MAC")
                for s, n in sorted(senders.items())]


class AuthFailureDetector:
    kind = "auth_failures"

    def __init__(self, window_s: float = 60.0, threshold: int = 5):
        self.window_s, self.threshold = window_s, threshold
        self._failures: Dict[str, Deque[float]] = defaultdict(deque)

    def process(self, ts, decided, auth) -> List[Anomaly]:
        for event in auth:
            if not event.success:
                self._failures[event.device].append(event.ts)
        out = []
        for subject, times in sorted(self._failures.items()):
            while times and ts - times[0] >= self.window_s:
                times.popleft()
            if len(times) >= self.threshold:
                out.append(Anomaly(ts, subject, self.kind, _clip(len(times) / (2 * self.threshold)),
                                   f"{len(times)} failed logins in {self.window_s:.0f} s"))
        return out


class PortScanDetector:
    kind = "port_scan"

    def __init__(self, window_s: float = 30.0, threshold: int = 10):
        self.window_s, self.threshold = window_s, threshold
        self._seen: Dict[str, Deque[Tuple[float, str, int]]] = defaultdict(deque)

    def process(self, ts, decided, auth) -> List[Anomaly]:
        for flow, _ in decided:
            self._seen[flow.src].append((flow.ts, flow.dst, flow.dst_port))
        out = []
        for subject, items in sorted(self._seen.items()):
            while items and ts - items[0][0] >= self.window_s:
                items.popleft()
            distinct = len({(d, p) for _, d, p in items})
            if distinct >= self.threshold:
                out.append(Anomaly(ts, subject, self.kind, _clip(distinct / (3 * self.threshold)),
                                   f"{distinct} distinct destination ports in {self.window_s:.0f} s"))
        return out


class PolicyViolationDetector:
    kind = "policy_violation"

    def __init__(self, full_at: int = 5):
        self.full_at = full_at

    def process(self, ts, decided, auth) -> List[Anomaly]:
        denied = defaultdict(int)
        for flow, decision in decided:
            if decision.code in (DENY_NO_RULE, DENY_TRUST):
                denied[flow.src] += 1
        return [Anomaly(ts, s, self.kind, _clip(n / self.full_at), f"{n} request(s) denied by policy")
                for s, n in sorted(denied.items())]


class VolumeDetector:
    """Packets per step vs. the subject's own exponentially weighted normal."""

    kind = "volume_anomaly"

    def __init__(self, alpha: float = 0.05, z_threshold: float = 6.0, min_packets: int = 2000,
                 warmup_steps: int = 20):
        self.alpha, self.z_threshold = alpha, z_threshold
        self.min_packets, self.warmup_steps = min_packets, warmup_steps
        self._mean: Dict[str, float] = {}
        self._var: Dict[str, float] = {}
        self._steps: Dict[str, int] = defaultdict(int)

    def process(self, ts, decided, auth, subjects: Sequence[str] = ()) -> List[Anomaly]:
        packets = defaultdict(int)
        for flow, _ in decided:
            packets[flow.src] += flow.packets
        out = []
        for subject in sorted(set(packets) | set(subjects)):
            value = float(packets.get(subject, 0))
            mean = self._mean.get(subject, value)
            var = self._var.get(subject, 0.0)
            std = math.sqrt(var) if var > 0 else max(1.0, mean * 0.5)
            z = (value - mean) / std
            anomalous = (self._steps[subject] >= self.warmup_steps and z > self.z_threshold
                         and value >= self.min_packets)
            if anomalous:
                out.append(Anomaly(ts, subject, self.kind, _clip(z / (3 * self.z_threshold)),
                                   f"{int(value)} packets in one step (normal ~{mean:.0f})"))
            else:  # learn only from normal behaviour
                diff = value - mean
                mean += self.alpha * diff
                var = (1 - self.alpha) * (var + self.alpha * diff * diff)
                self._mean[subject], self._var[subject] = mean, var
                self._steps[subject] += 1
        return out


def normalized_entropy(weights: Dict[str, float]) -> float:
    total = float(sum(weights.values()))
    if total <= 0 or len(weights) < 2:
        return 1.0
    h = -sum((w / total) * math.log(w / total) for w in weights.values() if w > 0)
    return h / math.log(len(weights))


class FloodDetector:
    """Entropy of the packet share per sender (H_flow).

    Normal traffic is spread across many senders; a flood concentrates it
    on one, so the entropy drops below its learned baseline.
    """

    kind = "flood"

    def __init__(self, alpha: float = 0.05, drop_threshold: float = 0.3, warmup_steps: int = 20):
        self.alpha, self.drop_threshold, self.warmup_steps = alpha, drop_threshold, warmup_steps
        self.baseline = None
        self._steps = 0
        self.last_entropy = 1.0
        self.last_drop = 0.0

    def process(self, ts, decided, auth) -> List[Anomaly]:
        packets = defaultdict(float)
        for flow, _ in decided:
            packets[flow.src] += flow.packets
        entropy = normalized_entropy(packets)
        self.last_entropy = entropy
        if self.baseline is None:
            self.baseline = entropy
        drop = max(0.0, self.baseline - entropy)
        self.last_drop = drop
        if self._steps >= self.warmup_steps and drop > self.drop_threshold and packets:
            top = max(packets, key=packets.get)
            return [Anomaly(ts, top, self.kind, _clip(drop / 0.6),
                            f"traffic entropy {entropy:.2f} vs normal {self.baseline:.2f}; top sender {top}")]
        self.baseline += self.alpha * (entropy - self.baseline)
        self._steps += 1
        return []


@dataclass(frozen=True)
class ThreatIndex:
    """Eq. (3) components, each in [0, 1], and their mean."""

    h_flow: float
    r_auth: float
    f_anom: float

    @property
    def value(self) -> float:
        return (self.h_flow + self.r_auth + self.f_anom) / 3.0

    def to_dict(self) -> dict:
        return {"h_flow": self.h_flow, "r_auth": self.r_auth, "f_anom": self.f_anom, "value": self.value}


class DetectorSuite:
    def __init__(self, subjects: Sequence[str]):
        self.subjects = list(subjects)
        self.fingerprint = FingerprintDetector()
        self.auth = AuthFailureDetector()
        self.scan = PortScanDetector()
        self.policy = PolicyViolationDetector()
        self.volume = VolumeDetector()
        self.flood = FloodDetector()

    def process(self, ts, decided, auth) -> Tuple[List[Anomaly], ThreatIndex]:
        anomalies: List[Anomaly] = []
        anomalies += self.fingerprint.process(ts, decided, auth)
        anomalies += self.auth.process(ts, decided, auth)
        anomalies += self.scan.process(ts, decided, auth)
        anomalies += self.policy.process(ts, decided, auth)
        anomalies += self.volume.process(ts, decided, auth, self.subjects)
        anomalies += self.flood.process(ts, decided, auth)
        failed = sum(1 for a in auth if not a.success)
        threat = ThreatIndex(
            h_flow=_clip(self.flood.last_drop / 0.6),
            r_auth=_clip(failed / max(1, len(auth))) if auth else 0.0,
            f_anom=_clip(len({a.subject for a in anomalies}) / max(1, len(self.subjects))),
        )
        return anomalies, threat
