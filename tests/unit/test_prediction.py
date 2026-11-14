"""Unit tests for Module 4 — failure prediction.

A small suite of simulated runs is generated once per test session.
Requires numpy and scikit-learn.
"""

import json
import pathlib

import numpy as np
import pytest

pytest.importorskip("sklearn")

from network.analytics.generate import main as generate_main  # noqa: E402
from network.analytics.scenarios import PlannedFailure, Precursor, ScenarioPlan, ScenarioRunner  # noqa: E402
from network.config import load_topology_config  # noqa: E402
from network.events import EventType  # noqa: E402
from network.prediction.data import (  # noqa: E402
    PredictionData,
    SuiteConfig,
    build_suite,
    concat,
    from_rows,
    load_dataset_dir,
)
from network.prediction.evaluation import EventScorer, choose_threshold, reliability, row_metrics  # noqa: E402
from network.prediction.experiment import pick_champion, render_markdown, run_experiment  # noqa: E402
from network.prediction.models import (  # noqa: E402
    MODEL_NAMES,
    ThresholdModel,
    ZScoreModel,
    feature_importance,
    make_model,
)
from network.prediction.online import OnlinePredictor, StoreView, parse_feature_layout  # noqa: E402
from network.prediction.registry import ModelRegistry  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = load_topology_config(ROOT / "network" / "config" / "enterprise.yaml")
SMALL = SuiteConfig(train_seeds=(1, 2), val_seeds=(3,), test_seeds=(4,), duration_s=4800,
                    n_failures=6, precursor_probability=1.0, windows_s=(30.0, 60.0))


@pytest.fixture(scope="module")
def suite():
    return build_suite(CONFIG, SMALL)


@pytest.fixture(scope="module")
def zscore(suite):
    tr = suite["train"]
    return ZScoreModel().fit(tr.X, tr.y, tr.feature_names)


# --------------------------------------------------------------------------
# 4.0 Data
# --------------------------------------------------------------------------


def test_suite_splits_are_separate_runs(suite):
    assert set(suite["train"].spans) == {"seed1", "seed2"}
    assert set(suite["val"].spans) == {"seed3"} and set(suite["test"].spans) == {"seed4"}
    train = suite["train"]
    assert train.X.shape == (len(train.y), len(train.feature_names))
    assert 0 < train.positives < len(train)
    assert train.simulated and train.synthetic_precursors
    assert train.hours == pytest.approx(2 * 4800 / 3600)
    assert train.summary()["failures"] == 12


def _rows():
    base = {"timestamp": 0.0, "target_kind": "link", "target": "a", "f__30s__last": 1.0,
            "label_failure_active": 0, "label_failure_type": "none", "label_failure_imminent": 0,
            "label_time_to_failure_s": None, "label_precursor_active": 0, "label_cooldown": 0}
    rows = []
    for i, (active, cooldown, imminent) in enumerate([(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)]):
        r = dict(base, timestamp=float(i * 5), label_failure_active=active, label_cooldown=cooldown,
                 label_failure_imminent=imminent, label_time_to_failure_s=10.0 if imminent else "")
        rows.append(r)
    return rows


def test_from_rows_drops_active_and_cooldown_rows():
    data = from_rows(_rows(), "r1", [], (0.0, 20.0), 5.0, 60.0)
    assert list(data.timestamps) == [0.0, 15.0]
    assert list(data.y) == [0, 1]
    assert data.feature_names == ["f__30s__last"]
    assert np.isnan(data.time_to_failure[0]) and data.time_to_failure[1] == 10.0


def test_concat_checks_columns():
    a = from_rows(_rows(), "r1", [], (0.0, 20.0), 5.0, 60.0)
    b = from_rows(_rows(), "r2", [], (0.0, 20.0), 5.0, 60.0)
    both = concat([a, b])
    assert len(both) == 4 and set(both.spans) == {"r1", "r2"}
    other = [dict(r, g__30s__last=1.0) for r in _rows()]
    with pytest.raises(ValueError):
        concat([a, from_rows(other, "r3", [], (0.0, 1.0), 5.0, 60.0)])


