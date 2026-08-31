#!/usr/bin/env python3
"""Capture the bounded supplemental signal set and refresh its sidecar."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from morning_edge.config import Settings  # noqa: E402
from morning_edge.daily import write_morning_run  # noqa: E402
from morning_edge.enhanced_collection import EnhancedDataset, collect_enhanced  # noqa: E402
from morning_edge.enhanced_features import build_enhanced_summary  # noqa: E402
from morning_edge.providers.budget import WeeklyRequestBudget  # noqa: E402
from morning_edge.providers.unusual_whales import UnusualWhalesClient  # noqa: E402
from morning_edge.store import SnapshotStore  # noqa: E402


SUPPLEMENTAL_DATASETS = (
    EnhancedDataset.STOCK_STATE,
    EnhancedDataset.OPTION_PRICE_LEVELS,
    EnhancedDataset.VOLATILITY_ANOMALY,
    EnhancedDataset.VOLATILITY_CHARACTER,
    EnhancedDataset.VARIANCE_RISK_PREMIUM,
    EnhancedDataset.ETF_TIDE_QQQ,
    EnhancedDataset.ETF_TIDE_SMH,
    EnhancedDataset.ETF_TIDE_SOXX,
    EnhancedDataset.ECONOMIC_CALENDAR,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--audit-accepted", action="store_true")
    args = parser.parse_args()
    if not args.live or not args.audit_accepted:
        raise SystemExit("supplemental capture requires --live --audit-accepted")

    settings = Settings.from_env()
    if settings.provider_name != "unusual_whales" or settings.provider_api_key is None:
        raise SystemExit("a configured Unusual Whales provider is required")
    with (
        SnapshotStore(settings.database_path) as store,
        WeeklyRequestBudget(settings.provider_usage_path) as budget,
    ):
        client = UnusualWhalesClient(settings.provider_api_key, request_budget=budget)
        report = collect_enhanced(
            client=client,
            snapshots=store,
            request_budget=budget,
            tickers=settings.watchlist,
            datasets=SUPPLEMENTAL_DATASETS,
        )
    if not report.preflight_passed:
        raise SystemExit("supplemental capture was blocked by the protected provider reserve")
    summary = build_enhanced_summary(settings.database_path)
    summary["capture_report"] = report.to_dict()
    summary["capture_scope"] = "supplemental_decision_signals"
    write_morning_run(args.output, summary)
    print(report.to_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
