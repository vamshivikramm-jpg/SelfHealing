"""Scripted failure scenarios on a simulated network (Submodule 3.7).

Runs a timeline of failures through the real :class:`NetworkController`
and :class:`FailureManager` (on the in-memory
:class:`SimulatedNetworkBuilder`), collects simulated telemetry every
step on a virtual clock, records everything to a repository, and keeps
the ground truth needed to label datasets: which failure happened, when,
and which links/nodes it affected.

Synthetic warning signs ("precursors")
--------------------------------------
Real failures are often preceded by gradual degradation; the simulation
on its own has none. A failure can optionally carry a precursor that
ramps an impairment up *before* the failure, through the controller's
impairment APIs (so it is not recorded as a failure):

- ``latency``: link latency rises linearly from 0 to ``peak`` ms
- ``loss``: packet loss rises linearly from 0 to ``peak`` %
- ``bandwidth``: capacity falls linearly from line rate to ``peak`` Mbps

Latency and loss precursor peaks default below Module 2's health
thresholds, so a model has to spot the trend before the health check
fires. A bandwidth precursor on a busy uplink can cross the utilisation
threshold early; ``FailureRecord.warned_at`` records when that happens. These patterns are
DESIGNED, not observed: results on such data show a model can learn the
patterns we created, not that real networks behave this way.
"""

from __future__ import annotations

import logging
import random
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple, Union

from network.analytics.recorder import TelemetryRecorder
from network.analytics.repository import TelemetryRepository
from network.config import TopologyConfig, load_topology_config
from network.control import NetworkController
from network.control.exceptions import NetworkControlError
from network.events import EventType
from network.failure import FailureManager
from network.failure.exceptions import FailureError
from network.simulation.dataplane import SimulatedNetworkBuilder
from network.telemetry.collector import TelemetryCollector
from network.telemetry.health import HealthStatus
from network.telemetry.sources.simulated import SimulatedSource

FAILURE_KINDS = ("router", "switch", "link", "latency", "packet_loss", "bandwidth")
NODE_KINDS = ("router", "switch")
PRECURSOR_KINDS = ("latency", "loss", "bandwidth")

# Which precursors make sense before each failure kind.
COMPATIBLE_PRECURSORS = {
    "router": ("latency", "loss"),
    "switch": ("latency", "loss"),
    "link": ("latency", "loss"),
    "latency": ("latency",),
    "packet_loss": ("loss",),
    "bandwidth": ("bandwidth",),
}
DEFAULT_PRECURSOR_PEAK = {"latency": 20.0, "loss": 1.5, "bandwidth": 40.0}
DEFAULT_START_TIME = 1_700_000_000.0


@dataclass(frozen=True)
class Precursor:
    kind: str
    duration_s: float
    peak: float
    link: Tuple[str, str]

    def __post_init__(self):
        if self.kind not in PRECURSOR_KINDS:
            raise ValueError(f"unknown precursor kind: {self.kind!r}")
        if self.duration_s <= 0:
            raise ValueError("precursor duration_s must be > 0")


@dataclass(frozen=True)
class PlannedFailure:
    """One failure on the timeline. Times are seconds from the run start."""

    kind: str
    target: Tuple[str, ...]  # (node,) for router/switch, (a, b) for link kinds
    start_s: float
    duration_s: float
    magnitude: Optional[float] = None  # ms / % / Mbps for impairment kinds
    precursor: Optional[Precursor] = None
    physical: bool = False  # Module 7: damage no software command can fix

    def __post_init__(self):
        if self.kind not in FAILURE_KINDS:
            raise ValueError(f"unknown failure kind: {self.kind!r}")
        expected = 1 if self.kind in NODE_KINDS else 2
        if len(self.target) != expected:
            raise ValueError(f"{self.kind} failure needs {expected} target name(s)")
        if self.duration_s <= 0:
            raise ValueError("duration_s must be > 0")
        if self.kind not in NODE_KINDS + ("link",) and self.magnitude is None:
            raise ValueError(f"{self.kind} failure needs a magnitude")
        if self.physical and self.kind not in NODE_KINDS + ("link",):
            raise ValueError("only link, switch and router failures can be physical")
        if self.precursor is not None:
            if self.precursor.kind not in COMPATIBLE_PRECURSORS[self.kind]:
                raise ValueError(f"{self.precursor.kind} precursor cannot precede a {self.kind} failure")
            if self.start_s - self.precursor.duration_s < 0:
                raise ValueError("precursor would start before the run")

    @property
    def window_start_s(self) -> float:
        return self.start_s - (self.precursor.duration_s if self.precursor else 0.0)

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s


