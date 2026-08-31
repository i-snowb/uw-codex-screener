#!/usr/bin/env python3
"""Generate a cutoff-bound Codex analysis batch from a fresh morning_run/v1 artifact.

The output is deliberately conditional. It ranks the supplied trade theses by
their uncalibrated conviction score, cites only the current run, and never turns
provider observations or historical analogs into an executable instruction.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from private_artifacts import write_private_bytes


def number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def money(value: object) -> str:
    parsed = number(value)
    return "—" if parsed is None else f"${parsed:,.2f}".rstrip("0").rstrip(".")


def percent(value: object, *, already_percent: bool = False) -> str:
    parsed = number(value)
    if parsed is None:
        return "—"
    scaled = parsed if already_percent else parsed * 100
    return f"{scaled:.1f}%"


def mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def source_id(record: Mapping[str, Any], dataset: str) -> int:
    collection = mapping(record.get("collection"))
    for item in collection.get("datasets", []):
        if isinstance(item, Mapping) and item.get("dataset") == dataset:
            value = item.get("snapshot_id")
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    raise ValueError(f"{record.get('ticker')} is missing a {dataset} snapshot")


def posture(record: Mapping[str, Any]) -> str:
    price = number(mapping(record.get("price")).get("value"))
    evidence = mapping(record.get("evidence"))
    price_evidence = mapping(evidence.get("price"))
    ema20, ema50 = number(price_evidence.get("ema_20")), number(price_evidence.get("ema_50"))
    if price is not None and ema20 is not None and ema50 is not None:
        if price > ema20 and price > ema50:
            return "BULLISH_TREND"
        if price < ema20 and price < ema50:
            return "BEARISH_TREND"
    return "MIXED"


def confidence(record: Mapping[str, Any]) -> int:
    edge = mapping(record.get("edge"))
    dimensions = mapping(edge.get("dimensions"))
    quality = number(dimensions.get("evidence_quality")) or 0
    analogs = mapping(edge.get("historical_analogs"))
    sample = number(analogs.get("sample_size")) or 0
    complete = bool(mapping(record.get("data_quality")).get("complete"))
    result = quality * 0.72 + min(sample, 8) * 2.2 + (8 if complete else 0)
    if sample < 5:
        result -= 12
    return max(25, min(88, round(result)))


def record_analysis(record: Mapping[str, Any]) -> dict[str, Any]:
    ticker = str(record["ticker"])
    evidence = mapping(record.get("evidence"))
    price_data = mapping(evidence.get("price"))
    gex = mapping(evidence.get("gex"))
    news = mapping(evidence.get("news"))
    edge = mapping(record.get("edge"))
    forecast = mapping(edge.get("forecast"))
    forecast_v4 = mapping(edge.get("forecast_v4"))
    analogs = mapping(edge.get("historical_analogs"))
    horizon20 = mapping(mapping(analogs.get("horizons")).get("20"))
    surface = mapping(edge.get("option_surface"))
    flow = mapping(edge.get("flow_conviction"))
    thesis = mapping(record.get("trade_thesis"))
    option = mapping(thesis.get("option_reference"))
    price = number(mapping(record.get("price")).get("value"))
    price_session = str(mapping(record.get("price")).get("as_of") or price_data.get("latest_market_date") or "prior session")[:10]
    ema20, ema50 = number(price_data.get("ema_20")), number(price_data.get("ema_50"))
    put_wall, flip, call_wall = (
        number(gex.get("put_wall")), number(gex.get("gamma_flip")), number(gex.get("call_wall"))
    )
    trend_text = "relative to incomplete moving-average history"
    if price is not None and ema20 is not None and ema50 is not None:
        trend_text = "below EMA20 and EMA50" if price < ema20 and price < ema50 else (
            "above EMA20 and EMA50" if price > ema20 and price > ema50 else "between EMA20 and EMA50"
        )
    sample = number(forecast.get("sample_size"))
    direction = str(thesis.get("direction") or "NEUTRAL")
    conviction = number(thesis.get("conviction_score")) or 0
    forecast_center = number(forecast.get("center_return_20d"))
    forecast_low = number(forecast.get("low_return_20d"))
    forecast_high = number(forecast.get("high_return_20d"))
    forecast_available = all(value is not None for value in (forecast_center, forecast_low, forecast_high, sample))
    v4_center_5 = number(forecast_v4.get("center_return_5d"))
    v4_low_5 = number(forecast_v4.get("p10_return_5d"))
    v4_high_5 = number(forecast_v4.get("p90_return_5d"))
    v4_center_20 = number(forecast_v4.get("center_return_20d"))
    v4_low_20 = number(forecast_v4.get("p10_return_20d"))
    v4_high_20 = number(forecast_v4.get("p90_return_20d"))
    analog_disposition = str(analogs.get("historical_disposition") or "UNAVAILABLE")
    analog_median = number(horizon20.get("median_return"))
    price_source = source_id(record, "ohlc")
    gex_source = source_id(record, "dealer_exposure")
    chain_source = source_id(record, "option_chain")
    flow_source = source_id(record, "flow_alerts")
    news_source = source_id(record, "news")
    upper_values = [value for value in (flip, call_wall, ema20) if value is not None]
    lower_values = [value for value in (put_wall, flip, ema20) if value is not None]
    upper_trigger = max(upper_values) if upper_values else price
    lower_trigger = min(lower_values) if lower_values else price
    gex_date = str(gex.get("latest_market_date") or "unavailable")
    contract = str(option.get("contract") or "No contract")
    option_type = str(option.get("type") or "option").lower()
    option_expiry = str(option.get("expiry") or "expiry unavailable")
    news_rows = news.get("latest_headlines")
    has_news = isinstance(news_rows, list) and bool(news_rows)

    evidence_points = [
        {
            "statement": (
                f"The {money(price)} {price_session} regular-session close was {trend_text}; "
                f"the 1-, 5-, and 20-session returns were "
                f"{percent(price_data.get('return_1d_pct'), already_percent=True)}, "
                f"{percent(price_data.get('return_5d'))}, and {percent(price_data.get('return_20d'))}."
            ),
            "field_refs": [
                "evidence.price.latest_close", "evidence.price.return_1d_pct",
                "evidence.price.return_5d", "evidence.price.return_20d",
                "evidence.price.ema_20", "evidence.price.ema_50",
            ],
            "source_snapshot_ids": [price_source],
        },
        {
            "statement": (
                f"The {gex_date} positioning map placed put wall, gamma flip, and call wall at "
                f"{money(put_wall)}, {money(flip)}, and {money(call_wall)}; these are modeled references, not support or resistance guarantees."
            ),
            "field_refs": [
                "evidence.gex.latest_market_date", "evidence.gex.put_wall",
                "evidence.gex.gamma_flip", "evidence.gex.call_wall",
            ],
            "source_snapshot_ids": [gex_source],
        },
        ({
            "statement": (
                f"V3 centers at {percent(forecast_center)} over 20 sessions. The V4 shadow centers at "
                f"{percent(v4_center_5)} over one week and {percent(v4_center_20)} over 20 sessions, with "
                f"p10-p90 ranges of {percent(v4_low_5)} to {percent(v4_high_5)} and "
                f"{percent(v4_low_20)} to {percent(v4_high_20)}. V4 is not calibrated."
            ),
            "field_refs": [
                "edge.forecast.status", "edge.forecast.center_return_20d",
                "edge.forecast.low_return_20d", "edge.forecast.high_return_20d",
                "edge.forecast_v4.center_return_5d", "edge.forecast_v4.center_return_20d",
                "edge.forecast_v4.p10_return_20d", "edge.forecast_v4.p90_return_20d",
            ],
            "source_snapshot_ids": [price_source, gex_source],
        } if forecast_available else {
            "statement": (
                f"The 20-session forecast is unavailable because matched-state history is insufficient; "
                f"the historical layer contains only n={int(number(analogs.get('sample_size')) or 0)} candidates."
            ),
            "field_refs": [
                "edge.forecast.status", "edge.forecast.reason",
                "edge.historical_analogs.status", "edge.historical_analogs.sample_size",
            ],
            "source_snapshot_ids": [price_source, gex_source],
        }),
        {
            "statement": (
                f"The stored {contract} {option_type} reference expires {option_expiry}; front IV is "
                f"{percent(surface.get('front_iv'))}, while flow remains {str(flow.get('status') or 'unavailable')} and unconfirmed."
            ),
            "field_refs": [
                "trade_thesis.option_reference.contract", "trade_thesis.option_reference.type",
                "trade_thesis.option_reference.expiry", "edge.option_surface.front_iv",
                "edge.flow_conviction.status",
            ],
            "source_snapshot_ids": [chain_source, flow_source],
        },
    ]
    if has_news:
        evidence_points[-1] = {
            "statement": "The latest provider headline is retained as a catalyst lead; its factual content and causal market impact are not independently verified by this run.",
            "field_refs": [
                "evidence.news.latest_headlines[0].headline",
                "evidence.news.latest_headlines[0].published_at",
                "edge.news_signal.major_count",
            ],
            "source_snapshot_ids": [news_source],
        }

    mismatch = str(forecast.get("direction") or "UNAVAILABLE") != direction
    counterevidence = [
        f"The matched-state sample is only n={int(sample) if sample is not None else 0}; its observed frequencies and range are descriptive, not calibrated probabilities.",
        f"Flow status is {str(flow.get('status') or 'unavailable')}; aggregate premium and OI changes do not identify buyer intent or opening-versus-closing activity.",
        f"The {contract} reference is not execution-fresh and does not pass calibration or order-entry gates.",
    ]
    if mismatch:
        counterevidence.append(
            f"The trade thesis is {direction} while the model direction is {str(forecast.get('direction') or 'UNAVAILABLE')}, so directional evidence is internally conflicted."
        )

    day_outlook = (
        f"Prior-session evidence leaves a {direction.lower()} thesis at {conviction:.0f}/100. "
        f"Confirm it only at the stated price trigger; the small matched sample and stored derivatives data limit conviction."
    )
    summary = (
        f"{ticker} closed at {money(price)}, {trend_text}. V4 points to {percent(v4_center_5)} over one week "
        f"and {percent(v4_center_20)} over 20 sessions, while V3 points to {percent(forecast_center)}. "
        f"The key price test is the {money(flip)} GEX flip; the matched sample and model disagreement limit the read."
    )
    return {
        "ticker": ticker,
        "action": "NO_RECOMMENDATION",
        "posture": posture(record),
        "research_priority": max(0, min(100, round(conviction))),
        "evidence_confidence": confidence(record),
        "day_outlook": day_outlook,
        "summary": summary,
        "evidence_points": evidence_points,
        "counterevidence": counterevidence[:4],
        "scenarios": [
            {
                "name": "BULL",
                "conditions": [
                    f"Fresh regular-session price establishes acceptance above {money(upper_trigger)}",
                    "Current chain and company-specific evidence confirm rather than contradict the move",
                ],
                "outcome": "The bullish conditional case gains support and the stored overhead references become the next areas to reassess.",
                "invalidation": [
                    f"Validated price returns below {money(flip)}",
                    "Current evidence fails to confirm the breakout state",
                ],
            },
            {
                "name": "BASE",
                "conditions": [
                    f"Price remains inside the stored {money(put_wall)} to {money(call_wall)} positioning range",
                    "No material company-specific catalyst is independently validated",
                ],
                "outcome": "The prior-session range and conflicting evidence remain the governing research state.",
                "invalidation": [
                    "A validated directional break persists outside the stored range",
                    "A verified event materially changes volatility or trend conditions",
                ],
            },
            {
                "name": "BEAR",
                "conditions": [
                    f"Fresh regular-session price establishes acceptance below {money(lower_trigger)}",
                    "Current trend and company-specific evidence confirm rather than contradict the move",
                ],
                "outcome": "The bearish conditional case gains support and the lower stored references become the next areas to reassess.",
                "invalidation": [
                    f"Validated price recovers above {money(flip)}",
                    "Current evidence fails to confirm the breakdown state",
                ],
            },
        ],
        "option_context": (
            f"{contract} ({option_type}, {option_expiry}) is a model-selected, non-actionable reference. "
            "The stored quote is not execution-fresh, the thesis is uncalibrated, and no order-entry gate is enabled."
        ),
        "unknowns": [
            "Fresh next-session underlying price and executable NBBO",
            "Opening-versus-closing character and beneficial-owner intent behind aggregate flow",
            "Out-of-sample calibration and friction-adjusted performance of the analog model",
        ],
    }


def write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).encode() + b"\n"
    write_private_bytes(path, encoded)


def build_batch(run: Mapping[str, Any], tickers: Sequence[str] = ()) -> dict[str, Any]:
    if run.get("run_schema_version") != "morning_run/v1":
        raise ValueError("input must use morning_run/v1")
    watchlist = run.get("watchlist")
    if not isinstance(watchlist, list) or not watchlist:
        raise ValueError("input watchlist is missing")
    selected = {ticker.strip().upper() for ticker in tickers if ticker.strip()}
    records = [
        record_analysis(record)
        for record in watchlist
        if isinstance(record, Mapping)
        and (not selected or str(record.get("ticker", "")).upper() in selected)
    ]
    if selected:
        found = {record["ticker"] for record in records}
        missing = sorted(selected - found)
        if missing:
            raise ValueError(f"input watchlist is missing requested tickers: {', '.join(missing)}")
    source_by_ticker = {
        str(record.get("ticker")): record for record in watchlist if isinstance(record, Mapping)
    }
    conviction_order = sorted(
        (record["ticker"] for record in records),
        key=lambda ticker: (
            -(number(mapping(source_by_ticker[ticker].get("trade_thesis")).get("conviction_score")) or 0),
            ticker,
        ),
    )
    priority_by_ticker = {ticker: 100 - index for index, ticker in enumerate(conviction_order)}
    for record in records:
        record["research_priority"] = priority_by_ticker[record["ticker"]]
    return {
        "schema": "codex_agent_enrichment/v1",
        "records": records,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ticker", action="append", default=[], help="Emit only this ticker; repeat for bounded batches")
    args = parser.parse_args(argv)
    run = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(run, Mapping):
        raise SystemExit("input JSON root must be an object")
    write_atomic(args.output, build_batch(run, args.ticker))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
