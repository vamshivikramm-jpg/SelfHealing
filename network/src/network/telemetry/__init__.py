"""Network telemetry and monitoring (Module 2).

Pipeline: sources -> samples -> rate derivation -> store -> health
evaluation -> events. Modules import only standard-library code and the
events layer, never Mininet, so the pipeline runs anywhere.

Currently available source: :class:`SimulatedSource` (development /
testing; every sample is marked ``simulated``). Live sources (Ryu
OpenFlow port statistics, active probes) are added once a Linux lab
with Mininet is available.
"""

from network.telemetry.collector import (
    CollectionResult,
    HealthChange,
    TelemetryCollector,
)
from network.telemetry.derive import RateDeriver
from network.telemetry.health import (
    HealthEvaluator,
    HealthReport,
    HealthStatus,
    Thresholds,
)
from network.telemetry.metrics import (
    METRIC_SPECS,
    Metric,
    MetricKind,
    MetricSpec,
    Sample,
    TargetKind,
    make_sample,
    spec_for,
)
from network.telemetry.sources.base import TelemetrySource
from network.telemetry.sources.simulated import SimulatedSource, SimulationProfile
from network.telemetry.store import TelemetryStore

__all__ = [
    "METRIC_SPECS",
    "CollectionResult",
    "HealthChange",
    "HealthEvaluator",
    "HealthReport",
    "HealthStatus",
    "Metric",
    "MetricKind",
    "MetricSpec",
    "RateDeriver",
    "Sample",
    "SimulatedSource",
    "SimulationProfile",
    "TargetKind",
    "TelemetryCollector",
    "TelemetrySource",
    "TelemetryStore",
    "Thresholds",
    "make_sample",
    "spec_for",
]
