#!/usr/bin/env python3
"""Resume a quota-aware, decision-relevant two-year provider backfill."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from morning_edge.backfill import BACKFILL_DATASETS, CoverageState, collect, make_plan  # noqa: E402
from morning_edge.config import Settings  # noqa: E402
from morning_edge.providers.budget import (  # noqa: E402
    API_BASIC_DAILY_CAP,
    API_BASIC_DAILY_RESERVE,
    API_BASIC_ROLLING_WINDOW,
    WeeklyRequestBudget,
)
from morning_edge.providers.unusual_whales import UnusualWhalesClient  # noqa: E402
from morning_edge.store import SnapshotStore  # noqa: E402


DEFAULT_DATASETS = (
    "ohlc",
    "earnings",
    "option_chain",
    "dealer_exposure",
    "flow_alerts",
)
RETRY_ATTEMPTS = 3


def _market_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=_market_date, required=True)
    parser.add_argument("--end-date", type=_market_date, required=True)
    parser.add_argument("--ticker", action="append", default=[])
    parser.add_argument("--dataset", action="append", choices=sorted(BACKFILL_DATASETS), default=[])
    parser.add_argument("--batch-items", type=int, default=100)
    parser.add_argument("--reserve", type=int, default=API_BASIC_DAILY_RESERVE)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--audit-accepted", action="store_true")
    return parser


def _compact(dataset: str, result: dict[str, Any], remaining: int) -> dict[str, Any]:
    return {
        "dataset": dataset,
        "attempted_transport_items": result["attempted_logical_items"],
        "captured_nonempty_responses": result["captured_nonempty_responses"],
        "empty_responses": result["empty_responses"],
        "failed_requests": result["failed_requests"],
        "state_counts": result["state_counts"],
        "remaining_before_reserve": remaining,
    }


def main() -> int:
    args = _parser().parse_args()
    if not args.live or not args.audit_accepted:
        raise SystemExit("two-year backfill requires --live --audit-accepted")
    if not 1 <= args.batch_items <= 2_000:
        raise SystemExit("--batch-items must be between 1 and 2000")
    if not 0 <= args.reserve < API_BASIC_DAILY_CAP:
        raise SystemExit(f"--reserve must be between 0 and {API_BASIC_DAILY_CAP - 1}")
    if args.start_date > args.end_date:
        raise SystemExit("--start-date cannot be after --end-date")

    settings = Settings.from_env()
    if settings.provider_name != "unusual_whales" or settings.provider_api_key is None:
        raise SystemExit("a configured Unusual Whales provider key is required")
    tickers = tuple(args.ticker) or settings.watchlist
    datasets = tuple(args.dataset) or DEFAULT_DATASETS
    report: dict[str, Any] = {
        "schema": "codex_screener/two_year_backfill/v1",
        "start_date": args.start_date.isoformat(),
        "end_date": args.end_date.isoformat(),
        "tickers": list(tickers),
        "datasets": list(datasets),
        "request_window_hours": 24,
        "request_cap": API_BASIC_DAILY_CAP,
        "protected_reserve": args.reserve,
        "excluded_by_default": {
            dataset: reason
            for dataset, reason in {
                "dark_pool": "large raw footprint and lower directional value",
                "open_interest": "paged endpoint duplicates canonical consecutive-chain OI deltas",
            }.items()
            if dataset not in datasets
        },
        "stages": [],
    }

    with (
        SnapshotStore(settings.database_path) as snapshots,
        WeeklyRequestBudget(
            settings.provider_usage_path,
            weekly_cap=API_BASIC_DAILY_CAP,
            protected_reserve=args.reserve,
            rolling_window=API_BASIC_ROLLING_WINDOW,
        ) as budget,
    ):
        client = UnusualWhalesClient(settings.provider_api_key, request_budget=budget)
        for dataset in datasets:
            plan = make_plan(
                provider="unusual_whales",
                start_date=args.start_date,
                end_date=args.end_date,
                tickers=tickers,
                datasets=(dataset,),
            )
            stage: dict[str, Any] = {"dataset": dataset, "plan_id": plan.plan_id, "batches": []}
            report["stages"].append(stage)
            while True:
                usage = budget.usage()
                batch_cap = min(args.batch_items, usage.remaining_before_reserve // RETRY_ATTEMPTS)
                if batch_cap < 1:
                    stage["status"] = "budget_exhausted"
                    break
                result = collect(
                    client=client,
                    snapshots=snapshots,
                    plan=plan,
                    max_requests=batch_cap,
                    audit_accepted=True,
                )
                summary = _compact(dataset, result, budget.usage().remaining_before_reserve)
                stage["batches"].append(summary)
                print(json.dumps(summary, sort_keys=True), flush=True)
                states = result["state_counts"]
                actionable = sum(
                    int(states[state.value])
                    for state in (
                        CoverageState.PLANNED,
                        CoverageState.FAILED,
                        CoverageState.BUDGET_EXHAUSTED,
                    )
                )
                if actionable == 0:
                    stage["status"] = "terminal"
                    stage["final_state_counts"] = states
                    break
                if result["attempted_logical_items"] == 0:
                    stage["status"] = "no_progress"
                    stage["final_state_counts"] = states
                    break

        report["usage_after"] = budget.usage().public_dict()

    args.report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(args.report, 0o600)
    print(json.dumps({"status": "finished", "report": str(args.report)}, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
