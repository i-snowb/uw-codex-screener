#!/usr/bin/env python3
"""Audit local evidence and run bounded, retrospective shadow experiments."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from morning_edge.experiments import (ABLATION_FEATURES, ABLATION_PAIRS, ExperimentPlan, digest,
                                      encoded, make_examples, run_experiment, signal_contribution_report)
from morning_edge.research_audit import audit_inputs, frozen_cohorts, load_development_data
from private_artifacts import write_private_bytes


def immutable_json(path: Path, value: object) -> None:
    body = encoded(value)
    if path.is_symlink():
        raise ValueError("artifact cannot be a symlink")
    if path.exists():
        if path.read_bytes() != body:
            raise FileExistsError("artifact differs; use a new experiment directory")
        return
    write_private_bytes(path, body)


def source_identity() -> dict:
    paths = [Path(__file__), ROOT / "scripts/private_artifacts.py"]
    # Pin the whole Python implementation, including calendar/normalization rules.
    paths += sorted((ROOT / "src/morning_edge").glob("*.py"))
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Stored raw publication input")
    parser.add_argument("--database", required=True, type=Path, help="Read-only snapshot and forecast database")
    parser.add_argument("--output", required=True, type=Path, help="New private experiment directory")
    parser.add_argument("--benchmark", choices=("SPY", "QQQ", "SMH", "SOXX", "IWM"), default="QQQ")
    parser.add_argument("--run-development", action="store_true")
    parser.add_argument("--run-signal-ablation", action="store_true")
    parser.add_argument("--accept-retrospective-limitations", action="store_true")
    args = parser.parse_args(argv)
    if args.run_development and not args.accept_retrospective_limitations:
        parser.error("--run-development requires --accept-retrospective-limitations")
    if args.run_signal_ablation and not args.run_development:
        parser.error("--run-signal-ablation requires --run-development")
    raw = args.input.read_bytes()
    run = json.loads(raw)
    plan, source = ExperimentPlan(), source_identity()
    registration = {"schema_version": "offline-experiment-registration-v1",
        "input_sha256": hashlib.sha256(raw).hexdigest(), "code_files": source,
        "benchmark": args.benchmark, "plan": asdict(plan),
        "hypothesis": "Adding benchmark-relative 5D/20D features to a pooled ridge model improves held-out equal-origin MAE over the same ridge model with price-only features.",
        "primary_endpoint": "MAE improvement of ridge_market over ridge_price, reported separately by horizon and absolute/excess target. No pooled winner selection.",
        "decision_rule": "Development screening only. No promotion or calibrated probability claim, regardless of apparent improvement.",
        "run_development": args.run_development,
        "signal_ablation": {"enabled": args.run_signal_ablation, "additional_models": ABLATION_FEATURES,
                            "paired_tests": ABLATION_PAIRS,
                            "endpoint": "Separate paired equal-origin MAE by group, horizon, and target; no winner selection or tuning.",
                            "scope": "Follow-up exploratory analysis of previously inspected development data; not fresh confirmation. Existing holdout remains unscored."},
        "holdout_policy": "Reserve the last 63 eligible origin dates; exclude earlier labels reaching that boundary; never score the holdout here.",
        "cost_boundary": "Close-to-close research labels; costs, fills, and option returns not modeled."}
    immutable_json(args.output / "registration.json", registration)
    dataset = load_development_data(args.database, run, args.benchmark, include_reconciliation=True)
    reconciliation = dataset.pop("reconciliation")
    immutable_json(args.output / "dataset.json", dataset)
    immutable_json(args.output / "reconciliation.json", reconciliation)
    audit = audit_inputs(run, dataset)
    immutable_json(args.output / "integrity-audit.json", audit)
    frozen = frozen_cohorts(args.database)
    immutable_json(args.output / "frozen-cohorts.json", frozen)
    experiment = None
    if args.run_development:
        if audit["status"] == "INVALID_INPUT":
            raise ValueError("invalid inputs; inspect integrity-audit.json before development")
        examples, exclusions = make_examples(dataset["series"], args.benchmark, plan.horizons)
        experiment = run_experiment(examples, plan, source_digest=digest(dataset), include_ablation=args.run_signal_ablation)
        experiment["code_sha256"] = digest(source)
        experiment["experiment_id"] = digest({"registration": registration, "dataset": digest(dataset)})
        experiment["example_exclusions"] = exclusions
        experiment["benchmark"] = args.benchmark
        immutable_json(args.output / "development-results.json", experiment)
        if args.run_signal_ablation:
            immutable_json(args.output / "signal-research.json", signal_contribution_report(experiment, capture_cutoff=run["cutoff_at"]))
    summary = {"status": experiment["status"] if experiment else "AUDIT_ONLY",
        "audit_status": audit["status"], "frozen_forecasts_compared": frozen["selected_forecasts"],
        "reconciliation_status": reconciliation["status"],
        "internal_price_gaps": sum(len(row["gaps"]) for row in reconciliation["tickers"]),
        "development_folds": len(experiment["folds"]) if experiment else 0,
        "development_prediction_rows": len(experiment["predictions"]) if experiment else 0,
        "holdout": experiment["holdout"] if experiment else None,
        "promotion_eligible": False, "active_engine_changed": False,
        "files": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sorted(args.output.glob("*.json")) if path.name != "manifest.json"}}
    immutable_json(args.output / "manifest.json", summary)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
