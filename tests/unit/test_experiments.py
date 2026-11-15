"""Unit tests for Module 9 — statistics, figures, topologies, provenance and the runner."""

import json
import pathlib

import pytest

from network.config import load_topology_config
from network.experiments import provenance, svg
from network.experiments.stats import bounds_for, fmt_ci, holm, mean_ci, paired_test
from network.experiments.topologies import campus, write_config_dir
from network.security.identity import IdentityRegistry


def test_mean_ci_and_bounds():
    s = mean_ci([1.0, 2.0, 3.0, 4.0])
    assert s["mean"] == 2.5 and s["ci_low"] < 2.5 < s["ci_high"]
    assert mean_ci([90.0, 100.0], lower=0, upper=100)["ci_high"] == 100.0
    assert mean_ci([]) == {"n": 0, "mean": None, "ci_low": None, "ci_high": None, "sd": None}
    assert mean_ci([5.0])["ci_low"] is None
    assert mean_ci([1.0, None, 3.0])["n"] == 2
    assert bounds_for("user_loss_pct") == (0.0, 100.0) and bounds_for("success_rate") == (0.0, 1.0)
    assert bounds_for("mttr_s") == (0.0, None)
    assert fmt_ci(mean_ci([0.5, 0.7]), 0, 100, "%").startswith("60 [")
    assert fmt_ci({"mean": None}) == "—"


def test_paired_test_and_holm():
    a = [10, 12, 9, 14, 11, 13, 10, 12, 15, 11]
    b = [20, 22, 19, 25, 21, 24, 18, 23, 26, 22]
    t = paired_test(a, b)
    assert t["n"] == 10 and t["median_diff"] < 0 and t["effect_r"] == -1.0 and t["p"] < 0.01
    assert paired_test([1, 2], [1, 2])["p"] == 1.0
    assert paired_test([1, None, 3], [2, 5, None])["n"] == 1
    adj = holm({"x": 0.01, "y": 0.04, "z": None})
    assert adj["x"]["p_adj"] == pytest.approx(0.02) and adj["x"]["significant"]
    assert adj["y"]["p_adj"] == pytest.approx(0.04) and adj["z"]["p_adj"] is None


def test_svg_figures():
    bar = svg.bar_chart("t", ["a", "b"], [1.0, None], [0.5, None], [1.5, None], "y")
    assert bar.startswith("<svg") and bar.count("<rect") == 2  # background + one bar
    line = svg.line_chart("t", {"s": [(10, 1.0), (100, 2.0)]}, "x", "y", log_x=True)
    assert "<path" in line and "<circle" in line
    assert "no data" in svg.line_chart("t", {"s": []})


def test_generated_campus_topology(tmp_path):
    topo, ids = campus(3, 2)
    assert len(topo["devices"]["switches"]) == 3 and len(topo["devices"]["hosts"]) == 6
    d = write_config_dir(str(tmp_path / "c"), 3, 2)
    config = load_topology_config(pathlib.Path(d) / "topology.yaml")
    assert len(config.devices) == 1 + 3 + 6 + 2
    assert len(config.links) == 3 + 6 + 2 + 3   # router uplinks, hosts, servers, ring
    registry = IdentityRegistry.from_config(config, pathlib.Path(d) / "identities.yaml")
    assert registry.get("h1-1").role == "admin" and registry.get("h3-1").role == "contractor"
    assert (pathlib.Path(d) / "zero_trust_policy.yaml").exists()
    with pytest.raises(ValueError):
        campus(1, 2)


def test_provenance():
    p = provenance.collect({"x": 1})
    assert p["settings"] == {"x": 1} and p["simulated"] is True
    assert "enterprise.yaml" in p["config_sha256"] and len(p["config_sha256"]["enterprise.yaml"]) == 64


def test_runner_subset(tmp_path):
    from network.experiments.runner import run

    results = run("quick", str(tmp_path), only=("security", "scalability"))
    assert set(results) >= {"security", "scalability", "provenance", "figures"}
    assert results["security"]["summary"]["zero_trust"]["detection_auroc"]["n"] == 2
    assert [r["devices"] for r in results["scalability"]["rows"]] == [13, 23]
    saved = json.loads((tmp_path / "results.json").read_text())
    assert saved["profile"] == "quick"
    report = (tmp_path / "report.md").read_text()
    assert "SIMULATED" in report and "## Zero Trust" in report and "## Scalability" in report
    assert (tmp_path / "security.csv").exists() and (tmp_path / "figures" / "scalability.svg").exists()
