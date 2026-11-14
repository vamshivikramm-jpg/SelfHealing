"""Simulated telemetry source (Submodule 2.3).

Generates realistic, deterministic telemetry from the *control-plane
view* of the network: :class:`NetworkState` (network running, nodes
online, links connected) and the controller's active link impairments
(latency, packet loss, bandwidth). It therefore reacts to every failure
injected through the existing ``FailureManager``.

Every sample is marked ``simulated=True``. This source exists so the
telemetry pipeline (and Modules 3-4) can be developed and tested without
a live Mininet network; it is NOT evidence that the real data plane
behaves this way. Live sources replace it once the Linux lab exists.

Modelling rules
---------------
- A link is *up* when the network is running, the link is connected and
  both endpoints are online. Down links report status 0 and 100 % loss
  and carry no traffic.
- Latency = base latency + configured impairment + small jitter.
- Loss is *measured* by simulated probes: each of ``probes_per_sample``
  probes is lost with the configured probability, so the reported loss
  has realistic sampling noise.
- Offered load follows a smooth periodic pattern plus noise; delivered
  traffic is capped by link capacity and reduced by loss. Traffic that
  cannot be delivered is counted as dropped packets.
- Byte/packet/drop counters are cumulative; they reset to zero when a
  switch or router restarts, like real OpenFlow port counters.
- Node CPU rises with the traffic through the node; memory drifts slowly.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from network.telemetry.metrics import Metric, Sample, make_sample
from network.telemetry.sources.base import TelemetrySource

FORWARDING_KINDS = ("router", "switch")


@dataclass(frozen=True)
class SimulationProfile:
    """Tunable parameters of the simulated network."""

    line_rate_mbps: float = 1000.0
    base_latency_ms: float = 0.05
    latency_jitter_ms: float = 0.02
    probes_per_sample: int = 50
    uplink_load_mbps: float = 120.0   # links touching a router
    server_load_mbps: float = 60.0    # links touching a server
    access_load_mbps: float = 20.0    # every other link
    load_variation: float = 0.3       # amplitude of the periodic pattern (0..1)
    load_period_s: float = 300.0
    load_noise: float = 0.05          # relative gaussian noise on offered load
    mean_packet_bytes: int = 800
    cpu_base_pct: Dict[str, float] = field(
        default_factory=lambda: {"router": 15.0, "switch": 10.0, "host": 5.0, "server": 20.0}
    )
    cpu_per_load_pct: float = 40.0    # extra CPU at 100 % of line rate
    memory_base_pct: Dict[str, float] = field(
        default_factory=lambda: {"router": 30.0, "switch": 25.0, "host": 20.0, "server": 40.0}
    )
    node_noise_pct: float = 1.5


@dataclass
class _LinkCounters:
    bytes: float = 0.0
    packets: float = 0.0
    dropped: float = 0.0


class SimulatedSource(TelemetrySource):
    """Deterministic simulated telemetry driven by controller state."""

    simulated = True

    def __init__(
        self,
        controller,
        seed: int = 0,
        profile: Optional[SimulationProfile] = None,
        name: str = "simulated",
    ):
        self._controller = controller
        self._rng = random.Random(seed)
        self._profile = profile or SimulationProfile()
        self.name = name
        self._counters: Dict[str, _LinkCounters] = {}
        self._memory: Dict[str, float] = {}
        self._node_was_online: Dict[str, bool] = {}
        self._last_timestamp: Optional[float] = None
        self._start_timestamp: Optional[float] = None
        self._phase: Dict[str, float] = {}

    @property
    def profile(self) -> SimulationProfile:
        return self._profile

    # ------------------------------------------------------------------
    # TelemetrySource
    # ------------------------------------------------------------------

    def collect(self, timestamp: float) -> List[Sample]:
        state = self._controller.state
        impairments = self._controller.impairments()
        if self._start_timestamp is None:
            self._start_timestamp = timestamp
        dt = 0.0 if self._last_timestamp is None else max(0.0, timestamp - self._last_timestamp)
        self._last_timestamp = timestamp

        running = bool(state.is_running)
        nodes = {name: node for name, node in sorted(state.nodes.items())}
        online = {name: running and node.is_online for name, node in nodes.items()}
        self._reset_counters_of_restarted_switches(nodes, online)

        samples: List[Sample] = []
        node_load: Dict[str, float] = {name: 0.0 for name in nodes}
        for link in sorted(state.links.values(), key=lambda l: l.name):
            link_samples, delivered = self._link_samples(
                link, nodes, online, impairments.get(link.name, {}), timestamp, dt, running
            )
            samples.extend(link_samples)
            for endpoint in (link.src, link.dst):
                if endpoint in node_load:
                    node_load[endpoint] += delivered

        for name, node in nodes.items():
            samples.extend(self._node_samples(node, online[name], node_load[name], timestamp))
        return samples

    def describe(self) -> dict:
        info = super().describe()
        info["profile"] = {
            "line_rate_mbps": self._profile.line_rate_mbps,
            "base_latency_ms": self._profile.base_latency_ms,
            "probes_per_sample": self._profile.probes_per_sample,
        }
        return info

    # ------------------------------------------------------------------
    # Links
    # ------------------------------------------------------------------

    def _link_samples(self, link, nodes, online, impairment, ts, dt, running):
        p = self._profile
        name = link.name
        labels = {"src": link.src, "dst": link.dst}
        counters = self._counters.setdefault(name, _LinkCounters())
        up = (
            running
            and link.is_connected
            and online.get(link.src, False)
            and online.get(link.dst, False)
        )
        capacity = float(impairment.get("bandwidth_mbps", p.line_rate_mbps))

        def sample(metric, value):
            return make_sample(metric, name, value, ts, self.name, True, labels)

        out = [sample(Metric.LINK_STATUS, 1.0 if up else 0.0),
               sample(Metric.LINK_CAPACITY_MBPS, capacity)]
        delivered = 0.0
        if up:
            latency = (
                p.base_latency_ms
                + float(impairment.get("latency_ms", 0.0))
                + abs(self._rng.gauss(0.0, p.latency_jitter_ms))
            )
            loss_probability = float(impairment.get("loss_percent", 0.0)) / 100.0
            out.append(sample(Metric.LINK_LATENCY_MS, latency))
            out.append(sample(Metric.LINK_PACKET_LOSS_PCT, self._measured_loss(loss_probability)))

            offered = self._offered_load(link, nodes, ts)
            delivered = min(offered, capacity) * (1.0 - loss_probability)
            dropped_mbps = max(0.0, offered - delivered)
            bytes_delivered = delivered * 1_000_000.0 / 8.0 * dt
            counters.bytes += bytes_delivered
            counters.packets += bytes_delivered / p.mean_packet_bytes
            counters.dropped += dropped_mbps * 1_000_000.0 / 8.0 * dt / p.mean_packet_bytes
        else:
            out.append(sample(Metric.LINK_PACKET_LOSS_PCT, 100.0))

        out.append(sample(Metric.LINK_BYTES, math.floor(counters.bytes)))
        out.append(sample(Metric.LINK_PACKETS, math.floor(counters.packets)))
        out.append(sample(Metric.LINK_DROPPED_PACKETS, math.floor(counters.dropped)))
        return out, delivered

    def _measured_loss(self, probability: float) -> float:
        n = self._profile.probes_per_sample
        if probability <= 0:
            return 0.0
        if probability >= 1:
            return 100.0
        lost = sum(1 for _ in range(n) if self._rng.random() < probability)
        return lost / n * 100.0

    def _offered_load(self, link, nodes, ts) -> float:
        p = self._profile
        kinds = {getattr(nodes.get(link.src), "kind", ""), getattr(nodes.get(link.dst), "kind", "")}
        if "router" in kinds:
            base = p.uplink_load_mbps
        elif "server" in kinds:
            base = p.server_load_mbps
        else:
            base = p.access_load_mbps
        phase = self._phase.setdefault(link.name, self._rng.uniform(0.0, 2.0 * math.pi))
        elapsed = ts - (self._start_timestamp or ts)
        pattern = 1.0 + p.load_variation * math.sin(2.0 * math.pi * elapsed / p.load_period_s + phase)
        noise = 1.0 + self._rng.gauss(0.0, p.load_noise)
        return max(0.0, base * pattern * noise)

    def _reset_counters_of_restarted_switches(self, nodes, online) -> None:
        for name, node in nodes.items():
            was_online = self._node_was_online.get(name)
            self._node_was_online[name] = online[name]
            if node.kind in FORWARDING_KINDS and was_online is False and online[name]:
                for link in self._controller.state.links.values():
                    if name in (link.src, link.dst):
                        self._counters[link.name] = _LinkCounters()

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def _node_samples(self, node, is_online, load_mbps, ts) -> List[Sample]:
        p = self._profile
        labels = {"kind": node.kind}

        def sample(metric, value):
            return make_sample(metric, node.name, value, ts, self.name, True, labels)

        out = [sample(Metric.NODE_STATUS, 1.0 if is_online else 0.0)]
        if not is_online:
            self._memory.pop(node.name, None)
            return out
        cpu = (
            p.cpu_base_pct.get(node.kind, 10.0)
            + p.cpu_per_load_pct * min(1.0, load_mbps / p.line_rate_mbps)
            + self._rng.gauss(0.0, p.node_noise_pct)
        )
        memory = self._memory.get(node.name, p.memory_base_pct.get(node.kind, 25.0))
        memory = memory + self._rng.gauss(0.0, p.node_noise_pct / 3.0)
        base_memory = p.memory_base_pct.get(node.kind, 25.0)
        memory += (base_memory - memory) * 0.1  # drift back towards the baseline
        self._memory[node.name] = memory
        out.append(sample(Metric.NODE_CPU_PCT, _clamp(cpu)))
        out.append(sample(Metric.NODE_MEMORY_PCT, _clamp(memory)))
        return out


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))