def test_load_dataset_dir(tmp_path, capsys):
    out = tmp_path / "run"
    generate_main(["--out", str(out), "--duration", "1800", "--failures", "2", "--seed", "3"])
    capsys.readouterr()
    data = load_dataset_dir(str(out))
    assert len(data.records["run"]) == 2
    assert data.feature_names[0].startswith("link.")


# --------------------------------------------------------------------------
# 4.1-4.3 Models
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_every_model_fits_and_scores(name, suite):
    tr, te = suite["train"], suite["test"]
    model = make_model(name).fit(tr.X, tr.y, tr.feature_names)
    scores = model.predict_proba(te.X)
    assert scores.shape == (len(te),)
    assert scores.min() >= 0.0 and scores.max() <= 1.0
    assert model.describe()["name"] == name


def test_model_input_validation(suite):
    tr = suite["train"]
    with pytest.raises(RuntimeError):
        ZScoreModel().predict_proba(tr.X)
    model = ZScoreModel().fit(tr.X, tr.y, tr.feature_names)
    with pytest.raises(ValueError):
        model.predict_proba(tr.X[:, :3])
    with pytest.raises(ValueError):
        make_model("crystal_ball")
    with pytest.raises(ValueError):
        make_model("random_forest").fit(tr.X, np.zeros_like(tr.y), tr.feature_names)


def test_threshold_model_rules():
    names = ["link.latency_ms__30s__last", "link.packet_loss_pct__30s__last"]
    model = ThresholdModel().fit(np.zeros((1, 2)), np.zeros(1), names)
    X = np.array([[10.0, 0.0], [60.0, 0.0], [10.0, 5.0], [np.nan, np.nan]])
    assert list(model.predict_proba(X)) == [0.0, 1.0, 1.0, 0.0]


def test_zscore_ranks_unusual_rows_higher():
    names = ["m__30s__last", "m__30s__mean"]
    X = np.array([[1.0, 0], [1.1, 0], [0.9, 0], [1.0, 0], [5.0, 0]])
    model = ZScoreModel().fit(X[:4], np.zeros(4, dtype=int), names)
    scores = model.predict_proba(X)
    assert scores[4] > 0.9 and scores[:4].max() < 0.7


def test_feature_importance(suite):
    tr = suite["train"]
    rf = make_model("random_forest").fit(tr.X, tr.y, tr.feature_names)
    top = feature_importance(rf, top=3)
    assert len(top) == 3 and top[0]["feature"] in tr.feature_names
    assert feature_importance(ZScoreModel().fit(tr.X, tr.y, tr.feature_names)) is None


def test_registry_champion_and_rollback(tmp_path, zscore):
    registry = ModelRegistry(str(tmp_path / "models"))
    assert registry.champion_ref() is None
    with pytest.raises(LookupError):
        registry.load_champion()
    _, v1 = registry.register(zscore, 0.5, {"m": 1}, {"simulated": True})
    _, v2 = registry.register(zscore, 0.7, {"m": 2}, {"simulated": True})
    assert (v1, v2) == (1, 2)
    registry.promote("zscore", 1)
    registry.promote("zscore", 2, reason="better")
    model, card = registry.load_champion()
    assert card["version"] == 2 and card["threshold"] == 0.7
    assert card["provenance"] == {"simulated": True}
    assert card["libraries"]["sklearn"]
    assert model.feature_names == zscore.feature_names
    assert registry.rollback()["version"] == 1
    assert len(registry.list()) == 2
    with pytest.raises(FileNotFoundError):
        registry.promote("zscore", 9)


# --------------------------------------------------------------------------
# 4.4 Evaluation
# --------------------------------------------------------------------------


def _toy():
    """One run, two links, one failure on link a at t=100 with a precursor from t=40."""
    ts = np.arange(0.0, 200.0, 5.0)
    timestamps = np.concatenate([ts, ts])
    targets = np.array(["a"] * len(ts) + ["b"] * len(ts), dtype=object)
    y = ((targets == "a") & (timestamps >= 40) & (timestamps <= 100)).astype(int)
    data = PredictionData(
        X=np.zeros((len(y), 1)), y=y, feature_names=["f__30s__last"], timestamps=timestamps,
        targets=targets, run_ids=np.array(["r"] * len(y), dtype=object),
        precursor=y.copy(), time_to_failure=np.full(len(y), np.nan),
        records={"r": [{"kind": "link", "affected": ["link:a"], "start": 100.0, "end": 130.0,
                        "precursor_start": 40.0, "precursor_kind": "latency"}]},
        spans={"r": (0.0, 3600.0)}, step_s=5.0, horizon_s=60.0,
    )
    return data


