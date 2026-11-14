"""Counter-to-rate derivation (Submodule 2.2).

Real data planes report cumulative counters (OpenFlow port statistics),
not rates. The deriver turns consecutive counter readings into rates:

- ``link.bytes``           -> ``link.throughput_mbps``
- ``link.dropped_packets`` -> ``link.drop_rate_pps``
- throughput / ``link.capacity_mbps`` -> ``link.utilization_pct``

A counter that goes *down* means the device restarted (counter reset).
No rate is emitted for that interval; the new value becomes the baseline.
The same deriver is used for simulated and live sources.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

from network.telemetry.metrics import Metric, Sample, make_sample

_RATE_RULES = {
    # counter metric -> (derived metric, scale applied to delta/second)
    Metric.LINK_BYTES: (Metric.LINK_THROUGHPUT_MBPS, 8.0 / 1_000_000.0),
    Metric.LINK_DROPPED_PACKETS: (Metric.LINK_DROP_RATE_PPS, 1.0),
}


class RateDeriver:
    """Stateful counter-delta -> rate converter."""

    def __init__(self):
        self._previous: Dict[Tuple[Metric, str], Sample] = {}
        self._resets = 0

    @property
    def resets_detected(self) -> int:
        return self._resets

    def reset(self) -> None:
        self._previous.clear()
        self._resets = 0

    def process(self, samples: Iterable[Sample]) -> List[Sample]:
        """Return derived samples for one batch of raw samples."""
        samples = list(samples)
        derived: List[Sample] = []
        throughput: Dict[str, Sample] = {}
        capacity: Dict[str, float] = {}

        for sample in samples:
            if sample.metric == Metric.LINK_CAPACITY_MBPS:
                capacity[sample.target] = sample.value
            rule = _RATE_RULES.get(sample.metric)
            if rule is None:
                continue
            rate = self._rate(sample, *rule)
            if rate is None:
                continue
            derived.append(rate)
            if rate.metric == Metric.LINK_THROUGHPUT_MBPS:
                throughput[rate.target] = rate

        for target, rate in throughput.items():
            cap = capacity.get(target)
            if cap is None or cap <= 0:
                continue
            derived.append(
                make_sample(
                    Metric.LINK_UTILIZATION_PCT,
                    target,
                    min(100.0, max(0.0, rate.value / cap * 100.0)),
                    rate.timestamp,
                    rate.source,
                    rate.simulated,
                    rate.labels,
                )
            )
        return derived

    def _rate(self, sample: Sample, derived_metric: Metric, scale: float):
        previous = self._previous.get(sample.key)
        self._previous[sample.key] = sample
        if previous is None:
            return None
        dt = sample.timestamp - previous.timestamp
        if dt <= 0:
            return None
        delta = sample.value - previous.value
        if delta < 0:  # counter reset (device restart)
            self._resets += 1
            return None
        return make_sample(
            derived_metric,
            sample.target,
            delta / dt * scale,
            sample.timestamp,
            sample.source,
            sample.simulated,
            sample.labels,
        )
