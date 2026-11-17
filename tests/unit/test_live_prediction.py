"""Unit tests for the live predictor trainer (P6).

The trainer only reads the collector, so it is exercised on the simulated
network's telemetry: it must learn from the warm-up, pick a threshold
above normal traffic, hand over an attached OnlinePredictor, and then
flag a link whose latency departs from what it learned.
"""

import pathlib

import pytest

pytest.importorskip("numpy")

from network.analytics.scenarios import PlannedFailure, ScenarioPlan, ScenarioRunner  # noqa: E402
from network.config import load_topology_config  # noqa: E402
from network.live.prediction import LivePredictorTrainer, choose_live_threshold  # noqa: E402

CONFIG = pathlib.Path(__file__).resolve().parents[2] / "network" / "config" / "enterprise-redundant.yaml"


def test_threshold_bounds():
    assert choose_live_threshold([0.1, 0.2, 0.3]) == 0.8
    assert choose_live_threshold([0.9] * 100) == 0.92
    assert choose_live_threshold([0.99] * 100) == 0.95
    assert choose_live_threshold([]) == 0.8


def test_trainer_learns_then_predicts():
    failure = PlannedFailure("latency", ("sw-a", "sw-b"), 500.0, 100.0, 150.0)
    plan = ScenarioPlan(duration_s=640.0, step_s=2.0, failures=(failure,), seed=3)
    runner = ScenarioRunner(plan, config=load_topology_config(str(CONFIG)))
    ready, predicted = [], []
    runner.controller.event_bus.subscribe(
        lambda e: predicted.append(e.data["target"]) if e.type.value == "FAILURE_PREDICTED" else None)
    trainer = LivePredictorTrainer(runner.collector, ready.append, warmup_s=120.0).attach()
    assert trainer.status()["trained"] is False
    runner.run()
    assert trainer.error is None, trainer.error
    assert len(ready) == 1 and ready[0] is trainer.predictor
    assert 0.8 <= trainer.threshold <= 0.95 and trainer.status()["trained"]
    assert trainer.predictor.provenance["simulated"] is False  # labelled as learnt from the network itself
    assert "sw-a-sw-b" in predicted, f"latency rise not flagged; predicted: {predicted}"