@dataclass(frozen=True)
class ScenarioPlan:
    duration_s: float
    step_s: float = 5.0
    failures: Tuple[PlannedFailure, ...] = ()
    seed: int = 0
    description: str = ""

    def __post_init__(self):
        if self.step_s <= 0 or self.duration_s <= 0:
            raise ValueError("duration_s and step_s must be > 0")
        for f in self.failures:
            if f.end_s > self.duration_s:
                raise ValueError("failure ends after the run")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FailureRecord:
    """Ground truth for one executed failure (absolute epoch seconds)."""

    kind: str
    target: Tuple[str, ...]
    affected: Tuple[str, ...]   # health keys, e.g. "link:h1-sw-a", "node:sw-a"
    start: float
    end: float
    precursor_start: Optional[float]
    precursor_kind: Optional[str]
    physical: bool = False
    detected_at: Optional[float] = None   # first non-HEALTHY reading during the failure
    warned_at: Optional[float] = None     # first non-HEALTHY reading during the precursor

    @property
    def detection_delay_s(self) -> Optional[float]:
        return None if self.detected_at is None else self.detected_at - self.start

    def to_dict(self) -> dict:
        data = asdict(self)
        data["detection_delay_s"] = self.detection_delay_s
        return data


@dataclass
class ScenarioResult:
    plan: ScenarioPlan
    records: List[FailureRecord]
    start: float
    end: float
    collections: int
    repository: TelemetryRepository = field(repr=False)

    def summary(self) -> dict:
        delays = [r.detection_delay_s for r in self.records if r.detection_delay_s is not None]
        return {
            "start": self.start,
            "end": self.end,
            "collections": self.collections,
            "failures": len(self.records),
            "with_precursor": sum(1 for r in self.records if r.precursor_kind),
            "detected": len(delays),
            "mean_detection_delay_s": (sum(delays) / len(delays)) if delays else None,
        }


# ----------------------------------------------------------------------
# Plan generation
# ----------------------------------------------------------------------


def _links(config: TopologyConfig) -> List[Tuple[str, str]]:
    return [(l.src, l.dst) for l in config.links]


def generate_plan(
    config: TopologyConfig,
    duration_s: float,
    seed: int = 0,
    n_failures: int = 8,
    step_s: float = 5.0,
    warmup_s: float = 300.0,
    gap_s: Union[float, Tuple[float, float]] = (180.0, 600.0),
    precursor_probability: float = 0.5,
    failure_duration_s: Tuple[float, float] = (30.0, 120.0),
    precursor_duration_s: Tuple[float, float] = (60.0, 180.0),
    kinds: Sequence[str] = FAILURE_KINDS,
    precursor_peaks: Optional[Dict[str, float]] = None,
    physical_probability: Optional[Dict[str, float]] = None,
) -> ScenarioPlan:
    """Random but reproducible plan: a healthy warm-up, then failures one
    after another (never overlapping, with healthy gaps between them).

    ``physical_probability`` (Module 7), e.g. ``{"link": 0.5, "switch": 0.2}``:
    chance that a failure of that kind is physical (no software fix). Drawn
    from a separate random stream, so plans for a seed are otherwise
    unchanged. Default: no physical failures.

    ``gap_s`` is the healthy time between one failure ending and the next
    one's warning ramp (or start). A ``(low, high)`` range draws a random
    gap each time; a fixed gap makes the next failure's timing predictable
    from the previous one, which leaks into prediction datasets (found
    while preparing Module 4), so the default is random.

    Raises ``ValueError`` if ``n_failures`` do not fit in ``duration_s``.
    """
    rng = random.Random(seed)
    peaks = dict(DEFAULT_PRECURSOR_PEAK, **(precursor_peaks or {}))
    links = _links(config)
    nodes = {kind: [d.name for d in config.devices if d.kind == kind] for kind in NODE_KINDS}

    def snap(x: float) -> float:
        return round(x / step_s) * step_s

    def gap() -> float:
        if isinstance(gap_s, (tuple, list)):
            return snap(rng.uniform(*gap_s))
        return float(gap_s)

    physical_rng = random.Random(seed * 1_000_003 + 17)
    physical_probability = dict(physical_probability or {})
    failures: List[PlannedFailure] = []
    cursor = warmup_s
    last_gap = 0.0
    for _ in range(n_failures):
        kind = rng.choice([k for k in kinds if k not in NODE_KINDS or nodes[k]])
        if kind in NODE_KINDS:
            node = rng.choice(nodes[kind])
            target: Tuple[str, ...] = (node,)
            candidate_links = [l for l in links if node in l]
        else:
            target = rng.choice(links)
            candidate_links = [target]
        magnitude = {
            "latency": round(rng.uniform(80, 200)),
            "packet_loss": round(rng.uniform(8, 30)),
            "bandwidth": round(rng.uniform(5, 10)),
        }.get(kind)
        precursor = None
        if rng.random() < precursor_probability:
            p_kind = rng.choice(COMPATIBLE_PRECURSORS[kind])
            precursor = Precursor(p_kind, snap(rng.uniform(*precursor_duration_s)),
                                  peaks[p_kind], rng.choice(candidate_links))
        duration = snap(rng.uniform(*failure_duration_s))
        start = cursor + (precursor.duration_s if precursor else 0.0)
        physical = physical_rng.random() < physical_probability.get(kind, 0.0)
        failures.append(PlannedFailure(kind, target, start, duration, magnitude, precursor, physical))
        last_gap = gap()
        cursor = start + duration + last_gap
    if cursor - last_gap > duration_s:
        raise ValueError(
            f"{n_failures} failures need about {int(cursor)} s; increase duration_s or reduce n_failures"
        )
    return ScenarioPlan(duration_s=duration_s, step_s=step_s, failures=tuple(failures), seed=seed,
                        description=f"generated: {n_failures} failures, precursor p={precursor_probability}")


