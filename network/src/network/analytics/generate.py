"""Generate a labelled, SIMULATED dataset from random failure scenarios.

Usage (from the repository root; needs the ``mininet`` Python package to
import the controller, but no running Mininet):

    PYTHONPATH=network/src python3 -m network.analytics.generate \\
        --out data/run1 --duration 7200 --failures 12 --seed 1

Writes ``links.csv``, ``nodes.csv``, ``metadata.json`` and, with
``--keep-db``, ``telemetry.db`` (SQLite) into ``--out``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from network.analytics.dataset import build_dataset, dataset_metadata, write_csv, write_metadata
from network.analytics.features import FeatureExtractor
from network.analytics.repository import TelemetryRepository
from network.analytics.scenarios import ScenarioRunner, generate_plan
from network.config import load_topology_config

DEFAULT_CONFIG = os.path.join(os.path.dirname(__file__), "..", "..", "..", "config", "enterprise.yaml")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="network.analytics.generate", description=__doc__.split("\n")[0])
    p.add_argument("--out", required=True, help="output directory (created if missing)")
    p.add_argument("--config", default=os.path.normpath(DEFAULT_CONFIG), help="topology YAML")
    p.add_argument("--duration", type=float, default=3600.0, help="simulated seconds (default 3600)")
    p.add_argument("--failures", type=int, default=8, help="number of failures (default 8)")
    p.add_argument("--seed", type=int, default=0, help="random seed (default 0)")
    p.add_argument("--step", type=float, default=5.0, help="collection interval in seconds (default 5)")
    p.add_argument("--precursor-probability", type=float, default=0.5,
                   help="chance a failure gets a synthetic warning ramp (default 0.5; 0 disables)")
    p.add_argument("--horizon", type=float, default=60.0, help="prediction horizon in seconds (default 60)")
    p.add_argument("--windows", default="30,120", help="feature windows in seconds (default 30,120)")
    p.add_argument("--keep-db", action="store_true", help="also write the SQLite telemetry database")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    config = load_topology_config(args.config)
    plan = generate_plan(config, args.duration, seed=args.seed, n_failures=args.failures,
                         step_s=args.step, precursor_probability=args.precursor_probability)

    db_path = os.path.join(args.out, "telemetry.db") if args.keep_db else ":memory:"
    if args.keep_db and os.path.exists(db_path):
        os.remove(db_path)
    repository = TelemetryRepository(db_path)
    result = ScenarioRunner(plan, config=config, repository=repository).run()

    extractor = FeatureExtractor(step_s=args.step,
                                 windows_s=[float(w) for w in args.windows.split(",")])
    datasets = build_dataset(result, extractor, horizon_s=args.horizon)
    for kind, rows in datasets.items():
        write_csv(rows, os.path.join(args.out, f"{kind}s.csv"))
    metadata = dataset_metadata(result, datasets, extractor, args.horizon)
    write_metadata(metadata, os.path.join(args.out, "metadata.json"))
    repository.close()

    print(json.dumps({
        "out": args.out,
        "scenario": metadata["scenario"],
        "rows": {k: v["labels"]["rows"] for k, v in metadata["datasets"].items()},
        "simulated": True,
        "synthetic_precursors": metadata["synthetic_precursors"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
