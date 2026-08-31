#!/usr/bin/env python3
"""Create a deterministic synthetic run for UI testing without provider data."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
import math
from pathlib import Path


def build_demo() -> dict[str, object]:
    tickers = (("DEMOA", 100.0, "BULLISH", 63), ("DEMOB", 72.0, "BEARISH", 57), ("DEMOC", 145.0, "NEUTRAL", 41))
    watchlist = []
    for rank, (ticker, anchor, direction, score) in enumerate(tickers, start=1):
        bars = []
        start = date(2026, 1, 2)
        for index in range(90):
            day = start + timedelta(days=index)
            close = anchor * (1 + 0.0015 * index + 0.045 * math.sin(index / 8 + rank))
            bars.append({"date": day.isoformat(), "close": round(close, 2)})
        price = bars[-1]["close"]
        watchlist.append({
            "ticker": ticker,
            "trade_rank": rank,
            "action": "NO_RECOMMENDATION",
            "gates": {"data_ready": False, "calibrated": False, "execution_ready": False},
            "price": {"value": price, "as_of": "2026-05-01"},
            "return_1d_pct": round((bars[-1]["close"] / bars[-2]["close"] - 1) * 100, 3),
            "technical": {"bars": bars, "ema20": price * .99, "ema50": price * .97, "rsi14": 55, "rv20_ann_pct": 38, "coverage_status": "SYNTHETIC_DEMO"},
            "trade_thesis": {"direction": direction, "conviction_score": score, "trigger_reference": "Synthetic trigger for layout testing.", "invalidation_reference": "Synthetic invalidation for layout testing."},
            "data_quality": {"complete": False, "freshness": "SYNTHETIC", "note": "Synthetic demo. No market or provider claim."},
            "freshness_contract": {"overall": "UNAVAILABLE", "latest_complete_session": "2026-05-01", "datasets": {}},
            "edge": {"dimensions": {"directional_edge": score, "evidence_quality": 0, "tradeability": 0}, "forecast_v4": {"status": "UNAVAILABLE_SYNTHETIC_DEMO"}},
            "provenance": {"provider": ["SYNTHETIC_DEMO"], "snapshot_ids": [], "as_of": "2026-05-01T10:45:00Z"},
        })
    return {"run_id": "2026-05-01-synthetic-demo", "generated_at": "2026-05-01T10:45:00Z", "cutoff_at": "2026-05-01T10:45:00Z", "mode": "SYNTHETIC_DEMO", "watchlist": watchlist}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("examples/synthetic-demo-run.json"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(build_demo(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