def test_event_scorer_lead_time_and_false_alarms():
    data = _toy()
    scorer = EventScorer(data, min_lead_s=30)
    scores = np.zeros(len(data))
    scores[(data.targets == "a") & (data.timestamps == 60.0)] = 0.9    # 40 s ahead -> caught
    scores[(data.targets == "b") & (data.timestamps >= 150.0) & (data.timestamps <= 160.0)] = 0.9  # 1 episode
    scores[(data.targets == "b") & (data.timestamps == 10.0)] = 0.9    # another episode
    result = scorer.evaluate(scores, 0.5)
    assert result["caught"] == 1 and result["event_recall"] == 1.0
    assert result["median_lead_s"] == 40.0
    assert result["recall_with_precursor"] == 1.0 and result["recall_without_precursor"] is None
    assert result["false_alarms"] == 2 and result["alarm_episodes"] == 3
    assert result["false_alarms_per_hour"] == pytest.approx(2.0)
    assert result["per_kind"] == {"link": {"failures": 1, "caught": 1}}


def test_event_scorer_late_alarm_not_caught():
    data = _toy()
    scores = np.zeros(len(data))
    scores[(data.targets == "a") & (data.timestamps == 90.0)] = 0.9  # only 10 s ahead
    result = EventScorer(data, min_lead_s=30).evaluate(scores, 0.5)
    assert result["caught"] == 0 and result["warned_any_lead"] == 1 and result["false_alarms"] == 0


def test_choose_threshold_respects_budget():
    data = _toy()
    scorer = EventScorer(data)
    scores = np.zeros(len(data))
    scores[(data.targets == "a") & (data.timestamps == 50.0)] = 0.8
    scores[(data.targets == "b") & (data.timestamps == 20.0)] = 0.6  # false alarm
    loose = choose_threshold(scorer, scores, max_false_alarms_per_hour=5.0)
    assert loose["within_budget"] and loose["validation"]["event_recall"] == 1.0
    assert loose["threshold"] == pytest.approx(0.8)  # higher threshold preferred on ties
    strict = choose_threshold(scorer, scores, max_false_alarms_per_hour=0.0)
    assert strict["within_budget"] and strict["validation"]["false_alarms"] == 0


def test_row_metrics_and_reliability():
    y = np.array([0, 0, 1, 1])
    scores = np.array([0.1, 0.6, 0.4, 0.9])
    m = row_metrics(y, scores, 0.5)
    assert m["precision"] == 0.5 and m["recall"] == 0.5 and m["alarms"] == 2
    assert m["roc_auc"] == 0.75
    assert row_metrics(np.zeros(3), np.zeros(3), 0.5)["average_precision"] is None
    bins = reliability(y, scores, bins=2)
    assert [b["rows"] for b in bins] == [2, 2]


# --------------------------------------------------------------------------
# 4.5 Experiment
# --------------------------------------------------------------------------


def test_run_experiment_report_and_registry(tmp_path, suite):
    report = run_experiment(models=("threshold", "zscore", "logistic_regression"), data=suite,
                            out_dir=str(tmp_path / "out"), registry_root=str(tmp_path / "models"),
                            suite=SMALL)
    assert {m["name"] for m in report["models"]} == {"threshold", "zscore", "logistic_regression"}
    assert report["data"]["simulated"] is True and report["data"]["synthetic_precursors"] is True
    assert "DESIGNED warning patterns" in report["warning"]
    assert set(report["o2"]) == {"logistic_regression"}
    for m in report["models"]:
        assert 0.0 <= m["threshold"] <= 1.0
        assert m["test"]["events"]["failures"] == 6
        assert m["test"]["events"]["recall_without_precursor"] is None  # every failure has signs here
    assert report["champion"] in {"threshold", "zscore", "logistic_regression", None}
    saved = json.loads((tmp_path / "out" / "report.json").read_text())
    assert saved["champion"] == report["champion"]
    assert "# Module 4" in (tmp_path / "out" / "report.md").read_text()
    registry = ModelRegistry(str(tmp_path / "models"))
    assert len(registry.list()) == 3
    if report["champion"]:
        assert registry.champion_ref()["name"] == report["champion"]


