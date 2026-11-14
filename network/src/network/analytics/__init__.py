"""Network data management and analytics (Module 3).

- ``repository``  SQLite storage for samples, events and baselines
- ``recorder``    persists collector output and network events
- ``timeseries``  resampling and alignment
- ``preprocess``  gap filling, clipping, scaling
- ``features``    rolling-window feature rows (Module 4 inputs)
- ``baseline``    learned normal ranges and deviation scoring
- ``scenarios``   scripted failure runs on a simulated network
- ``dataset``     labelled CSV datasets + metadata
- ``summary``     availability, incidents, MTTR/MTBF
- ``generate``    CLI: ``python -m network.analytics.generate``

All data produced by the scenario tools is SIMULATED and flagged as such.
"""

from network.analytics.baseline import Baseline, BaselineModel, Deviation
from network.analytics.dataset import build_dataset, dataset_metadata, write_csv, write_metadata
from network.analytics.features import FeatureExtractor, window_stats
from network.analytics.preprocess import MinMaxScaler, StandardScaler, clip, fill_gaps
from network.analytics.recorder import TelemetryRecorder
from network.analytics.repository import TelemetryRepository
from network.analytics.scenarios import (
    FailureRecord,
    PlannedFailure,
    Precursor,
    ScenarioPlan,
    ScenarioResult,
    ScenarioRunner,
    generate_plan,
)
from network.analytics.summary import incident_stats, incidents, network_summary, target_summary
from network.analytics.timeseries import Aggregation, align, load_aligned, resample

__all__ = [
    "Aggregation",
    "Baseline",
    "BaselineModel",
    "Deviation",
    "FailureRecord",
    "FeatureExtractor",
    "MinMaxScaler",
    "PlannedFailure",
    "Precursor",
    "ScenarioPlan",
    "ScenarioResult",
    "ScenarioRunner",
    "StandardScaler",
    "TelemetryRecorder",
    "TelemetryRepository",
    "align",
    "build_dataset",
    "clip",
    "dataset_metadata",
    "fill_gaps",
    "generate_plan",
    "incident_stats",
    "incidents",
    "load_aligned",
    "network_summary",
    "resample",
    "target_summary",
    "window_stats",
    "write_csv",
    "write_metadata",
]
