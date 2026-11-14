"""Unit tests for Module 3.5 (features) and 3.6 (baselines)."""

import pytest

from network.analytics.baseline import Baseline, BaselineModel
from network.analytics.features import (
    DEFAULT_LINK_METRICS,
    STATS,
    FeatureExtractor,
    feature_name,
    window_stats,
)
from network.analytics.repository import TelemetryRepository
from network.telemetry import Metric, make_sample


def s(metric, target, value, ts):
    return make_sample(metric, target, value, ts, "test", True)


# --------------------------------------------------------------------------
# 3.5 Features
# --------------------------------------------------------------------------


def test_window_stats_values():
    stats = window_stats([1.0, 2.0, None, 4.0], step_s=5.0)
    assert stats["mean"] == pytest.approx(7 / 3)
    assert stats["min"] == 1.0 and stats["max"] == 4.0
    assert stats["last"] == 4.0 and stats["delta"] == 3.0
    # points at t=0,5,15 with values 1,2,4 -> least-squares slope 0.2 per second
    assert stats["slope"] == pytest.approx(0.2)


def test_window_stats_degenerate():
    assert window_stats([None, None], 5.0) == {k: None for k in STATS}
    single = window_stats([3.0], 5.0)
    assert single["std"] == 0.0 and single["slope"] is None


def test_feature_names():
    extractor = FeatureExtractor(step_s=5, windows_s=(120, 30))
    assert extractor.windows_s == (30.0, 120.0)
    names = extractor.feature_names([Metric.LINK_LATENCY_MS])
    assert names[0] == "link.latency_ms__30s__mean"
    assert len(names) == 2 * len(STATS)
    assert len(extractor.feature_names(DEFAULT_LINK_METRICS)) == len(DEFAULT_LINK_METRICS) * 2 * len(STATS)


def test_extractor_validation():
    with pytest.raises(ValueError):
        FeatureExtractor(step_s=0)
    with pytest.raises(ValueError):
        FeatureExtractor(step_s=10, windows_s=(5,))


def _repo_with_ramp():
    repo = TelemetryRepository()
    # latency rises 1 ms per 5 s bucket; one sample per bucket at bucket start
    repo.write_samples([s(Metric.LINK_LATENCY_MS, "h1-sw-a", float(i), i * 5.0) for i in range(12)])
    return repo


def test_extract_rows_and_no_lookahead():
    extractor = FeatureExtractor(step_s=5, windows_s=(10, 20))
    rows = extractor.extract(_repo_with_ramp(), "h1-sw-a", [Metric.LINK_LATENCY_MS], 0, 60)
    # 12 buckets, longest window = 4 buckets -> rows from bucket index 3
    assert len(rows) == 9
    first = rows[0]
    assert first["timestamp"] == 20.0  # end of bucket 3: only data from t < 20 used
    assert first["target"] == "h1-sw-a"
    assert first[feature_name(Metric.LINK_LATENCY_MS, 20, "last")] == 3.0
    assert first[feature_name(Metric.LINK_LATENCY_MS, 20, "mean")] == 1.5
    assert first[feature_name(Metric.LINK_LATENCY_MS, 10, "mean")] == 2.5
    assert first[feature_name(Metric.LINK_LATENCY_MS, 20, "slope")] == pytest.approx(0.2)


def test_extract_fills_short_gaps_only():
    repo = TelemetryRepository()
    points = [0, 1, 2, 4, 5, 6, 7]  # bucket 3 missing
    repo.write_samples([s(Metric.LINK_LATENCY_MS, "x", float(i), i * 5.0) for i in points])
    extractor = FeatureExtractor(step_s=5, windows_s=(5,), max_gap_buckets=1)
    rows = extractor.extract(repo, "x", [Metric.LINK_LATENCY_MS], 0, 40)
    assert rows[3][feature_name(Metric.LINK_LATENCY_MS, 5, "last")] == 2.0  # filled from bucket 2
    strict = FeatureExtractor(step_s=5, windows_s=(5,), max_gap_buckets=0)
    assert strict.extract(repo, "x", [Metric.LINK_LATENCY_MS], 0, 40)[3][
        feature_name(Metric.LINK_LATENCY_MS, 5, "last")] is None


# --------------------------------------------------------------------------
# 3.6 Baselines
# --------------------------------------------------------------------------


def _healthy_repo():
    repo = TelemetryRepository()
    samples = []
    for i in range(100):
        samples.append(s(Metric.LINK_LATENCY_MS, "h1-sw-a", 1.0 + (i % 5) * 0.1, float(i)))
        samples.append(s(Metric.LINK_PACKET_LOSS_PCT, "h1-sw-a", 0.0, float(i)))
        samples.append(s(Metric.LINK_BYTES, "h1-sw-a", i * 1000.0, float(i)))
    samples.append(s(Metric.NODE_CPU_PCT, "h1", 5.0, 1.0))  # too few samples
    repo.write_samples(samples)
    return repo


def test_fit_baselines():
    model = BaselineModel().fit(_healthy_repo())
    latency = model.get(Metric.LINK_LATENCY_MS, "h1-sw-a")
    assert latency.count == 100
    assert latency.mean == pytest.approx(1.2)
    assert latency.min == 1.0 and latency.max == pytest.approx(1.4)
    assert latency.p50 == pytest.approx(1.2)
    assert model.get(Metric.LINK_BYTES, "h1-sw-a") is None  # counters skipped
    assert model.get(Metric.NODE_CPU_PCT, "h1") is None     # below min_samples


def test_fit_restricted_to_period_and_metrics():
    model = BaselineModel().fit(_healthy_repo(), start=0, end=50, metrics=[Metric.LINK_LATENCY_MS])
    assert model.get(Metric.LINK_LATENCY_MS, "h1-sw-a").count == 50
    assert model.get(Metric.LINK_PACKET_LOSS_PCT, "h1-sw-a") is None


def test_zscore_and_assess():
    model = BaselineModel().fit(_healthy_repo())
    assert model.zscore(Metric.LINK_LATENCY_MS, "h1-sw-a", 1.2) == pytest.approx(0.0)
    assert model.zscore(Metric.LINK_LATENCY_MS, "unknown", 1.0) is None
    deviations = model.assess([
        s(Metric.LINK_LATENCY_MS, "h1-sw-a", 1.2, 200.0),   # normal
        s(Metric.LINK_LATENCY_MS, "h1-sw-a", 5.0, 200.0),   # far above normal
        s(Metric.LINK_PACKET_LOSS_PCT, "h1-sw-a", 1.0, 200.0),  # constant-zero series changes
    ])
    assert [d.metric for d in deviations] == [Metric.LINK_PACKET_LOSS_PCT, Metric.LINK_LATENCY_MS]
    assert deviations[0].zscore == pytest.approx(1000.0)  # min_std floor, not infinity
    assert deviations[1].above_p99 is True
    assert deviations[1].to_dict()["metric"] == "link.latency_ms"


def test_baseline_persistence():
    repo = _healthy_repo()
    model = BaselineModel().fit(repo)
    assert model.save(repo, created_ts=500.0) == 2
    loaded = BaselineModel.load(repo)
    assert loaded.get(Metric.LINK_LATENCY_MS, "h1-sw-a") == model.get(Metric.LINK_LATENCY_MS, "h1-sw-a")
    assert isinstance(loaded.get(Metric.LINK_LATENCY_MS, "h1-sw-a"), Baseline)