# ----------------------------------------------------------------------
# Running
# ----------------------------------------------------------------------


def _quiet_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f"scenario.quiet.{name}")
    logger.handlers[:] = [logging.NullHandler()]
    logger.propagate = False
    return logger


class _VirtualClock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


class ScenarioRunner:
    """Executes a :class:`ScenarioPlan` on a simulated network."""

    def __init__(self, plan: ScenarioPlan, config: Optional[TopologyConfig] = None,
                 config_path: Optional[str] = None, repository: Optional[TelemetryRepository] = None,
                 start_time: float = DEFAULT_START_TIME, telemetry_seed: Optional[int] = None):
        self.plan = plan
        self.config = config or load_topology_config(config_path or "config/enterprise.yaml")
        self.repository = repository or TelemetryRepository()
        self.start_time = start_time
        self.clock = _VirtualClock(start_time)
        self.builder = SimulatedNetworkBuilder(config=self.config)
        self.controller = NetworkController(builder=self.builder, logger=_quiet_logger("controller"))
        self.manager = FailureManager(self.controller, logger=_quiet_logger("failures"))
        self.collector = TelemetryCollector(
            [SimulatedSource(self.controller, seed=plan.seed if telemetry_seed is None else telemetry_seed,
                             physical=lambda: self.builder.net)],
            event_bus=self.controller.event_bus, interval_s=plan.step_s, clock=self.clock,
            logger=_quiet_logger("collector"),
        )
        self.recorder = TelemetryRecorder(self.repository, collector=self.collector,
                                          event_bus=self.controller.event_bus,
                                          logger=_quiet_logger("recorder"))
        self._health_changes: List[dict] = []

    def run(self) -> ScenarioResult:
        self.recorder.attach()
        token = self.controller.event_bus.subscribe(self._on_event)
        self.controller.start_network()
        steps = int(round(self.plan.duration_s / self.plan.step_s))
        try:
            for i in range(steps + 1):
                t = i * self.plan.step_s
                self._apply_actions(t)
                self.clock.now = self.start_time + t
                self.collector.collect_once()
        finally:
            self.controller.event_bus.unsubscribe(token)
            self.recorder.detach()
        records = [self._record(f) for f in self.plan.failures]
        return ScenarioResult(self.plan, records, self.start_time,
                              self.start_time + steps * self.plan.step_s, steps + 1, self.repository)

    # -- timeline -------------------------------------------------------------

    def _apply_actions(self, t: float) -> None:
        eps = 1e-9
        for f in self.plan.failures:
            p = f.precursor
            if p is not None and f.window_start_s - eps <= t < f.start_s - eps:
                progress = (t - f.window_start_s + self.plan.step_s) / p.duration_s
                self._set_precursor(p, min(1.0, progress))
            if abs(t - f.start_s) < eps:
                self._inject(f)
            if abs(t - f.end_s) < eps:
                self._recover(f)
                if p is not None:
                    self._clear_precursor(p)

    def _set_precursor(self, p: Precursor, progress: float) -> None:
        a, b = p.link
        if p.kind == "latency":
            self.controller.set_link_latency(a, b, p.peak * progress)
        elif p.kind == "loss":
            self.controller.set_link_packet_loss(a, b, p.peak * progress)
        else:
            line_rate = self.collector.sources[0].profile.line_rate_mbps
            self.controller.set_link_bandwidth(a, b, line_rate - (line_rate - p.peak) * progress)

    def _clear_precursor(self, p: Precursor) -> None:
        a, b = p.link
        {"latency": self.controller.clear_link_latency,
         "loss": self.controller.clear_link_packet_loss,
         "bandwidth": self.controller.clear_link_bandwidth}[p.kind](a, b)

    def _inject(self, f: PlannedFailure) -> None:
        m = self.manager
        if f.kind == "router":
            m.inject_router_failure(f.target[0])
        elif f.kind == "switch":
            m.inject_switch_failure(f.target[0])
        elif f.kind == "link":
            m.disconnect_link(*f.target)
        elif f.kind == "latency":
            m.inject_latency(*f.target, f.magnitude)
        elif f.kind == "packet_loss":
            m.inject_packet_loss(*f.target, f.magnitude)
        else:
            m.limit_bandwidth(*f.target, f.magnitude)
        if f.physical:
            net = self.builder.net
            if f.kind in NODE_KINDS:
                net.break_node(f.target[0])
            else:
                net.break_link(*f.target)

    def _recover(self, f: PlannedFailure) -> None:
        """End of the planned failure (the "technician" fixes it).

        Physical damage is repaired first. If self-healing (Module 7) already
        recovered the failure through the controller, there is nothing left
        to recover and the resulting errors are ignored.
        """
        if f.physical:
            net = self.builder.net
            if f.kind in NODE_KINDS:
                net.repair_node(f.target[0])
            else:
                net.repair_link(*f.target)
        try:
            self._recover_with_manager(f)
        except (FailureError, NetworkControlError):
            pass

    def _recover_with_manager(self, f: PlannedFailure) -> None:
        m = self.manager
        if f.kind == "router":
            m.recover_router(f.target[0])
        elif f.kind == "switch":
            m.recover_switch(f.target[0])
        elif f.kind == "link":
            m.reconnect_link(*f.target)
        elif f.kind == "latency":
            m.remove_latency(*f.target)
        elif f.kind == "packet_loss":
            m.remove_packet_loss(*f.target)
        else:
            m.remove_bandwidth_limit(*f.target)

    # -- ground truth -------------------------------------------------------

    def affected_keys(self, f: PlannedFailure) -> Tuple[str, ...]:
        state = self.controller.state
        if f.kind in NODE_KINDS:
            node = f.target[0]
            links = sorted(l.name for l in state.links.values() if node in (l.src, l.dst))
            return (f"node:{node}",) + tuple(f"link:{name}" for name in links)
        return (f"link:{state.get_link(*f.target).name}",)

    def _on_event(self, event) -> None:
        if event.type == EventType.HEALTH_CHANGED:
            self._health_changes.append(event.data)

    def _first_unhealthy(self, keys: Tuple[str, ...], start: float, end: float) -> Optional[float]:
        """First time in ``[start, end]`` at which any key is not HEALTHY,
        including a state that was already unhealthy when ``start`` began."""
        changes = sorted((c for c in self._health_changes if c["key"] in keys),
                         key=lambda c: c["timestamp"])
        status_at_start: Dict[str, str] = {}
        for change in changes:
            if change["timestamp"] < start:
                status_at_start[change["key"]] = change["status"]
        if any(v != HealthStatus.HEALTHY.value for v in status_at_start.values()):
            return start
        for change in changes:
            if start <= change["timestamp"] <= end and change["status"] != HealthStatus.HEALTHY.value:
                return change["timestamp"]
        return None

    def _record(self, f: PlannedFailure) -> FailureRecord:
        affected = self.affected_keys(f)
        start = self.start_time + f.start_s
        end = self.start_time + f.end_s
        p = f.precursor
        precursor_start = (self.start_time + f.window_start_s) if p else None
        warned = None
        if p is not None:
            warned = self._first_unhealthy(affected, precursor_start, start - 1e-6)
        return FailureRecord(
            kind=f.kind, target=f.target, affected=affected, start=start, end=end,
            precursor_start=precursor_start, precursor_kind=p.kind if p else None, physical=f.physical,
            detected_at=self._first_unhealthy(affected, start, end), warned_at=warned,
        )
