"""Unit tests for Module 3.3 (time-series operations) and 3.4 (preprocessing)."""

import pytest

from network.analytics.preprocess import (
    MinMaxScaler,
    StandardScaler,
    clip,
    clip_percentiles,
    fill_gaps,
    missing_ratio,
    percentile,
)
from network.analytics.repository import TelemetryRepository
from network.analytics.timeseries import Aggregation, align, bucket_starts, load_aligned, resample
from network.telemetry import Metric, make_sample

POINTS = [(0.0, 1.0), (1.0, 3.0), (5.0, 10.0), (6.0, 20.0), (14.0, 7.0)]


# --------------------------------------------------------------------------
# 3.3 Time series
# --------------------------------------------------------------------------


def test_bucket_starts():
    assert bucket_starts(0, 15, 5) == [0, 5, 10]
    assert bucket_starts(0, 16, 5) == [0, 5, 10, 15]
    assert bucket_starts(5, 5, 5) == []
    with pytest.raises(ValueError):
        bucket_starts(0, 10, 0)


@pytest.mark.parametrize(
    "how,expected",
    [
        (Aggregation.MEAN, [2.0, 15.0, 7.0]),
        (Aggregation.MIN, [1.0, 10.0, 7.0]),
        (Aggregation.MAX, [3.0, 20.0, 7.0]),
        (Aggregation.LAST, [3.0, 20.0, 7.0]),
        (Aggregation.SUM, [4.0, 30.0, 7.0]),
        (Aggregation.COUNT, [2.0, 2.0, 1.0]),
    ],
)
def test_resample_aggregations(how, expected):
    assert [v for _, v in resample(POINTS, 0, 15, 5, how)] == expected


def test_resample_empty_buckets_and_range():
    result = resample([(0.0, 1.0), (12.0, 2.0), (99.0, 5.0), (-1.0, 9.0)], 0, 15, 5)
    assert result == [(0, 1.0), (5, None), (10, 2.0)]
    assert [v for _, v in resample([], 0, 10, 5, Aggregation.COUNT)] == [0.0, 0.0]


def test_align_shares_grid():
    grid, values = align({"a": POINTS, "b": [(7.0, 1.0)]}, 0, 15, 5)
    assert grid == [0, 5, 10]
    assert values["a"] == [2.0, 15.0, 7.0]
    assert values["b"] == [None, 1.0, None]


def test_load_aligned_from_repository():
    repo = TelemetryRepository()
    repo.write_samples(
        [make_sample(Metric.LINK_LATENCY_MS, "h1-sw-a", v, t, "x", True) for t, v in POINTS]
        + [make_sample(Metric.LINK_PACKET_LOSS_PCT, "h1-sw-a", 2, 3.0, "x", True)]
    )
    grid, values = load_aligned(repo, "h1-sw-a", [Metric.LINK_LATENCY_MS, Metric.LINK_PACKET_LOSS_PCT], 0, 15, 5)
    assert values["link.latency_ms"] == [2.0, 15.0, 7.0]
    assert values["link.packet_loss_pct"] == [2.0, None, None]


# --------------------------------------------------------------------------
# 3.4 Preprocessing
# --------------------------------------------------------------------------


def test_fill_short_gaps_only():
    values, filled = fill_gaps([1.0, None, 3.0, None, None, None, 7.0, None], max_gap=2)
    assert values == [1.0, 1.0, 3.0, None, None, None, 7.0, 7.0]
    assert filled == [False, True, False, False, False, False, False, True]


def test_fill_gaps_never_fills_leading_values():
    assert fill_gaps([None, None, 2.0], 5)[0] == [None, None, 2.0]
    with pytest.raises(ValueError):
        fill_gaps([1.0], -1)


def test_missing_ratio():
    assert missing_ratio([1, None, None, 4]) == 0.5
    assert missing_ratio([]) == 0.0


def test_clip_and_percentiles():
    assert clip([1, 5, None, 10], low=2, high=8) == [2, 5, None, 8]
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([5], 99) == 5.0
    with pytest.raises(ValueError):
        percentile([], 50)
    clipped = clip_percentiles([float(i) for i in range(101)] + [None], 5, 95)
    assert clipped[0] == 5.0 and clipped[100] == 95.0 and clipped[-1] is None


def test_standard_scaler_round_trip_and_serialisation():
    data = {"x": [1.0, 2.0, 3.0, None], "flat": [4.0, 4.0, 4.0, 4.0]}
    scaler = StandardScaler().fit(data)
    scaled = scaler.transform(data)
    assert scaled["x"][1] == pytest.approx(0.0)
    assert scaled["x"][3] is None
    assert all(abs(v) < 1e-6 for v in scaled["flat"])  # constant column does not divide by zero
    restored = StandardScaler.from_dict(scaler.to_dict()).inverse(scaled)
    assert restored["x"][:3] == pytest.approx([1.0, 2.0, 3.0])
    with pytest.raises(KeyError):
        scaler.transform({"unknown": [1.0]})


def test_minmax_scaler():
    scaler = MinMaxScaler().fit({"x": [0.0, 5.0, 10.0], "c": [3.0, 3.0]})
    out = scaler.transform({"x": [0.0, 5.0, 10.0, None], "c": [3.0, 3.0]})
    assert out["x"] == [0.0, 0.5, 1.0, None]
    assert out["c"] == [0.0, 0.0]
    assert scaler.to_dict()["type"] == "minmax"