def test_pick_champion_uses_validation_only():
    def result(name, val_recall, within=True, test_recall=0.0):
        return {"name": name, "threshold_within_budget": within,
                "validation": {"events": {"event_recall": val_recall}, "rows": {"average_precision": 0.1}},
                "test": {"events": {"event_recall": test_recall}}}
    assert pick_champion([result("a", 0.5, test_recall=1.0), result("b", 0.9)]) == "b"
    assert pick_champion([result("a", 0.9, within=False)]) is None


def test_render_markdown_handles_missing_values(tmp_path, suite):
    report = run_experiment(models=("threshold",), data=suite, suite=SMALL)
    text = render_markdown(report)
    assert "| threshold" in text and "Brier" in text


# --------------------------------------------------------------------------
# 4.6 Online prediction
# --------------------------------------------------------------------------


def test_parse_feature_layout(suite):
    metrics, windows = parse_feature_layout(suite["train"].feature_names)
    assert windows == [30.0, 60.0]
    assert metrics[0].value == "link.status"
    with pytest.raises(ValueError):
        parse_feature_layout(["nonsense"])


def test_store_view_filters_by_time():
    from network.telemetry import Metric, TelemetryStore, make_sample

    store = TelemetryStore()
    store.add([make_sample(Metric.LINK_LATENCY_MS, "a", i, float(i), "t", True) for i in range(10)])
    assert [t for t, _ in StoreView(store).query_points(Metric.LINK_LATENCY_MS, "a", 3, 6)] == [3.0, 4.0, 5.0]


def test_online_predictor_warns_before_failure(suite, zscore, tmp_path):
    registry = ModelRegistry(str(tmp_path / "models"))
    val = suite["val"]
    threshold = choose_threshold(EventScorer(val), zscore.predict_proba(val.X))["threshold"]
    registry.register(zscore, threshold, {}, {"simulated": True, "synthetic_precursors": True})
    registry.promote("zscore", 1)

    precursor = Precursor("latency", 120, 20.0, ("sw-a", "h1"))
    plan = ScenarioPlan(duration_s=900, step_s=5, seed=11,
                        failures=(PlannedFailure("link", ("sw-a", "h1"), 600, 60, None, precursor),))
    runner = ScenarioRunner(plan, config=CONFIG)
    predictor = OnlinePredictor.from_registry(registry, runner.collector).attach()
    events = []
    runner.controller.event_bus.subscribe(
        lambda e: events.append(e) if e.type in (EventType.FAILURE_PREDICTED, EventType.PREDICTION_CLEARED)
        else None)
    result = runner.run()

    start = result.records[0].start
    predicted = [e for e in events if e.type == EventType.FAILURE_PREDICTED and e.data["target"] == "h1-sw-a"]
    assert predicted, "expected an early warning on the failing link"
    first = predicted[0]
    assert result.records[0].precursor_start <= _epoch(first.timestamp) < start - 30  # >= 30 s lead
    assert first.data["model"] == "zscore v1"
    assert first.data["simulated_training_data"] is True and first.data["synthetic_precursors"] is True
    assert any(e.type == EventType.PREDICTION_CLEARED for e in events)
    status = predictor.status()
    assert status["attached"] and status["model"] == "zscore v1"
    assert {p["target"] for p in predictor.predictions()} >= {"h1-sw-a", "h2-sw-a"}
    predictor.detach()
    assert predictor.status()["attached"] is False


def _epoch(iso):
    from datetime import datetime

    return datetime.fromisoformat(iso).timestamp()


def test_online_predictor_rejects_mismatched_model(suite):
    from network.telemetry import TelemetryCollector
    from network.telemetry.sources.base import TelemetrySource

    class Empty(TelemetrySource):
        name = "empty"

        def collect(self, timestamp):
            return []

    model = ZScoreModel().fit(np.zeros((2, 1)), np.zeros(2, dtype=int), ["bogus"])
    with pytest.raises(ValueError):
        OnlinePredictor(model, 0.5, TelemetryCollector([Empty()]))
