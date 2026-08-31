#!/usr/bin/env python3
"""Render a fail-closed, self-contained Codex Screener HTML fragment.

The renderer deliberately consumes a prepared JSON run artifact instead of the
SQLite archive.  It is therefore safe to run in a static/reporting context and
cannot make provider requests or turn archive data into a trading action.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from private_artifacts import write_private_text


MAX_BARS = 180
MAX_TEXT = 480
MAX_ITEMS = 12


def _text(value: object, default: str = "—") -> str:
    if value is None:
        return default
    value = str(value).strip()
    return value[:MAX_TEXT] if value else default


def _plain_option_context(value: object) -> str:
    text = _text(value, "No validated option context is available.")
    return text.replace(
        "is a model-selected, non-actionable reference.",
        "is a screened reference contract, not an actionable order.",
    )


def _items(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [_text(item) for item in value[:MAX_ITEMS] if _text(item) != "—"]


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _bars(value: object) -> list[dict[str, float | str]]:
    """Keep only valid, bounded close/EMA chart observations."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    parsed: list[dict[str, float | str]] = []
    for item in value:
        item = _mapping(item)
        date = item.get("date", item.get("d"))
        close = _number(item.get("close", item.get("c")))
        if not isinstance(date, str) or close is None:
            continue
        row: dict[str, float | str] = {"d": date[:10], "c": round(close, 6)}
        for source, target in (("ema20", "e20"), ("ema50", "e50")):
            number = _number(item.get(source, item.get(target)))
            if number is not None:
                row[target] = round(number, 6)
        parsed.append(row)
    parsed.sort(key=lambda item: str(item["d"]))
    return parsed[-MAX_BARS:]


def _gate(entry: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Return a display action, only allowing a supplied action through strict gates."""
    requested = _text(entry.get("action", entry.get("recommendation")), "NO_RECOMMENDATION")
    requested = requested.upper().replace(" ", "_")
    gates = _mapping(entry.get("gates", entry.get("data_gate")))
    quality = _mapping(entry.get("data_quality", entry.get("quality")))
    reasons: list[str] = []
    for key, label in (
        ("data_ready", "required data is incomplete"),
        ("calibrated", "model calibration is not validated"),
        ("execution_ready", "execution inputs are not ready"),
    ):
        if gates.get(key) is not True:
            reasons.append(label)
    if quality.get("complete") is not True:
        reasons.append("data-quality completeness is not confirmed")
    if requested in {"", "—", "BUY", "SELL", "TRIM", "EXIT"} and reasons:
        return "NO_RECOMMENDATION", reasons
    if requested in {"", "—"}:
        return "NO_RECOMMENDATION", reasons or ["no model action was supplied"]
    return requested, reasons


def _candidate(candidate: Mapping[str, Any], parent_gates: Mapping[str, Any]) -> dict[str, Any]:
    strike = _number(candidate.get("strike"))
    expiry = candidate.get("expiry", candidate.get("expiration"))
    option_type = _text(candidate.get("option_type", candidate.get("type")), "").upper()
    bid, ask = _number(candidate.get("bid")), _number(candidate.get("ask"))
    executable = (
        parent_gates.get("data_ready") is True
        and parent_gates.get("execution_ready") is True
        and strike is not None
        and isinstance(expiry, str)
        and option_type in {"CALL", "PUT"}
        and bid is not None and ask is not None and 0 <= bid <= ask
    )
    mechanics = _mapping(candidate.get("mechanics"))
    matrix_rows: list[dict[str, Any]] = []
    raw_matrix = mechanics.get("matrix")
    if isinstance(raw_matrix, Sequence) and not isinstance(raw_matrix, (str, bytes)):
        for matrix_row in raw_matrix[:7]:
            matrix_row = _mapping(matrix_row)
            points = matrix_row.get("points")
            clean_points = []
            if isinstance(points, Sequence) and not isinstance(points, (str, bytes)):
                clean_points = [{
                    "elapsed": _number(_mapping(point).get("elapsed_fraction")),
                    "days": _number(_mapping(point).get("remaining_days")),
                    "value": _number(_mapping(point).get("modeled_value")),
                    "return": _number(_mapping(point).get("return_on_ask")),
                } for point in points[:5]]
            matrix_rows.append({
                "move": _number(matrix_row.get("underlying_change_pct")),
                "price": _number(matrix_row.get("underlying_price")),
                "points": clean_points,
            })
    return {
        "contract": _text(candidate.get("contract", candidate.get("option_symbol"))),
        "type": option_type or "—",
        "strike": strike,
        "expiry": _text(expiry),
        "bid": bid,
        "ask": ask,
        "mid": _number(candidate.get("mid")),
        "dte": _number(candidate.get("dte")),
        "delta": _number(candidate.get("delta")),
        "iv": _number(candidate.get("implied_volatility", candidate.get("iv"))),
        "oi": _number(candidate.get("open_interest")),
        "volume": _number(candidate.get("volume")),
        "spreadPct": _number(candidate.get("spread_pct_of_mid")),
        "quoteAt": _text(candidate.get("quote_at")),
        "quoteFresh": candidate.get("quote_fresh") is True,
        "thesis": _text(candidate.get("thesis", candidate.get("rationale"))),
        "status": "CANDIDATE" if executable else "NOT_ELIGIBLE",
        "reason": "" if executable else _text(candidate.get("reason"), "Fresh executable chain fields and passed data gates are required."),
        "mechanics": {
            "status": _text(mechanics.get("status")),
            "method": _text(mechanics.get("method")),
            "breakeven": _number(mechanics.get("breakeven")),
            "breakevenMove": _number(mechanics.get("breakeven_move_pct")),
            "riskNeutralBreakeven": _number(mechanics.get("risk_neutral_breakeven_probability")),
            "physicalProbabilityAvailable": mechanics.get("physical_probability_available") is True,
            "matrix": matrix_rows,
        },
    }


def _edge_view(raw: Mapping[str, Any]) -> dict[str, Any]:
    edge = _mapping(raw.get("edge"))
    dimensions = _mapping(edge.get("dimensions"))
    surface = _mapping(edge.get("option_surface")); flow = _mapping(edge.get("flow_conviction"))
    oi = _mapping(edge.get("open_interest")); gex = _mapping(edge.get("gex_topology"))
    dark = _mapping(edge.get("dark_pool")); earnings = _mapping(edge.get("earnings_priors"))
    news = _mapping(edge.get("news_signal")); analogs = _mapping(edge.get("historical_analogs"))
    forecast = _mapping(edge.get("forecast"))
    forecast_v4 = _mapping(edge.get("forecast_v4"))
    challenger_suite = _mapping(edge.get("shadow_challengers"))
    challenger_models = challenger_suite.get("models")
    if not isinstance(challenger_models, Sequence) or isinstance(challenger_models, (str, bytes)):
        challenger_models = []
    horizons = _mapping(analogs.get("horizons"))
    analog_rows = analogs.get("analogs")
    if not isinstance(analog_rows, Sequence) or isinstance(analog_rows, (str, bytes)):
        analog_rows = []
    analog_path = analogs.get("path_distribution")
    if not isinstance(analog_path, Sequence) or isinstance(analog_path, (str, bytes)):
        analog_path = []
    excursion = _mapping(analogs.get("excursion_summary"))
    baseline = _mapping(analogs.get("baseline_comparison"))
    match = _mapping(analogs.get("match_quality"))
    derivative_match = _mapping(match.get("derivative_match"))
    stability = _mapping(analogs.get("stability"))
    history = surface.get("history")
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        history = []
    flow_history = flow.get("history")
    if not isinstance(flow_history, Sequence) or isinstance(flow_history, (str, bytes)):
        flow_history = []
    forecast_path = forecast.get("path")
    if not isinstance(forecast_path, Sequence) or isinstance(forecast_path, (str, bytes)):
        forecast_path = []
    forecast_v4_path = forecast_v4.get("path")
    if not isinstance(forecast_v4_path, Sequence) or isinstance(forecast_v4_path, (str, bytes)):
        forecast_v4_path = []
    forecast_v4_scaling = _mapping(forecast_v4.get("volatility_scaling"))
    why = raw.get("why_today")
    if not isinstance(why, Sequence) or isinstance(why, (str, bytes)):
        why = []
    return {
        "version": _text(edge.get("feature_version")),
        "attention": _number(edge.get("attention_score")),
        "dimensions": {
            "directional": _number(dimensions.get("directional_edge")),
            "volatility": _number(dimensions.get("long_volatility_attractiveness")),
            "positioning": _number(dimensions.get("positioning_context")),
            "tradeability": _number(dimensions.get("tradeability")),
            "catalystRisk": _number(dimensions.get("catalyst_risk")),
            "evidenceQuality": _number(dimensions.get("evidence_quality")),
            "calibrated": dimensions.get("calibrated_probability_available") is True,
        },
        "surface": {
            "status": _text(surface.get("status")), "date": _text(surface.get("market_date")),
            "frontIv": _number(surface.get("front_iv")), "frontDte": _number(surface.get("front_dte")),
            "backIv": _number(surface.get("back_iv")), "backDte": _number(surface.get("back_dte")),
            "termSlope": _number(surface.get("term_slope")), "skew25": _number(surface.get("put_call_skew_25d")),
            "ivPercentile": _number(surface.get("iv_percentile")), "ivRvGap": _number(surface.get("iv_rv_gap")),
            "medianSpread": _number(surface.get("median_spread_pct")), "historySessions": _number(surface.get("history_sessions")),
            "history": [{"date": _text(_mapping(item).get("date")), "iv": _number(_mapping(item).get("front_iv")),
                         "slope": _number(_mapping(item).get("term_slope"))} for item in history[-90:]],
        },
        "flow": {"status": _text(flow.get("status")), "alerts": _number(flow.get("alert_count")),
                 "grossPremium": _number(flow.get("gross_premium")),
                 "directionalPremium": _number(flow.get("directional_premium")),
                 "directionalShare": _number(flow.get("directional_share")), "openingShare": _number(flow.get("opening_share")),
                 "singleLegShare": _number(flow.get("single_leg_share")), "sweepShare": _number(flow.get("sweep_share")),
                 "multiLegShare": _number(flow.get("multileg_share")), "quality": _number(flow.get("quality_multiplier")),
                 "percentile": _number(flow.get("directional_percentile")),
                 "zscore": _number(flow.get("directional_zscore")),
                 "historySessions": _number(flow.get("history_sessions")), "oiConfirmed": flow.get("oi_confirmed") is True,
                 "history": [{"date": _text(_mapping(item).get("market_date")),
                              "premium": _number(_mapping(item).get("directional_premium")),
                              "share": _number(_mapping(item).get("directional_share")),
                              "alerts": _number(_mapping(item).get("alert_count"))}
                             for item in flow_history[-65:] if isinstance(item, Mapping)]},
        "oi": {"status": _text(oi.get("status")), "callChange": _number(oi.get("call_oi_change")),
               "putChange": _number(oi.get("put_oi_change")), "netChange": _number(oi.get("net_call_minus_put_change")),
               "nearCall": _number(oi.get("near_spot_call_change")), "nearPut": _number(oi.get("near_spot_put_change")),
               "callBuild": _number(oi.get("largest_call_build_strike")), "putBuild": _number(oi.get("largest_put_build_strike")),
               "matchedContracts": _number(oi.get("matched_contracts"))},
        "gex": {"status": _text(gex.get("status")), "regime": _text(gex.get("spot_regime")),
                "flipDistance": _number(gex.get("distance_to_flip_pct")), "flipChange": _number(gex.get("flip_change")),
                "callWallChange": _number(gex.get("call_wall_change")), "putWallChange": _number(gex.get("put_wall_change")),
                "historySessions": _number(gex.get("history_sessions")),
                "comparableHistorySessions": _number(gex.get("comparable_history_sessions")),
                "comparisonStatus": _text(gex.get("comparison_status")),
                "methodVersion": _text(gex.get("method_version")),
                "methodBoundary": _text(gex.get("method_boundary_date")),
                "nearbyFlips": [_number(value) for value in _items(gex.get("nearby_flips")) if _number(value) is not None]},
        "dark": {"status": _text(dark.get("status")), "level": _number(dark.get("dominant_price_level")),
                 "levelShare": _number(dark.get("dominant_level_share")), "levelDistance": _number(dark.get("distance_to_dominant_level_pct")),
                 "priceState": _text(dark.get("price_state")), "premiumAdv": _number(dark.get("premium_to_adv_ratio")),
                 "nearBid": _number(dark.get("near_bid_premium_share")), "nearAsk": _number(dark.get("near_ask_premium_share")),
                 "historySessions": _number(dark.get("history_sessions"))},
        "earnings": {"status": _text(earnings.get("status")), "eventCount": _number(earnings.get("event_count")),
                     "exceedRate": _number(earnings.get("implied_move_exceed_rate")), "medianMove": _number(earnings.get("median_absolute_move_1d")),
                     "straddle1d": _number(earnings.get("median_long_straddle_return_1d")),
                     "straddle1w": _number(earnings.get("median_long_straddle_return_1w"))},
        "news": {"status": _text(news.get("status")), "count": _number(news.get("headline_count")),
                 "major": _number(news.get("major_count")), "historySessions": _number(news.get("history_sessions")),
                 "sentiment": _number(news.get("mean_provider_sentiment")), "sources": _number(news.get("source_count")),
                 "method": _text(news.get("method"))},
        "analogs": {"status": _text(analogs.get("status")), "sample": _number(analogs.get("sample_size")),
                    "disposition": _text(analogs.get("historical_disposition"), "UNAVAILABLE"),
                    "version": _text(analogs.get("model_version")),
                    "lookback": _number(analogs.get("lookback_sessions")),
                    "method": _text(analogs.get("method")),
                    "horizons": {key: {"sample": _number(_mapping(horizons.get(key)).get("sample_size")),
                                                "upRate": _number(_mapping(horizons.get(key)).get("up_rate")),
                                                "p10": _number(_mapping(horizons.get(key)).get("p10_return")),
                                                "median": _number(_mapping(horizons.get(key)).get("median_return")),
                                                "p90": _number(_mapping(horizons.get(key)).get("p90_return"))} for key in ("1", "5", "10", "20")},
                    "path": [{"session": _number(_mapping(item).get("session")),
                              "sample": _number(_mapping(item).get("sample_size")),
                              "upRate": _number(_mapping(item).get("up_rate")),
                              "p10": _number(_mapping(item).get("p10_return")),
                              "median": _number(_mapping(item).get("median_return")),
                              "p90": _number(_mapping(item).get("p90_return"))}
                             for item in analog_path[:20] if isinstance(item, Mapping)],
                    "excursion": {
                        "sample": _number(excursion.get("sample_size")),
                        "medianMfe": _number(excursion.get("median_max_favorable_excursion")),
                        "medianMae": _number(excursion.get("median_max_adverse_excursion")),
                        "asymmetry": _number(excursion.get("payoff_asymmetry")),
                        "hitUp5": _number(excursion.get("hit_up_5_rate")),
                        "hitDown5": _number(excursion.get("hit_down_5_rate")),
                        "hitUp10": _number(excursion.get("hit_up_10_rate")),
                        "hitDown10": _number(excursion.get("hit_down_10_rate")),
                        "upFirst5": _number(excursion.get("up_first_5_rate")),
                        "downFirst5": _number(excursion.get("down_first_5_rate")),
                        "neither5": _number(excursion.get("neither_5_rate")),
                        "peakSession": _number(excursion.get("median_peak_session")),
                        "troughSession": _number(excursion.get("median_trough_session")),
                    },
                    "baseline": {
                        "status": _text(baseline.get("status")), "sample": _number(baseline.get("sample_size")),
                        "upRate": _number(baseline.get("up_rate")), "p10": _number(baseline.get("p10_return")),
                        "median": _number(baseline.get("median_return")), "p90": _number(baseline.get("p90_return")),
                        "upLift": _number(baseline.get("analog_up_rate_lift")),
                        "medianLift": _number(baseline.get("analog_median_return_lift")),
                    },
                    "stability": {
                        "status": _text(stability.get("status"), "UNTESTED"),
                        "dispositionStable": stability.get("disposition_stable") is True,
                        "medianMin": _number(stability.get("leave_one_out_median_min")),
                        "medianMax": _number(stability.get("leave_one_out_median_max")),
                        "upMin": _number(stability.get("leave_one_out_up_rate_min")),
                        "upMax": _number(stability.get("leave_one_out_up_rate_max")),
                        "effectiveSample": _number(stability.get("effective_sample_size")),
                        "maxWeight": _number(stability.get("maximum_distance_weight_share")),
                    },
                    "match": {"medianDistance": _number(match.get("median_distance")),
                              "bestDistance": _number(match.get("best_distance")),
                              "worstDistance": _number(match.get("worst_distance")),
                              "candidateCount": _number(match.get("candidate_count")),
                              "features": _items(match.get("features")),
                              "allFeatures": _items(match.get("all_current_features")),
                              "derivativeStatus": _text(derivative_match.get("status"), "UNAVAILABLE"),
                              "derivativeCandidates": _number(derivative_match.get("candidate_count")),
                              "derivativeIndependent": _number(derivative_match.get("independent_candidate_count")),
                              "derivativeRequired": _number(derivative_match.get("required_independent_candidates")),
                              "derivativeReason": _text(derivative_match.get("reason")),
                              "untested": _items(match.get("current_context_not_tested_in_match"))},
                    "rows": [{"date": _text(_mapping(item).get("date")), "distance": _number(_mapping(item).get("distance")),
                              "outcomes": _mapping(_mapping(item).get("outcomes")),
                              "mfe": _number(_mapping(item).get("max_favorable_excursion_20d")),
                              "mae": _number(_mapping(item).get("max_adverse_excursion_20d")),
                              "peakSession": _number(_mapping(item).get("peak_session")),
                              "troughSession": _number(_mapping(item).get("trough_session")),
                              "firstMove": _text(_mapping(item).get("first_5pct_move"))}
                             for item in analog_rows[:15]]},
        "forecast": {
            "status": _text(forecast.get("status")), "version": _text(forecast.get("model_version")),
            "reason": _text(forecast.get("reason"), "Model path is unavailable for this ticker."),
            "direction": _text(forecast.get("direction")), "sample": _number(forecast.get("sample_size")),
            "center20": _number(forecast.get("center_return_20d")),
            "low20": _number(forecast.get("low_return_20d")), "high20": _number(forecast.get("high_return_20d")),
            "directionalFrequency": _number(forecast.get("directional_analog_frequency")),
            "calibrated": forecast.get("calibrated") is True, "method": _text(forecast.get("method")),
            "path": [{"date": _text(_mapping(item).get("date")), "session": _number(_mapping(item).get("session")),
                      "center": _number(_mapping(item).get("center_price")), "low": _number(_mapping(item).get("low_price")),
                      "high": _number(_mapping(item).get("high_price"))}
                     for item in forecast_path[:20] if isinstance(item, Mapping)],
        },
        "forecastV4": {
            "status": _text(forecast_v4.get("status")), "version": _text(forecast_v4.get("model_version")),
            "reason": _text(forecast_v4.get("reason"), "V4 volatility scaling is unavailable for this ticker."),
            "direction": _text(forecast_v4.get("direction")), "sample": _number(forecast_v4.get("sample_size")),
            "center5": _number(forecast_v4.get("center_return_5d")),
            "p10_5": _number(forecast_v4.get("p10_return_5d")), "p25_5": _number(forecast_v4.get("p25_return_5d")),
            "p50_5": _number(forecast_v4.get("p50_return_5d")), "p75_5": _number(forecast_v4.get("p75_return_5d")),
            "p90_5": _number(forecast_v4.get("p90_return_5d")),
            "center20": _number(forecast_v4.get("center_return_20d")),
            "p10": _number(forecast_v4.get("p10_return_20d")), "p25": _number(forecast_v4.get("p25_return_20d")),
            "p50": _number(forecast_v4.get("p50_return_20d")), "p75": _number(forecast_v4.get("p75_return_20d")),
            "p90": _number(forecast_v4.get("p90_return_20d")),
            "calibrated": forecast_v4.get("calibrated") is True,
            "promotionEligible": forecast_v4.get("promotion_eligible") is True,
            "method": _text(forecast_v4.get("method")),
            "scaling": {
                "targetVol": _number(forecast_v4_scaling.get("target_annualized_volatility")),
                "realizedVol": _number(forecast_v4_scaling.get("realized_volatility_20")),
                "frontIv": _number(forecast_v4_scaling.get("front_implied_volatility")),
                "minScale": _number(forecast_v4_scaling.get("minimum_scale")),
                "medianScale": _number(forecast_v4_scaling.get("median_scale")),
                "maxScale": _number(forecast_v4_scaling.get("maximum_scale")),
                "clippedCount": _number(forecast_v4_scaling.get("clipped_analog_count")),
            },
            "path": [{"date": _text(_mapping(item).get("date")), "session": _number(_mapping(item).get("session")),
                      "center": _number(_mapping(item).get("center_price")),
                      "p10": _number(_mapping(item).get("p10_price")), "p25": _number(_mapping(item).get("p25_price")),
                      "p50": _number(_mapping(item).get("p50_price")), "p75": _number(_mapping(item).get("p75_price")),
                      "p90": _number(_mapping(item).get("p90_price"))}
                     for item in forecast_v4_path[:20] if isinstance(item, Mapping)],
        },
        "challengers": {
            "status": _text(challenger_suite.get("status"), "UNAVAILABLE"),
            "version": _text(challenger_suite.get("version")),
            "agreement": _number(challenger_suite.get("directional_agreement")),
            "directionalCount": _number(challenger_suite.get("directional_model_count")),
            "models": [{
                "version": _text(_mapping(item).get("model_version")),
                "horizon": _number(_mapping(item).get("horizon_sessions")),
                "direction": _text(_mapping(item).get("direction"), "NEUTRAL"),
                "sample": _number(_mapping(item).get("sample_size")),
                "rawScore": _number(_mapping(item).get("raw_direction_score")),
                "center": _number(_mapping(item).get("center_return")),
                "low": _number(_mapping(item).get("low_return")),
                "high": _number(_mapping(item).get("high_return")),
                "annualizedVol": _number(_mapping(item).get("annualized_volatility")),
                "method": _text(_mapping(item).get("method")),
            } for item in challenger_models if isinstance(item, Mapping)],
        },
        "whyToday": [{"factor": _text(_mapping(item).get("factor")), "change": _number(_mapping(item).get("change")),
                      "unit": _text(_mapping(item).get("unit")), "note": _text(_mapping(item).get("note"))} for item in why[:8]],
    }


def _scenario(value: object) -> dict[str, Any]:
    raw = _mapping(value)
    return {
        "name": _text(raw.get("name"), "UNSPECIFIED").upper(),
        "conditions": _items(raw.get("conditions")),
        "outcome": _text(raw.get("outcome")),
        "invalidation": _items(raw.get("invalidation")),
    }


def _entry(value: object) -> dict[str, Any]:
    raw = _mapping(value)
    ticker = _text(raw.get("ticker", raw.get("symbol")), "UNKNOWN").upper()
    technical = _mapping(raw.get("technical", raw.get("ta")))
    analyst = _mapping(raw.get("analyst", raw.get("analysis")))
    enrichment = (
        _mapping(raw.get("agent_enrichment"))
        if raw.get("agent_enrichment_validated") is True
        else {}
    )
    quality = _mapping(raw.get("data_quality", raw.get("quality")))
    provenance = _mapping(raw.get("provenance"))
    evidence = _mapping(raw.get("evidence"))
    chain = _mapping(evidence.get("chain"))
    gex = _mapping(evidence.get("gex"))
    flow = _mapping(evidence.get("flow"))
    oi = _mapping(evidence.get("open_interest"))
    dark_pool = _mapping(evidence.get("dark_pool"))
    news = _mapping(evidence.get("news"))
    earnings = _mapping(evidence.get("earnings"))
    action, gate_reasons = _gate(raw)
    gates = _mapping(raw.get("gates", raw.get("data_gate")))
    options = _mapping(raw.get("options", raw.get("option_candidates")))
    option_research = _mapping(raw.get("option_research"))
    option_research_rows = option_research.get("rows")
    if not isinstance(option_research_rows, Sequence) or isinstance(option_research_rows, (str, bytes)):
        option_research_rows = []
    option_rows = options.get("candidates", options if isinstance(options, Sequence) else [])
    price_source = raw.get("price", raw.get("last_close"))
    price = _number(_mapping(price_source).get("value")) if isinstance(price_source, Mapping) else _number(price_source)
    provider_source = provenance.get("provider")
    if isinstance(provider_source, Sequence) and not isinstance(provider_source, (str, bytes)):
        provider = ", ".join(_text(item) for item in provider_source)
    else:
        provider = _text(provider_source)
    call_oi = _number(chain.get("latest_call_open_interest"))
    put_oi = _number(chain.get("latest_put_open_interest"))
    put_call_oi = put_oi / call_oi if call_oi and put_oi is not None else None
    raw_headlines = news.get("latest_headlines", [])
    headlines: list[dict[str, str]] = []
    if isinstance(raw_headlines, Sequence) and not isinstance(raw_headlines, (str, bytes)):
        for item in raw_headlines[:5]:
            item = _mapping(item)
            headlines.append({
                "headline": _text(item.get("headline")),
                "source": _text(item.get("source")),
                "published": _text(item.get("published_at")),
            })
    positions = raw.get("positions", raw.get("position"))
    if isinstance(positions, Mapping):
        positions = [positions]
    evidence_points = enrichment.get("evidence_points")
    grounded_claims: list[str] = []
    if isinstance(evidence_points, Sequence) and not isinstance(evidence_points, (str, bytes)):
        grounded_claims = [
            _text(_mapping(item).get("statement"))
            for item in evidence_points[:MAX_ITEMS]
            if _text(_mapping(item).get("statement")) != "—"
        ]
    scenarios = enrichment.get("scenarios")
    if isinstance(scenarios, Mapping):
        scenarios = [scenarios.get(name) for name in ("BULL", "BASE", "BEAR")]
    thesis = _mapping(raw.get("trade_thesis")); option_reference = _mapping(thesis.get("option_reference"))
    thesis_oi = _mapping(thesis.get("unusual_oi")); thesis_premium = _mapping(thesis.get("premium_expansion"))
    intraday = _mapping(raw.get("intraday_condition"))
    freshness = _mapping(raw.get("freshness_contract"))
    freshness_datasets = _mapping(freshness.get("datasets"))
    thesis_direction = _text(thesis.get("direction"), "UNASSESSED").upper()
    thesis_score = _number(thesis.get("conviction_score"))
    trigger = _text(thesis.get("trigger_reference"))
    invalidation = _text(thesis.get("invalidation_reference"))
    deterministic_summary = (
        f"{thesis_direction} conditional thesis"
        + (f" · evidence {thesis_score:.0f}/100" if thesis_score is not None else "")
        + ". The daily forecast remains frozen; intraday evidence can confirm, weaken, or invalidate it."
    )
    deterministic_outlook = f"Confirm: {trigger} Invalidate: {invalidation}"
    return {
        "ticker": ticker,
        "rank": _number(raw.get("trade_rank", raw.get("research_rank"))),
        "action": action,
        "gateReasons": gate_reasons,
        "price": price,
        "change": _number(raw.get("return_1d_pct", technical.get("return_1d_pct"))),
        "bars": _bars(technical.get("bars", raw.get("bars"))),
        "technical": {
            "rsi14": _number(technical.get("rsi14")),
            "rv20": _number(technical.get("rv20_ann_pct", technical.get("rv20"))),
            "ema20": _number(technical.get("ema20")),
            "ema50": _number(technical.get("ema50")),
            "ema200": _number(technical.get("ema_200", technical.get("ema200"))),
            "atr14": _number(technical.get("atr_14_pct", technical.get("atr14"))),
            "return5": _number(technical.get("return_5d")),
            "return20": _number(technical.get("return_20d")),
            "return63": _number(technical.get("return_63d")),
            "volumeRatio": _number(technical.get("volume_ratio_20")),
            "observations": _number(technical.get("observations")),
            "drawdown": _number(technical.get("drawdown_126d_pct", technical.get("drawdown_pct"))),
            "coverage": _text(technical.get("coverage_status", raw.get("coverage_status", technical.get("history_status")))),
        },
        "analysis": {
            "validated": bool(enrichment),
            "mode": "VALIDATED_AGENT" if enrichment else "DETERMINISTIC_INTRADAY",
            "posture": _text(enrichment.get("posture"), thesis_direction),
            "priority": _number(enrichment.get("research_priority", thesis_score)),
            "confidence": _number(enrichment.get("evidence_confidence", thesis_score)),
            "dayOutlook": _text(enrichment.get("day_outlook"), deterministic_outlook),
            "summary": _text(enrichment.get("summary"), deterministic_summary),
            "optionContext": _plain_option_context(enrichment.get("option_context", option_reference.get("reason"))),
            "scenarios": [
                _scenario(item) for item in scenarios[:3] if isinstance(item, Mapping)
            ] if isinstance(scenarios, Sequence) and not isinstance(scenarios, (str, bytes)) else [],
        },
        "claims": grounded_claims or _items(analyst.get("claims", raw.get("claims"))),
        "counter": _items(enrichment.get("counterevidence", analyst.get("counterevidence", analyst.get("counter_evidence")))),
        "unknowns": _items(enrichment.get("unknowns", analyst.get("unknowns", raw.get("unknowns")))),
        "quality": {
            "complete": quality.get("complete") is True,
            "freshness": _text(quality.get("freshness", quality.get("as_of"))),
            "chain": _text(quality.get("chain_status", quality.get("chain"))),
            "gex": _text(quality.get("gex_status", quality.get("gex"))),
            "note": _text(quality.get("note", quality.get("reason"))),
        },
        "freshness": {
            "overall": _text(freshness.get("overall"), "UNAVAILABLE"),
            "completeSession": _text(freshness.get("latest_complete_session")),
            "cutoffEt": _text(freshness.get("cutoff_et")),
            "datasets": {
                str(name): {
                    "status": _text(_mapping(item).get("status"), "UNAVAILABLE"),
                    "session": _text(_mapping(item).get("observed_session")),
                    "lag": _number(_mapping(item).get("session_lag")),
                }
                for name, item in freshness_datasets.items()
            },
        },
        "provenance": {
            "asOf": _text(provenance.get("as_of", raw.get("as_of"))),
            "snapshots": _items(provenance.get("snapshot_ids", provenance.get("sources"))),
            "provider": provider,
        },
        "options": [_candidate(_mapping(item), gates) for item in option_rows[:MAX_ITEMS]] if isinstance(option_rows, Sequence) and not isinstance(option_rows, (str, bytes)) else [],
        "optionResearch": {
            "status": _text(option_research.get("status"), "UNAVAILABLE"),
            "version": _text(option_research.get("version")),
            "selected": _text(option_research.get("selected_reference")),
            "rows": [{
                "contract": _text(_mapping(item).get("contract")),
                "fit": _number(_mapping(item).get("research_fit_score")),
                "status": _text(_mapping(item).get("status"), "NOT_ELIGIBLE"),
                "scenarios": _mapping(_mapping(item).get("scenarios")),
            } for item in option_research_rows[:MAX_ITEMS] if isinstance(item, Mapping)],
        },
        "positions": [_mapping(item) for item in positions[:MAX_ITEMS]] if isinstance(positions, Sequence) and not isinstance(positions, (str, bytes)) else [],
        "signals": {
            "chainDate": _text(chain.get("latest_market_date")),
            "validContracts": _number(chain.get("latest_valid_contract_count")),
            "medianIv": _number(chain.get("latest_median_implied_volatility")),
            "putCallOi": put_call_oi,
            "gexDate": _text(gex.get("latest_market_date")),
            "callWall": _number(gex.get("call_wall")),
            "gammaFlip": _number(gex.get("gamma_flip")),
            "gammaMagnet": _number(gex.get("gamma_magnet")),
            "putWall": _number(gex.get("put_wall")),
            "flowDate": _text(_mapping(flow.get("source")).get("market_date")),
            "flowAlerts": _number(flow.get("alert_count")),
            "flowPremium": _number(flow.get("aggregate_reported_premium")),
            "oiDate": _text(oi.get("latest_market_date")),
            "oiRows": _number(oi.get("latest_row_count")),
            "oiChange": _number(oi.get("latest_aggregate_explicit_change")),
            "darkDate": _text(dark_pool.get("latest_market_date")),
            "darkPrints": _number(dark_pool.get("latest_unique_print_count")),
            "darkPremium": _number(dark_pool.get("latest_aggregate_reported_premium")),
            "earningsDate": _text(earnings.get("report_date")),
            "earningsTime": _text(earnings.get("report_time")),
            "earningsExpectedMove": _number(earnings.get("expected_move")),
            "lastEarningsDate": _text(earnings.get("last_report_date")),
            "lastEarningsTime": _text(earnings.get("last_report_time")),
        },
        "headlines": headlines,
        "edge": _edge_view(raw),
        "thesis": {
            "status": _text(thesis.get("status")), "direction": _text(thesis.get("direction"), "NEUTRAL"),
            "conviction": _number(thesis.get("conviction_score")),
            "expected20": _number(thesis.get("expected_return_20d")),
            "low20": _number(thesis.get("range_low_20d")), "high20": _number(thesis.get("range_high_20d")),
            "trigger": _text(thesis.get("trigger_reference")), "invalidation": _text(thesis.get("invalidation_reference")),
            "option": {"contract": _text(option_reference.get("contract")), "type": _text(option_reference.get("type")),
                       "expiry": _text(option_reference.get("expiry")), "dte": _number(option_reference.get("dte")),
                       "strike": _number(option_reference.get("strike")), "bid": _number(option_reference.get("bid")),
                       "ask": _number(option_reference.get("ask")), "volume": _number(option_reference.get("volume")),
                       "spread": _number(option_reference.get("spread_pct")), "delta": _number(option_reference.get("delta")),
                       "oi": _number(option_reference.get("open_interest")), "fit": _number(option_reference.get("fit_score")),
                       "breakeven": _number(option_reference.get("breakeven")),
                       "breakevenMove": _number(option_reference.get("breakeven_move_pct")),
                       "status": _text(option_reference.get("status")), "reason": _text(option_reference.get("reason"))},
            "oi": {"status": _text(thesis_oi.get("status")), "strike": _number(thesis_oi.get("directional_build_strike")),
                   "callChange": _number(thesis_oi.get("call_change")), "putChange": _number(thesis_oi.get("put_change"))},
            "premium": {"status": _text(thesis_premium.get("status")),
                        "directional": _number(thesis_premium.get("directional_premium")),
                        "percentile": _number(thesis_premium.get("directional_percentile")),
                        "ivChange": _number(thesis_premium.get("iv_change"))},
        },
        "intraday": {
            "status": _text(intraday.get("status"), "STORED"),
            "observedAt": _text(intraday.get("observed_at")),
            "observedPrice": _number(intraday.get("observed_price")),
            "dailyAnchorPrice": _number(intraday.get("daily_anchor_price")),
            "changeFromAnchor": _number(intraday.get("change_from_daily_anchor")),
            "drivers": _items(intraday.get("drivers")),
            "conflicts": _items(intraday.get("conflicts")),
            "neutral": _items(intraday.get("neutral_context")),
            "directionalVotes": _number(intraday.get("directional_votes")),
            "voteCount": _number(intraday.get("vote_count")),
            "boundary": _text(intraday.get("boundary")),
        },
    }


def _enhanced_view(value: object) -> dict[str, Any]:
    raw = _mapping(value)
    stock_state = _mapping(raw.get("stock_state"))
    activity = _mapping(raw.get("option_price_levels"))
    gex = _mapping(raw.get("greek_exposure"))
    flow = _mapping(raw.get("greek_flow"))
    volatility = _mapping(raw.get("volatility"))
    volatility_diagnostics = _mapping(raw.get("volatility_diagnostics"))
    dark = _mapping(raw.get("dark_pool_levels"))
    short = _mapping(raw.get("short_crowding"))
    sources = _mapping(raw.get("sources"))
    return {
        "available": bool(raw),
        "sourceCount": sum(1 for value in sources.values() if isinstance(value, int) and not isinstance(value, bool)),
        "stockState": {
            "marketTime": _text(stock_state.get("market_time")),
            "tapeTime": _text(stock_state.get("tape_time")),
            "price": _number(stock_state.get("price")),
            "previousClose": _number(stock_state.get("previous_close")),
            "change": _number(stock_state.get("change_pct")),
            "volume": _number(stock_state.get("total_volume", stock_state.get("volume"))),
        },
        "activity": {
            "levels": _number(activity.get("level_count")),
            "balance": _number(activity.get("call_minus_put_share")),
            "dominantPrice": _number(activity.get("dominant_activity_price")),
            "dominantShare": _number(activity.get("dominant_activity_share")),
            "callPeak": _number(activity.get("call_peak_price")),
            "putPeak": _number(activity.get("put_peak_price")),
            "nearBalance": _number(activity.get("near_spot_call_minus_put_share")),
        },
        "gex": {
            "date": _text(gex.get("provider_date")),
            "regime": _text(gex.get("near_spot_regime"), "unavailable").upper(),
            "nearNet": _number(gex.get("near_spot_net_gex_5pct")),
            "concentration": _number(gex.get("gex_concentration")),
            "positiveStrike": _number(gex.get("strongest_positive_gex_strike")),
            "positiveExposure": _number(gex.get("strongest_positive_gex")),
            "negativeStrike": _number(gex.get("strongest_negative_gex_strike")),
            "negativeExposure": _number(gex.get("strongest_negative_gex")),
            "vanna": _number(gex.get("net_vanna_total")),
            "charm": _number(gex.get("net_charm_total")),
        },
        "flow": {
            "timestamp": _text(flow.get("final_timestamp")),
            "delta": _number(flow.get("directional_delta_flow")),
            "vega": _number(flow.get("directional_vega_flow")),
            "otmShare": _number(flow.get("otm_delta_share")),
            "persistence": _number(flow.get("delta_sign_persistence")),
            "lateChange": _number(flow.get("last_quarter_delta_change")),
            "transactions": _number(flow.get("transactions")),
            "volume": _number(flow.get("volume")),
            "rows": _number(flow.get("row_count")),
        },
        "volatility": {
            "date": _text(volatility.get("provider_date")),
            "iv": _number(volatility.get("iv")),
            "rv": _number(volatility.get("realized_volatility")),
            "gap": _number(volatility.get("iv_minus_rv")),
            "rank": _number(volatility.get("iv_rank")),
            "frontIv": _number(volatility.get("front_iv")),
            "frontDte": _number(volatility.get("front_dte")),
            "frontMove": _number(volatility.get("front_implied_move_pct")),
            "iv30": _number(volatility.get("iv_30d")),
            "iv60": _number(volatility.get("iv_60d")),
            "slope": _number(volatility.get("term_slope_30d_to_60d")),
            "move30": _number(volatility.get("fixed_30d_implied_move_pct")),
            "percentile30": _number(volatility.get("fixed_30d_iv_percentile")),
        },
        "volatilityDiagnostics": {
            "anomalyDate": _text(volatility_diagnostics.get("anomaly_date")),
            "anomalyDirection": _text(volatility_diagnostics.get("anomaly_direction")),
            "anomalyScore": _number(volatility_diagnostics.get("anomaly_score")),
            "characterDate": _text(volatility_diagnostics.get("character_date")),
            "character": _text(volatility_diagnostics.get("character")),
            "halfLife": _number(volatility_diagnostics.get("half_life_days")),
            "hurst": _number(volatility_diagnostics.get("hurst_rv")),
            "premiumDate": _text(volatility_diagnostics.get("variance_risk_premium_date")),
            "premium": _number(volatility_diagnostics.get("variance_risk_premium")),
            "premiumRank": _number(volatility_diagnostics.get("variance_risk_premium_rank")),
        },
        "dark": {
            "level": _number(dark.get("dominant_price")),
            "distance": _number(dark.get("dominant_distance_pct")),
            "share": _number(dark.get("dark_share_at_reported_levels")),
            "darkVolume": _number(dark.get("dark_volume_total")),
            "regularVolume": _number(dark.get("regular_volume_total")),
            "levels": _number(dark.get("level_count")),
        },
        "short": {
            "date": _text(short.get("short_interest_date")),
            "siFloat": _number(short.get("short_interest_float")),
            "daysCover": _number(short.get("days_to_cover")),
            "borrowTimestamp": _text(short.get("latest_borrow_timestamp")),
            "borrowFee": _number(short.get("borrow_fee_rate")),
            "sharesAvailable": _number(short.get("short_shares_available")),
            "sharesChange": _number(short.get("shares_available_change_vs_approx_1d")),
            "shortVolumeDate": _text(short.get("short_volume_date")),
            "shortVolumeRatio": _number(short.get("short_volume_ratio")),
            "shortVolumeMean20": _number(short.get("short_volume_ratio_20d_mean")),
        },
    }


def _tide_view(value: object) -> dict[str, Any]:
    raw = _mapping(value)
    return {
        "available": bool(raw),
        "date": _text(raw.get("provider_date")),
        "timestamp": _text(raw.get("final_timestamp")),
        "calls": _number(raw.get("net_call_premium")),
        "puts": _number(raw.get("net_put_premium")),
        "callMinusPut": _number(raw.get("call_minus_put_premium")),
        "volume": _number(raw.get("net_volume")),
    }


def _evaluation_view(value: object) -> dict[str, Any]:
    raw = _mapping(value)
    raw_horizons = _mapping(raw.get("horizons"))
    horizons: dict[str, Any] = {}
    for horizon in ("1", "5", "10", "20"):
        item = _mapping(raw_horizons.get(horizon))
        interval = _mapping(item.get("prospective_direction_accuracy_wilson_95"))
        classification = _mapping(item.get("prospective_classification"))
        origin = _mapping(item.get("origin_dependence"))
        independent = _mapping(item.get("independent_origin_metrics"))
        horizons[horizon] = {
            "registered": _number(item.get("registered")),
            "evaluated": _number(item.get("evaluated")),
            "pending": _number(item.get("pending")),
            "directionCount": _number(item.get("direction_evaluable")),
            "accuracy": _number(item.get("direction_accuracy")),
            "baseline": _number(item.get("majority_direction_baseline")),
            "lift": _number(item.get("accuracy_lift_vs_majority")),
            "medianReturn": _number(item.get("median_underlying_return_pct")),
            "medianError": _number(item.get("median_absolute_center_error_pct_points")),
            "rangeCoverage": _number(item.get("range_coverage")),
            "optionCount": _number(item.get("option_evaluated")),
            "medianOptionReturn": _number(item.get("median_option_return_pct")),
            "prospectiveEvaluated": _number(item.get("prospective_evaluated")),
            "artifactEvaluated": _number(item.get("artifact_seed_evaluated")),
            "prospectiveDirectionCount": _number(item.get("prospective_direction_evaluable")),
            "prospectiveAccuracy": _number(item.get("prospective_direction_accuracy")),
            "prospectiveAccuracyLow": _number(interval.get("low")),
            "prospectiveAccuracyHigh": _number(interval.get("high")),
            "balancedAccuracy": _number(classification.get("balanced_accuracy")),
            "matthewsCorrelation": _number(classification.get("matthews_correlation")),
            "prospectiveBaseline": _number(item.get("prospective_majority_direction_baseline")),
            "prospectiveLift": _number(item.get("prospective_accuracy_lift_vs_majority")),
            "prospectiveOptionCount": _number(item.get("prospective_option_evaluated")),
            "prospectiveMedianOptionReturn": _number(item.get("prospective_median_option_return_pct")),
            "originSessions": _number(item.get("distinct_origin_sessions")),
            "originAccuracyMedian": _number(origin.get("origin_accuracy_median")),
            "originAccuracyP10": _number(origin.get("origin_accuracy_p10")),
            "originAccuracyP90": _number(origin.get("origin_accuracy_p90")),
            "independentOrigins": _number(independent.get("origin_sessions")),
            "independentAccuracy": _number(independent.get("equal_weight_accuracy")),
            "independentBaseline": _number(independent.get("equal_weight_majority_baseline")),
            "independentLift": _number(independent.get("equal_weight_lift")),
            "independentP10": _number(independent.get("origin_accuracy_p10")),
            "independentMedian": _number(independent.get("origin_accuracy_median")),
            "independentP90": _number(independent.get("origin_accuracy_p90")),
            "medianIntervalScore": _number(item.get("prospective_median_interval_score")),
            "signedCenterError": _number(item.get("prospective_median_signed_center_error_pct_points")),
            "baselines": _mapping(item.get("baselines")),
            "sampleGate": item.get("sample_gate_passed") is True,
        }
    recent_rows = []
    prospective_rows: list[dict[str, Any]] = []
    raw_rows = raw.get("rows")
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
        raw_rows = ()
    for row in raw_rows:
        item = _mapping(row)
        if item.get("daily_tracking_eligible") is False:
            continue
        row_view = {
            "ticker": _text(item.get("ticker")),
            "origin": _text(item.get("origin_session"))[:10],
            "target": _text(item.get("target_session"))[:10],
            "horizon": _number(item.get("horizon_sessions")),
            "status": _text(item.get("status")),
            "direction": _text(item.get("direction")),
            "modelVersion": _text(item.get("model_version")),
            "modelRole": _text(item.get("model_role"), "ACTIVE_THESIS_V3"),
            "registrationMode": _text(item.get("registration_mode"), "UNKNOWN"),
            "return": _number(item.get("underlying_return_pct")),
            "correct": item.get("direction_correct") if isinstance(item.get("direction_correct"), bool) else None,
            "error": _number(item.get("absolute_center_error_pct_points")),
            "covered": item.get("range_covered") if isinstance(item.get("range_covered"), bool) else None,
            "optionReturn": _number(item.get("option_return_pct")),
            "optionAvailable": item.get("option_available") is True,
        }
        if row_view["registrationMode"] == "PROSPECTIVE" and row_view["modelRole"] == "ACTIVE_THESIS_V3":
            prospective_rows.append(row_view)
        if row_view["status"] == "EVALUATED" and row_view["modelRole"] == "ACTIVE_THESIS_V3":
            recent_rows.append(row_view)

    heatmap = [
        row for row in prospective_rows
        if row["status"] == "EVALUATED" and row["horizon"] == 1
    ][-70:]
    timelines: dict[str, list[dict[str, Any]]] = {}
    tickers = sorted({str(row["ticker"]) for row in prospective_rows})
    for ticker in tickers:
        ticker_rows = [row for row in prospective_rows if row["ticker"] == ticker]
        resolved_origins = sorted({
            str(row["origin"]) for row in ticker_rows if row["status"] == "EVALUATED"
        })
        if not resolved_origins:
            continue
        latest_origin = resolved_origins[-1]
        timelines[ticker] = sorted(
            [row for row in ticker_rows if row["origin"] == latest_origin],
            key=lambda row: float(row["horizon"] or 0),
        )[:4]

    def performance_slices(value: object) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for label, raw_item in _mapping(value).items():
            item = _mapping(raw_item)
            result[str(label)] = {
                "registered": _number(item.get("registered")),
                "evaluated": _number(item.get("evaluated")),
                "pending": _number(item.get("pending")),
                "directionCount": _number(item.get("direction_evaluable")),
                "accuracy": _number(item.get("direction_accuracy")),
                "baseline": _number(item.get("majority_direction_baseline")),
                "lift": _number(item.get("accuracy_lift_vs_majority")),
                "medianReturn": _number(item.get("median_underlying_return_pct")),
                "medianError": _number(item.get("median_absolute_center_error_pct_points")),
                "rangeCoverage": _number(item.get("range_coverage")),
                "optionCount": _number(item.get("option_evaluated")),
                "medianOptionReturn": _number(item.get("median_option_return_pct")),
                "prospectiveEvaluated": _number(item.get("prospective_evaluated")),
                "prospectiveDirectionCount": _number(item.get("prospective_direction_evaluable")),
                "prospectiveAccuracy": _number(item.get("prospective_direction_accuracy")),
                "prospectiveBaseline": _number(item.get("prospective_majority_direction_baseline")),
                "prospectiveLift": _number(item.get("prospective_accuracy_lift_vs_majority")),
                "prospectiveOptionCount": _number(item.get("prospective_option_evaluated")),
                "prospectiveMedianOptionReturn": _number(item.get("prospective_median_option_return_pct")),
            }
        return result
    return {
        "available": bool(raw),
        "status": _text(raw.get("status"), "NOT_INITIALIZED"),
        "calibrated": raw.get("calibrated") is True,
        "registered": _number(raw.get("registered_forecasts")),
        "evaluated": _number(raw.get("evaluated_forecasts")),
        "prospectiveEvaluated": _number(raw.get("prospective_evaluated_forecasts")),
        "artifactEvaluated": _number(raw.get("artifact_seed_evaluated_forecasts")),
        "pending": _number(raw.get("pending_forecasts")),
        "horizons": horizons,
        "directions": performance_slices(raw.get("direction_breakdown")),
        "tickers": performance_slices(raw.get("ticker_breakdown")),
        "convictionBands": performance_slices(raw.get("conviction_breakdown")),
        "models": performance_slices(raw.get("model_breakdown")),
        "regimes": {
            "trend": performance_slices(_mapping(raw.get("regime_breakdown")).get("trend")),
            "volatility": performance_slices(_mapping(raw.get("regime_breakdown")).get("volatility")),
        },
        "minimumGates": _mapping(raw.get("minimum_gates")),
        "dependenceNote": _text(raw.get("dependence_note")),
        "probabilityScoring": _mapping(raw.get("probability_scoring")),
        "optionMethod": _text(raw.get("paper_option_method")),
        "limitations": _items(raw.get("limitations")),
        "recentRows": recent_rows[-40:],
        "heatmap": heatmap,
        "timelines": timelines,
    }


def _previous_entries(value: object) -> dict[str, dict[str, Any]]:
    raw = _mapping(value)
    watchlist = raw.get("watchlist", raw.get("symbols", raw.get("tickers", [])))
    if not isinstance(watchlist, Sequence) or isinstance(watchlist, (str, bytes)):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in watchlist[:100]:
        entry = _entry(item)
        result[entry["ticker"]] = {
            "asOf": _text(raw.get("cutoff_at", raw.get("as_of", raw.get("asOf")))),
            "rank": entry["rank"], "price": entry["price"],
            "direction": entry["thesis"]["direction"],
            "conviction": entry["thesis"]["conviction"],
            "dimensions": entry["edge"]["dimensions"],
        }
    return result


def normalize_run(run: Mapping[str, Any]) -> dict[str, Any]:
    """Create bounded view data and always retain the run's research-only posture."""
    watchlist = run.get("watchlist", run.get("symbols", run.get("tickers", [])))
    if not isinstance(watchlist, Sequence) or isinstance(watchlist, (str, bytes)):
        watchlist = []
    enhanced = _mapping(run.get("enhanced_summary"))
    enhanced_symbols = _mapping(enhanced.get("symbols"))
    entries = [_entry(item) for item in watchlist[:100]]
    previous = _previous_entries(run.get("previous_run"))
    for entry in entries:
        entry["whale"] = _enhanced_view(enhanced_symbols.get(entry["ticker"]))
        entry["previous"] = previous.get(entry["ticker"])
    entries.sort(key=lambda item: (
        item["rank"] is None,
        item["rank"] if item["rank"] is not None else 10_000,
        item["ticker"],
    ))
    contexts = _mapping(enhanced.get("contexts"))
    refresh = _mapping(run.get("intraday_status"))
    return {
        "dataVersion": _text(run.get("run_id"), _text(run.get("generated_at"))),
        "generatedAt": _text(run.get("generated_at", run.get("generatedAt"))),
        "asOf": _text(run.get("cutoff_at", run.get("as_of", run.get("asOf")))),
        "mode": _text(run.get("mode"), "HISTORICAL / SHADOW"),
        "enhancedAt": _text(enhanced.get("generated_at")),
        "refresh": {
            "mode": _text(refresh.get("mode"), "DAILY_PUBLICATION"),
            "marketStatus": _text(refresh.get("market_status"), "STORED"),
            "lastCycleAt": _text(refresh.get("last_cycle_at")),
            "completedTiers": _items(refresh.get("completed_tiers")),
            "logicalRequests": _number(refresh.get("logical_requests")),
            "transportAttempts": _number(refresh.get("transport_attempts")),
            "remainingBeforeReserve": _number(refresh.get("remaining_before_reserve")),
            "pollSeconds": _number(refresh.get("browser_poll_seconds")) or 30,
            "analysisMode": _text(refresh.get("analysis_mode"), "FROZEN_DAILY_MODEL"),
            "limitations": _items(refresh.get("limitations")),
        },
        "evaluation": _evaluation_view(run.get("model_evaluation")),
        "contexts": {
            "market": _tide_view(contexts.get("market_tide")),
            "technology": _tide_view(contexts.get("sector_tide_technology")),
            "communication": _tide_view(contexts.get("sector_tide_communication_services")),
            "qqq": _tide_view(contexts.get("etf_tide_qqq")),
            "smh": _tide_view(contexts.get("etf_tide_smh")),
            "soxx": _tide_view(contexts.get("etf_tide_soxx")),
            "calendar": _mapping(contexts.get("economic_calendar")),
        },
        "entries": entries,
    }


def _inline_script_fragment(fragment: str) -> str:
    """Keep JavaScript inline for mobile renderers while escaping end tags."""
    opening = "<script>"
    closing = "</script>"
    start = fragment.find(opening)
    end = fragment.rfind(closing)
    if start < 0 or end <= start:
        raise ValueError("dashboard fragment must contain one inline script")
    script_start = start + len(opening)
    source = fragment[script_start:end]
    source = re.sub(r"</script", r"<\\/script", source, flags=re.IGNORECASE)
    source = source.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    script = f"<script>\n{source}\n</script>"
    return fragment[:start] + script + fragment[end + len(closing):]


def _build_legacy_fragment(run: Mapping[str, Any]) -> str:
    """Return a single HTML fragment. The caller owns writing it to disk."""
    data = json.dumps(normalize_run(run), separators=(",", ":"), ensure_ascii=False)
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template = '''<meta charset="utf-8">
<section id="codex-screener" aria-label="Codex Screener evidence dashboard">
<style>
#codex-screener{{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#edf4fb;background:#07111d;border:1px solid #22364a;border-radius:16px;overflow:hidden;font-variant-numeric:tabular-nums}}#codex-screener *{{box-sizing:border-box}}#codex-screener .top{{padding:18px 20px;border-bottom:1px solid #22364a;background:#0a1623}}#codex-screener h1{{margin:0;font-size:20px}}#codex-screener .sub,#codex-screener .muted{{color:#9fb0c2;font-size:12px;line-height:1.5}}#codex-screener .pill{{font-size:10px;letter-spacing:.08em;color:#f5ce88;border:1px solid #614d2d;padding:5px 7px;border-radius:5px;white-space:nowrap}}#codex-screener .headline{{display:flex;align-items:start;justify-content:space-between;gap:12px}}#codex-screener .layout{{display:grid;grid-template-columns:300px 1fr;min-height:650px}}#codex-screener .watch{{border-right:1px solid #22364a;max-height:900px;overflow:auto}}#codex-screener .watch button{{appearance:none;border:0;border-bottom:1px solid #172a3d;background:transparent;color:#dfeaf3;width:100%;padding:12px 15px;text-align:left;cursor:pointer;transition:background-color 140ms ease,box-shadow 140ms ease}}#codex-screener .watch button:hover,#codex-screener .watch button.active{{background:#102237;box-shadow:inset 3px 0 #70b8ff}}#codex-screener .row{{display:flex;align-items:center;justify-content:space-between;gap:10px}}#codex-screener .ticker{{font-weight:750;letter-spacing:.03em}}#codex-screener .rank{{display:inline-block;min-width:28px;color:#70b8ff;font-size:10px;font-weight:750;letter-spacing:.05em}}#codex-screener .action{{font-size:10px;letter-spacing:.08em;color:#f5ce88}}#codex-screener .posture{{font-size:9px;letter-spacing:.06em;color:#9fb0c2}}#codex-screener .price{{font-size:17px;font-weight:680;margin-top:4px}}#codex-screener .detail{{padding:17px;min-width:0}}#codex-screener .banner{{border:1px solid #614d2d;background:#1b1810;color:#f3ce89;padding:9px 11px;border-radius:7px;font-size:12px;line-height:1.45;margin-bottom:13px}}#codex-screener .grid{{display:grid;grid-template-columns:1.2fr .8fr;gap:12px}}#codex-screener .card{{border:1px solid #22364a;background:#0b1826;border-radius:10px;padding:13px;min-width:0;animation:me-enter 150ms ease-out}}#codex-screener .full{{grid-column:1/-1}}#codex-screener h2{{font-size:12px;margin:0 0 10px;letter-spacing:.01em}}#codex-screener .analysis-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:10px}}#codex-screener .tags{{display:flex;flex-wrap:wrap;gap:6px}}#codex-screener .tag{{border:1px solid #2b4760;background:#102237;border-radius:999px;padding:4px 7px;font-size:9px;letter-spacing:.06em;color:#bcd4e8}}#codex-screener .lead{{font-size:13px;line-height:1.5;color:#edf4fb;margin:0 0 8px}}#codex-screener .summary{{font-size:11.5px;line-height:1.55;color:#cbd9e5;margin:0}}#codex-screener .scoregrid{{display:grid;grid-template-columns:repeat(2,minmax(150px,1fr));gap:8px;margin:10px 0}}#codex-screener .scorebox{{background:#102031;border-radius:7px;padding:8px}}#codex-screener .scorebox .row{{font-size:10px;color:#aebdca;margin-bottom:6px}}#codex-screener .scorebar{{height:4px;border-radius:4px;background:#22364a;overflow:hidden}}#codex-screener .scorebar i{{display:block;height:100%;background:#70b8ff;border-radius:4px}}#codex-screener .metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:7px}}#codex-screener .metric{{background:#102031;padding:8px;border-radius:6px}}#codex-screener .metric label{{display:block;color:#9fb0c2;font-size:9px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px}}#codex-screener .metric b{{font-size:13px}}#codex-screener .chart{{height:240px;border:1px solid #1c3348;border-radius:7px;background:#08131f;position:relative;margin-top:8px}}#codex-screener svg{{width:100%;height:100%;display:block}}#codex-screener .line{{fill:none;stroke:#70b8ff;stroke-width:2}}#codex-screener .ema20{{fill:none;stroke:#56d4bd;stroke-width:1.5}}#codex-screener .ema50{{fill:none;stroke:#b6a1ff;stroke-width:1.5}}#codex-screener ul{{margin:0;padding-left:17px}}#codex-screener li{{font-size:11px;line-height:1.45;margin:5px 0;color:#d7e3ee}}#codex-screener .counter li{{color:#f0c28b}}#codex-screener .unknown li{{color:#aebdca}}#codex-screener table{{border-collapse:collapse;width:100%;font-size:10.5px}}#codex-screener th,#codex-screener td{{text-align:left;padding:7px 5px;border-top:1px solid #1d3042;vertical-align:top}}#codex-screener th{{color:#9fb0c2;font-size:9px;text-transform:uppercase;letter-spacing:.07em}}#codex-screener .scenario-name{{font-weight:750;letter-spacing:.08em}}#codex-screener .scenario-bull{{color:#67d69e}}#codex-screener .scenario-base{{color:#70b8ff}}#codex-screener .scenario-bear{{color:#f2c478}}#codex-screener .context{{margin-top:10px;border-left:2px solid #70b8ff;padding:7px 9px;background:#0d1d2d;color:#cbd9e5;font-size:11px;line-height:1.5}}#codex-screener .status-good{{color:#67d69e}}#codex-screener .status-blocked{{color:#f2c478}}#codex-screener .provenance{{margin-top:12px;font-size:10px;color:#92a5b8;overflow-wrap:anywhere}}@keyframes me-enter{{from{{opacity:.65;transform:translateY(2px)}}to{{opacity:1;transform:none}}}}@media(prefers-reduced-motion:reduce){{#codex-screener .card,#codex-screener .watch button{{animation:none;transition:none}}}}@media(max-width:800px){{#codex-screener .layout{{grid-template-columns:1fr}}#codex-screener .watch{{border-right:0;border-bottom:1px solid #22364a;max-height:260px}}#codex-screener .grid{{grid-template-columns:1fr}}#codex-screener .full{{grid-column:auto}}#codex-screener .metrics{{grid-template-columns:repeat(2,1fr)}}#codex-screener .scoregrid{{grid-template-columns:1fr}}}}
</style>
<style>
#codex-screener{{color-scheme:light dark}}#codex-screener td{{color:#d7e3ee}}#codex-screener button:focus-visible{{outline:2px solid #70b8ff;outline-offset:-3px}}
@media(prefers-color-scheme:light){{#codex-screener{{color:#142231;background:#f6f8fb;border-color:#cbd6e0}}#codex-screener .top{{background:#eef3f8;border-color:#cbd6e0}}#codex-screener .watch{{border-color:#cbd6e0}}#codex-screener .watch button{{color:#1c2c3c;border-color:#dbe3ea}}#codex-screener .watch button:hover,#codex-screener .watch button.active{{background:#e5f1fb}}#codex-screener .card{{background:#fff;border-color:#cbd6e0}}#codex-screener .metric,#codex-screener .scorebox{{background:#edf3f8}}#codex-screener .tag{{background:#e5f1fb;border-color:#b8ccdc;color:#25445f}}#codex-screener .chart{{background:#f7fafc;border-color:#cbd6e0}}#codex-screener .context{{background:#edf6fd;color:#274058}}#codex-screener .lead{{color:#142231}}#codex-screener .summary,#codex-screener li,#codex-screener td{{color:#344b60}}#codex-screener .sub,#codex-screener .muted,#codex-screener th,#codex-screener .posture,#codex-screener .provenance{{color:#5c7185}}#codex-screener th,#codex-screener td{{border-color:#dbe3ea}}#codex-screener svg text{{fill:#5c7185}}#codex-screener svg line{{stroke:#c5d2dd}}}}
</style>
<div class="top"><div class="headline"><div class="sub" id="run-meta"></div><div class="pill">RESEARCH ONLY · NOT AN EXECUTION ENGINE</div></div></div>
<div class="layout"><nav class="watch" id="watchlist" aria-label="Watchlist"></nav><main class="detail" id="detail" aria-live="polite"></main></div>
<script>
(()=>{{
const DATA=__DATA__; const root=document.getElementById('codex-screener'); const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])); const n=v=>typeof v==='number'&&Number.isFinite(v); const money=v=>n(v)?'$'+v.toLocaleString(undefined,{{maximumFractionDigits:2}}):'—'; const pct=v=>n(v)?v.toFixed(1)+'%':'—'; const compact=v=>n(v)?'$'+Intl.NumberFormat(undefined,{{notation:'compact',maximumFractionDigits:1}}).format(v):'—'; const et=v=>{{const d=new Date(v);return Number.isNaN(d.valueOf())?String(v??'—'):new Intl.DateTimeFormat(undefined,{{timeZone:'America/New_York',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short'}}).format(d)}};
let selected=0; document.getElementById('run-meta').textContent=`${{DATA.mode.replaceAll('_',' ')}} · cutoff ${{et(DATA.asOf)}} · generated ${{et(DATA.generatedAt)}}`;
function chart(bars){{if(!bars.length)return '<div class="muted" style="padding:24px">No valid regular-session chart data in this run artifact.</div>';const w=640,h=240,p=34,vals=bars.flatMap(b=>[b.c,b.e20,b.e50].filter(n)),lo=Math.min(...vals),hi=Math.max(...vals),pad=(hi-lo||1)*.08;const x=i=>p+(w-p-8)*(i/(Math.max(1,bars.length-1))),y=v=>h-p-(h-p-12)*((v-(lo-pad))/(hi-lo+2*pad));const path=k=>bars.map((b,i)=>n(b[k])?`${{i?'L':'M'}}${{x(i).toFixed(1)}},${{y(b[k]).toFixed(1)}}`:'').join(' ');return `<svg viewBox="0 0 ${{w}} ${{h}}" role="img" aria-label="Regular-session close and available moving averages"><line x1="${{p}}" y1="${{h-p}}" x2="${{w-8}}" y2="${{h-p}}" stroke="#294158"/><text x="3" y="18" fill="#9fb0c2" font-size="10">${{money(hi)}}</text><text x="3" y="${{h-p}}" fill="#9fb0c2" font-size="10">${{money(lo)}}</text><path class="line" d="${{path('c')}}"/><path class="ema20" d="${{path('e20')}}"/><path class="ema50" d="${{path('e50')}}"/><text x="${{p}}" y="${{h-8}}" fill="#9fb0c2" font-size="10">${{esc(bars[0].d)}}</text><text x="${{w-84}}" y="${{h-8}}" fill="#9fb0c2" font-size="10">${{esc(bars.at(-1).d)}}</text></svg>`}}
function list(values,kind){{return values.length?`<ul class="${{kind||''}}">${{values.map(x=>`<li>${{esc(x)}}</li>`).join('')}}</ul>`:'<div class="muted">Not supplied in this run artifact.</div>'}}
function options(rows){{return rows.length?`<table><thead><tr><th>Contract</th><th>Market</th><th>Greeks / liquidity</th><th>Status</th></tr></thead><tbody>${{rows.map(r=>`<tr><td><b>${{esc(r.contract)}} · ${{esc(r.type)}} ${{money(r.strike)}}</b><br><span class="muted">${{esc(r.expiry)}} · ${{n(r.dte)?r.dte.toFixed(0)+' DTE':'—'}}</span></td><td>${{money(r.bid)}} / ${{money(r.ask)}}<br><span class="muted">mid ${{money(r.mid)}} · spread ${{n(r.spreadPct)?pct(r.spreadPct*100):'—'}}</span></td><td>Δ ${{n(r.delta)?r.delta.toFixed(2):'—'}} · IV ${{n(r.iv)?pct(r.iv*100):'—'}}<br><span class="muted">OI ${{n(r.oi)?r.oi.toLocaleString():'—'}} · vol ${{n(r.volume)?r.volume.toLocaleString():'—'}}</span></td><td class="${{r.status==='CANDIDATE'?'status-good':'status-blocked'}}">${{esc(r.status)}}<br><span class="muted">quote ${{esc(et(r.quoteAt))}} · ${{r.quoteFresh?'fresh':'stale/reference'}}</span>${{r.reason?`<br><span class="muted">${{esc(r.reason)}}</span>`:''}}</td></tr>`).join('')}}</tbody></table>`:'<div class="muted">No liquid 30–240 DTE reference contracts passed the display screen. No option entry is eligible.</div>'}}
function signals(s){{const earnings=s.earningsDate!=='—'?`${{esc(s.earningsDate)}} · ${{esc(s.earningsTime)}}`:s.lastEarningsDate!=='—'?`unavailable · last reported ${{esc(s.lastEarningsDate)}}`:'unavailable';return `<table><tbody><tr><th>Option chain</th><td>${{esc(s.chainDate)}} · ${{n(s.validContracts)?s.validContracts.toLocaleString():'—'}} valid · median IV ${{n(s.medianIv)?pct(s.medianIv*100):'—'}} · put/call OI ${{n(s.putCallOi)?s.putCallOi.toFixed(2):'—'}}</td></tr><tr><th>Prior GEX levels</th><td>${{esc(s.gexDate)}} · put wall ${{money(s.putWall)}} · flip ${{money(s.gammaFlip)}} · call wall ${{money(s.callWall)}}</td></tr><tr><th>Flow alerts</th><td>${{esc(s.flowDate)}} · ${{n(s.flowAlerts)?s.flowAlerts.toLocaleString():'—'}} rows · reported premium ${{compact(s.flowPremium)}}</td></tr><tr><th>OI change page</th><td>${{esc(s.oiDate)}} · ${{n(s.oiRows)?s.oiRows.toLocaleString():'—'}} rows · summed explicit change ${{n(s.oiChange)?s.oiChange.toLocaleString():'—'}} <span class="muted">(bounded cross-check, not direction)</span></td></tr><tr><th>Dark-pool page</th><td>${{esc(s.darkDate)}} · ${{n(s.darkPrints)?s.darkPrints.toLocaleString():'—'}} unique prints · ${{compact(s.darkPremium)}} <span class="muted">(owner/direction unknown)</span></td></tr><tr><th>Next earnings</th><td>${{earnings}}</td></tr></tbody></table>`}}
function headlines(rows){{return rows.length?`<ul>${{rows.map(r=>`<li>${{esc(r.headline)}}<br><span class="muted">${{esc(r.source)}} · ${{esc(r.published)}}</span></li>`).join('')}}</ul>`:'<div class="muted">No provider headlines were present in the bounded current response.</div>'}}
function scenarios(rows){{return rows.length?`<table><thead><tr><th>Case</th><th>Conditions</th><th>Conditional read</th><th>Invalidation</th></tr></thead><tbody>${{rows.map(r=>`<tr><td class="scenario-name scenario-${{esc(r.name.toLowerCase())}}">${{esc(r.name)}}</td><td>${{esc(r.conditions.join('; '))}}</td><td>${{esc(r.outcome)}}</td><td>${{esc(r.invalidation.join('; '))}}</td></tr>`).join('')}}</tbody></table>`:'<div class="muted">No validated conditional scenarios were supplied.</div>'}}
function positions(rows){{return rows.length?`<table><thead><tr><th>Position</th><th>State</th><th>Handling note</th></tr></thead><tbody>${{rows.map(r=>`<tr><td>${{esc(r.contract||r.symbol||'—')}}</td><td>${{esc(r.state||'UNASSESSED')}}</td><td>${{esc(r.note||r.reason||'No handling rule supplied.')}}</td></tr>`).join('')}}</tbody></table>`:'<div class="muted">No tracked position supplied.</div>'}}
function render(){{const e=DATA.entries[selected];if(!e){{document.getElementById('detail').innerHTML='<div class="banner">No watchlist entries were supplied. The dashboard remains in NO_RECOMMENDATION state.</div>';return}}const t=e.technical,q=e.quality;document.getElementById('watchlist').innerHTML=DATA.entries.map((x,i)=>`<button class="${{i===selected?'active':''}" data-i="${{i}}"><div class="row"><span class="ticker">${{esc(x.ticker)}}</span><span class="action">${{esc(x.action)}}</span></div><div class="row"><span class="price">${{money(x.price)}}</span><span class="${{x.change>0?'status-good':x.change<0?'status-blocked':'muted'}}">${{pct(x.change)}}</span></div></button>`).join('');document.querySelectorAll('#watchlist button').forEach(b=>b.onclick=()=>{{selected=Number(b.dataset.i);render()}});const blocked=e.action==='NO_RECOMMENDATION'||!q.complete;document.getElementById('detail').innerHTML=`${{blocked?`<div class="banner"><b>${{esc(e.action)}}</b> · ${{esc(e.gateReasons.join('; ')||'A validated action was not supplied.')}}</div>`:''}}<div class="grid"><section class="card"><h2>${{esc(e.ticker)}} · regular-session technical context</h2><div class="metrics"><div class="metric"><label>Last close</label><b>${{money(e.price)}}</b></div><div class="metric"><label>RSI 14</label><b>${{n(t.rsi14)?t.rsi14.toFixed(1):'—'}}</b></div><div class="metric"><label>RV 20 annual</label><b>${{pct(t.rv20)}}</b></div><div class="metric"><label>126D drawdown</label><b>${{pct(t.drawdown)}}</b></div></div><div class="chart">${{chart(e.bars)}}</div><div class="muted">Close <span style="color:#70b8ff">—</span> · EMA20 <span style="color:#56d4bd">—</span> · EMA50 <span style="color:#b6a1ff">—</span> · ${{esc(t.coverage)}}</div></section><section class="card"><h2>Data quality</h2><table><tbody><tr><th>Completeness</th><td class="${{q.complete?'status-good':'status-blocked'}}">${{q.complete?'CONFIRMED':'UNCONFIRMED'}}</td></tr><tr><th>Freshness</th><td>${{esc(q.freshness)}}</td></tr><tr><th>Chain</th><td>${{esc(q.chain)}}</td></tr><tr><th>GEX</th><td>${{esc(q.gex)}}</td></tr><tr><th>Note</th><td>${{esc(q.note)}}</td></tr></tbody></table><div class="provenance">Provider: ${{esc(e.provenance.provider)}} · as-of: ${{esc(e.provenance.asOf)}} · sources: ${{esc(e.provenance.snapshots.join(', '))}}</div></section><section class="card"><h2>Grounded analyst claims</h2>${{list(e.claims,'')}}</section><section class="card"><h2>Counterevidence</h2>${{list(e.counter,'counter')}}<h2 style="margin-top:16px">Unknowns</h2>${{list(e.unknowns,'unknown')}}</section><section class="card"><h2>Option candidate gate</h2>${{options(e.options)}}</section><section class="card"><h2>Tracked position state</h2>${{positions(e.positions)}}</section></div>`}}
function renderDashboard(){{
 const e=DATA.entries[selected];
 if(!e){{document.getElementById('detail').innerHTML='<div class="banner">No watchlist entries were supplied. The dashboard remains in NO_RECOMMENDATION state.</div>';return}}
 const t=e.technical,q=e.quality,a=e.analysis;
 document.getElementById('watchlist').innerHTML=DATA.entries.map((x,i)=>`<button class="${{i===selected?'active':''}}" data-i="${{i}}"><div class="row"><span><span class="rank">${{n(x.rank)?'#'+x.rank.toFixed(0):'—'}}</span><span class="ticker">${{esc(x.ticker)}}</span></span><span class="price">${{money(x.price)}}</span></div><div class="row"><span class="posture">${{esc(x.analysis.posture.replaceAll('_',' '))}}</span><span class="${{x.change>0?'status-good':x.change<0?'status-blocked':'muted'}}">${{pct(x.change)}}</span></div></button>`).join('');
 document.querySelectorAll('#watchlist button').forEach(b=>b.onclick=()=>{{selected=Number(b.dataset.i);renderDashboard()}});
 const blocked=e.action==='NO_RECOMMENDATION'||!q.complete;
 document.getElementById('detail').innerHTML=`${{blocked?`<div class="banner"><b>${{esc(e.action)}}</b> · ${{esc(e.gateReasons.join('; ')||'A validated action was not supplied.')}} Current derivatives evidence is reference-only until refreshed.</div>`:''}}<div class="grid"><section class="card full"><div class="analysis-head"><div><h2>${{esc(e.ticker)}} · validated evidence synthesis</h2><div class="tags"><span class="tag">${{n(e.rank)?'#'+e.rank.toFixed(0)+' RESEARCH ATTENTION':'UNRANKED'}}</span><span class="tag">${{esc(a.posture.replaceAll('_',' '))}}</span><span class="tag">${{a.validated?'PROVENANCE VALIDATED':'FALLBACK ANALYSIS'}}</span></div></div></div><p class="lead">${{esc(a.dayOutlook)}}</p><p class="summary">${{esc(a.summary)}}</p><div class="scoregrid"><div class="scorebox"><div class="row"><span>Research priority</span><b>${{n(a.priority)?a.priority.toFixed(0)+'/100':'—'}}</b></div><div class="scorebar"><i style="width:${{n(a.priority)?Math.max(0,Math.min(100,a.priority)):0}}%"></i></div></div><div class="scorebox"><div class="row"><span>Evidence confidence</span><b>${{n(a.confidence)?a.confidence.toFixed(0)+'/100':'—'}}</b></div><div class="scorebar"><i style="width:${{n(a.confidence)?Math.max(0,Math.min(100,a.confidence)):0}}%"></i></div></div></div><div class="muted">These scores rank research attention and evidence support. They are not win probability, expected return, or trade confidence.</div></section><section class="card"><h2>${{esc(e.ticker)}} · regular-session technical context</h2><div class="metrics"><div class="metric"><label>Last close</label><b>${{money(e.price)}}</b></div><div class="metric"><label>1D return</label><b>${{pct(e.change)}}</b></div><div class="metric"><label>RSI 14</label><b>${{n(t.rsi14)?t.rsi14.toFixed(1):'—'}}</b></div><div class="metric"><label>RV 20 annual</label><b>${{pct(t.rv20)}}</b></div><div class="metric"><label>EMA 20</label><b>${{money(t.ema20)}}</b></div><div class="metric"><label>EMA 50</label><b>${{money(t.ema50)}}</b></div><div class="metric"><label>126D drawdown</label><b>${{pct(t.drawdown)}}</b></div></div><div class="chart">${{chart(e.bars)}}</div><div class="muted">Close <span style="color:#70b8ff">—</span> · EMA20 <span style="color:#56d4bd">—</span> · EMA50 <span style="color:#b6a1ff">—</span> · ${{esc(t.coverage)}}</div></section><section class="card"><h2>Data quality and lineage</h2><table><tbody><tr><th>Capture</th><td class="${{q.complete?'status-good':'status-blocked'}}">${{q.complete?'BOUNDED CAPTURE COMPLETE':'INCOMPLETE'}}</td></tr><tr><th>Freshness</th><td>${{esc(q.freshness)}}</td></tr><tr><th>Chain</th><td>${{esc(q.chain)}}</td></tr><tr><th>GEX</th><td>${{esc(q.gex)}}</td></tr><tr><th>Note</th><td>${{esc(q.note)}}</td></tr></tbody></table><div class="provenance">Provider: ${{esc(e.provenance.provider)}} · cutoff: ${{esc(e.provenance.asOf)}} · current source IDs: ${{esc(e.provenance.snapshots.join(', '))}}</div></section><section class="card"><h2>Options, positioning, and event evidence</h2>${{signals(e.signals)}}</section><section class="card"><h2>Recent provider headlines</h2>${{headlines(e.headlines)}}</section><section class="card full"><h2>Conditional scenarios · no assigned probabilities</h2>${{scenarios(a.scenarios)}}</section><section class="card"><h2>Evidence supporting this read</h2>${{list(e.claims,'')}}</section><section class="card"><h2>Counterevidence</h2>${{list(e.counter,'counter')}}<h2 style="margin-top:16px">Unknowns</h2>${{list(e.unknowns,'unknown')}}</section><section class="card full"><h2>Option reference screen · not an entry instruction</h2>${{options(e.options)}}<div class="context"><b>Analyst option context:</b> ${{esc(a.optionContext)}}</div></section><section class="card"><h2>Tracked position state</h2>${{positions(e.positions)}}</section></div>`;
}}
renderDashboard();
}})();
</script>
</section>'''
    fragment = template.replace("{{", "{").replace("}}", "}").replace("__DATA__", data)
    return _inline_script_fragment(fragment)


def build_fragment(run: Mapping[str, Any]) -> str:
    """Render the compact macro board and one drill-down workspace."""
    def compact_numbers(value: Any) -> Any:
        if isinstance(value, float):
            return round(value, 6)
        if isinstance(value, list):
            return [compact_numbers(item) for item in value]
        if isinstance(value, Mapping):
            return {key: compact_numbers(item) for key, item in value.items()}
        return value

    data = json.dumps(compact_numbers(normalize_run(run)), separators=(",", ":"), ensure_ascii=False)
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template = '''<meta charset="utf-8">
<section id="codex-screener" aria-label="Codex Screener research dashboard">
<style>
#codex-screener{{color-scheme:light dark;--me-bg:light-dark(#f7f9fc,#07111d);--me-panel:light-dark(#ffffff,#0b1826);--me-panel-2:light-dark(#edf3f8,#102031);--me-fg:light-dark(#172636,#edf4fb);--me-muted:light-dark(#5d7184,#a9b8c7);--me-border:light-dark(#ccd7e1,#22364a);--me-accent:light-dark(#1469a8,#70b8ff);--me-positive:light-dark(#067647,#32d583);--me-negative:light-dark(#b42318,#ff6b6b);--me-warning-bg:light-dark(#fff7df,#1b1810);--me-warning-fg:light-dark(#775000,#f3ce89);--me-series-1:light-dark(#1469a8,#70b8ff);--me-series-2:light-dark(#14806c,#56d4bd);--me-series-3:light-dark(#7259ac,#b6a1ff);--me-series-4:light-dark(#9a6500,#f3ce89);--me-series-5:light-dark(#546d82,#9eb0c2);--me-grid:light-dark(#dbe4ec,#263b4f);--me-popover:light-dark(#ffffff,#07111d);--me-popover-fg:light-dark(#172636,#edf4fb);display:block;width:100%;background:var(--me-bg);color:var(--me-fg);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;font-variant-numeric:tabular-nums;border-top:1px solid var(--me-border);border-bottom:1px solid var(--me-border)}}
#codex-screener *{{box-sizing:border-box}}#codex-screener button{{font:inherit}}#codex-screener .me-header{{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:16px 18px;border-bottom:1px solid var(--me-border);background:var(--me-panel)}}#codex-screener h1{{font-size:20px;font-weight:500;margin:0}}#codex-screener h2,#codex-screener h3{{font-size:13px;font-weight:500;margin:0}}#codex-screener .me-sub,#codex-screener .me-muted{{color:var(--me-muted);font-size:11px}}#codex-screener .me-mode{{font-size:11px;color:var(--me-warning-fg);text-align:right}}#codex-screener .me-alert{{padding:8px 18px;color:var(--me-warning-fg);background:var(--me-warning-bg);border-bottom:1px solid var(--me-border);font-size:11px}}#codex-screener .me-macro{{display:flex;flex-wrap:wrap;gap:8px 18px;padding:9px 18px;border-bottom:1px solid var(--me-border);font-size:11px}}#codex-screener .me-macro b{{font-weight:500}}#codex-screener .me-overview{{display:grid;grid-template-columns:minmax(330px,.78fr) minmax(0,1.22fr);align-items:start}}#codex-screener .me-ranking{{border-right:1px solid var(--me-border)}}#codex-screener .me-section-head{{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 14px;border-bottom:1px solid var(--me-border)}}#codex-screener .me-rank-list{{display:grid}}#codex-screener .me-rank-row{{appearance:none;width:100%;display:grid;grid-template-columns:34px minmax(74px,.8fr) minmax(84px,.9fr) minmax(60px,.55fr);align-items:center;gap:6px;padding:7px 12px;border:0;border-bottom:1px solid var(--me-border);background:transparent;color:var(--me-fg);text-align:left;cursor:pointer}}#codex-screener .me-rank-row>span{{min-width:0}}#codex-screener .me-rank-row:hover{{background:var(--me-panel-2)}}#codex-screener .me-rank-row[aria-pressed="true"]{{background:var(--me-panel-2);box-shadow:inset 3px 0 var(--me-accent)}}#codex-screener .me-rank{{font-size:11px;color:var(--me-accent)}}#codex-screener .me-symbol{{font-size:13px;font-weight:500}}#codex-screener .me-last{{display:block;text-align:right;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}#codex-screener .me-rank-meta{{display:block;color:var(--me-muted);font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}#codex-screener .me-score{{text-align:right;font-size:11px;white-space:nowrap}}#codex-screener .me-pos{{color:var(--me-positive)}}#codex-screener .me-neg{{color:var(--me-negative)}}#codex-screener .me-focus{{min-width:0;padding:12px 14px}}#codex-screener .me-focus-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:6px}}#codex-screener .me-selected-price{{font-size:18px;font-weight:500}}#codex-screener .me-outlook{{margin:6px 0 10px;color:var(--me-muted);font-size:11px}}#codex-screener .me-ask{{appearance:none;border:1px solid var(--me-border);background:transparent;color:var(--me-fg);padding:6px 8px;border-radius:4px;cursor:pointer;font-size:11px;white-space:nowrap}}#codex-screener .me-controls{{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;margin:8px 0}}#codex-screener .me-control-group{{display:flex;align-items:center;gap:8px}}#codex-screener .me-control{{appearance:none;border:0;background:transparent;color:var(--me-muted);padding:3px 1px;cursor:pointer;font-size:11px}}#codex-screener .me-control[aria-pressed="true"]{{color:var(--me-fg);text-decoration:underline;text-decoration-color:var(--me-accent);text-underline-offset:4px}}#codex-screener .me-swatch{{display:inline-block;width:12px;height:2px;margin-right:4px;vertical-align:middle;background:var(--swatch)}}#codex-screener .me-quote{{display:flex;flex-wrap:wrap;gap:6px 14px;padding:7px 9px;background:var(--me-panel-2);font-size:11px}}#codex-screener .me-quote b{{font-weight:500}}#codex-screener .me-chart-wrap{{position:relative;width:100%;height:360px;margin-top:7px}}#codex-screener .me-chart{{display:block;width:100%;height:100%}}#codex-screener .me-chart text{{fill:var(--me-muted);font-size:11px}}#codex-screener .me-chart .me-axis-title{{fill:var(--me-fg)}}#codex-screener .me-chart .me-grid-line,#codex-screener .me-chart .me-frame{{stroke:var(--me-grid);stroke-width:1;fill:none}}#codex-screener .me-chart .me-close{{stroke:var(--me-series-1);stroke-width:2;fill:none}}#codex-screener .me-chart .me-ema20{{stroke:var(--me-series-2);stroke-width:1.5;fill:none}}#codex-screener .me-chart .me-ema50{{stroke:var(--me-series-3);stroke-width:1.5;fill:none}}#codex-screener .me-chart .me-ref{{stroke:var(--me-muted);stroke-width:1;opacity:.7}}#codex-screener .me-chart .me-guide{{stroke:var(--me-fg);stroke-width:1;opacity:.6}}#codex-screener .me-chart .me-marker{{stroke:var(--me-bg);stroke-width:2}}#codex-screener .me-chart .me-hit{{fill:transparent;pointer-events:all;cursor:crosshair}}#codex-screener .me-tooltip{{position:absolute;display:none;pointer-events:none;z-index:2;min-width:152px;padding:7px 9px;background:var(--me-popover);color:var(--me-popover-fg);border:1px solid var(--me-border);font-size:11px}}#codex-screener .me-tooltip b{{font-weight:500}}#codex-screener .me-edge{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border-top:1px solid var(--me-border);border-bottom:1px solid var(--me-border)}}#codex-screener .me-edge-block{{padding:10px 12px;border-right:1px solid var(--me-border);font-size:11px}}#codex-screener .me-edge-block:last-child{{border-right:0}}#codex-screener .me-edge-label{{color:var(--me-muted);margin-bottom:4px}}#codex-screener .me-edge-value{{font-size:12px;font-weight:500;margin-bottom:3px}}#codex-screener .me-details details{{border-bottom:1px solid var(--me-border);background:var(--me-panel)}}#codex-screener .me-details summary{{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 16px;cursor:pointer;font-size:12px;font-weight:500}}#codex-screener .me-details summary span{{color:var(--me-muted);font-size:11px;font-weight:400;text-align:right}}#codex-screener .me-detail-body{{padding:0 16px 14px;font-size:11px}}#codex-screener .me-detail-body h3{{margin:10px 0 6px}}#codex-screener .me-detail-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}#codex-screener .me-detail-body p{{margin:0 0 8px}}#codex-screener .me-detail-body ul{{margin:0;padding-left:18px}}#codex-screener .me-detail-body li{{margin:4px 0}}#codex-screener .me-table-wrap{{overflow-x:auto}}#codex-screener table{{width:100%;border-collapse:collapse;font-size:11px}}#codex-screener th,#codex-screener td{{padding:6px 5px;border-top:1px solid var(--me-border);text-align:left;vertical-align:top;color:var(--me-fg)}}#codex-screener th{{color:var(--me-muted);font-weight:400}}#codex-screener .me-nowrap{{white-space:nowrap}}#codex-screener .me-empty{{color:var(--me-muted)}}
#codex-screener .me-edge{{grid-template-columns:repeat(3,minmax(0,1fr))}}#codex-screener .me-edge-block{{border-bottom:1px solid var(--me-border)}}#codex-screener .me-edge-block:nth-child(3n){{border-right:0}}#codex-screener .me-edge-block:nth-last-child(-n+3){{border-bottom:0}}#codex-screener .me-score-track{{height:3px;margin:6px 0;background:var(--me-grid)}}#codex-screener .me-score-fill{{display:block;height:100%;background:var(--me-accent)}}#codex-screener .me-table-note{{margin:8px 0;color:var(--me-muted)}}#codex-screener .me-heat td{{text-align:right;white-space:nowrap}}#codex-screener .me-heat th:first-child,#codex-screener .me-heat td:first-child{{text-align:left}}#codex-screener .me-heat-pos{{background:color-mix(in srgb,var(--me-positive) 16%,transparent)}}#codex-screener .me-heat-neg{{background:color-mix(in srgb,var(--me-negative) 16%,transparent)}}#codex-screener .me-heat-flat{{background:var(--me-panel-2)}}
#codex-screener{{height:min(820px,calc(100vh - 16px));min-height:620px;overflow:auto;overflow-anchor:none;overscroll-behavior:contain;scrollbar-gutter:stable;contain:layout paint style}}#codex-screener *,#codex-screener *::before,#codex-screener *::after{{animation:none!important;transition:none!important}}#codex-screener .me-thesis{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;margin:8px 0;background:var(--me-border);border:1px solid var(--me-border)}}#codex-screener .me-thesis-item{{min-width:0;padding:7px 9px;background:var(--me-panel-2);font-size:11px}}#codex-screener .me-thesis-item b{{display:block;margin-top:2px;font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}#codex-screener .me-thesis-rules{{grid-column:1/-1;padding:7px 9px;background:var(--me-panel);font-size:11px;line-height:1.45;color:var(--me-muted)}}#codex-screener .me-thesis-rules strong{{color:var(--me-fg);font-weight:500}}#codex-screener .me-chart .me-forecast{{stroke:var(--me-series-4);stroke-width:2.2;fill:none}}#codex-screener .me-chart .me-forecast-band{{fill:var(--me-series-1);opacity:.11;stroke:none}}#codex-screener .me-chart .me-v4-band-outer{{fill:var(--me-series-1);opacity:.09;stroke:none}}#codex-screener .me-chart .me-v4-band-inner{{fill:var(--me-series-1);opacity:.20;stroke:none}}#codex-screener .me-chart .me-v4-center{{stroke:var(--me-series-4);stroke-width:2.3;fill:none}}#codex-screener .me-chart .me-v3-center{{stroke:var(--me-series-3);stroke-width:1.5;stroke-dasharray:5 4;fill:none}}#codex-screener .me-chart .me-analog-forecast{{stroke:var(--me-series-1);stroke-width:1.6;stroke-dasharray:5 4;fill:none}}#codex-screener .me-chart .me-iv-band{{fill:var(--me-series-5);opacity:.09;stroke:none}}#codex-screener .me-chart .me-iv-edge{{stroke:var(--me-series-5);stroke-width:1;stroke-dasharray:2 4;fill:none}}#codex-screener .me-chart .me-today{{stroke:var(--me-warning-fg);stroke-width:1;stroke-dasharray:3 4}}#codex-screener .me-direction-bull{{color:var(--me-positive)}}#codex-screener .me-direction-bear{{color:var(--me-negative)}}
#codex-screener .me-derivatives{{margin:10px 0;border:1px solid var(--me-border);background:var(--me-panel)}}#codex-screener .me-deriv-head{{display:flex;align-items:baseline;justify-content:space-between;gap:10px;padding:8px 10px;border-bottom:1px solid var(--me-border)}}#codex-screener .me-deriv-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}}#codex-screener .me-deriv-card{{min-width:0;padding:9px 10px;border-right:1px solid var(--me-border);border-bottom:1px solid var(--me-border);font-size:11px;line-height:1.42}}#codex-screener .me-deriv-card:nth-child(3n){{border-right:0}}#codex-screener .me-deriv-card:nth-last-child(-n+3){{border-bottom:0}}#codex-screener .me-deriv-top{{display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin-bottom:2px}}#codex-screener .me-deriv-title{{color:var(--me-muted)}}#codex-screener .me-deriv-signal{{font-size:9px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;white-space:nowrap}}#codex-screener .me-deriv-verdict{{font-size:12px;font-weight:500;letter-spacing:.02em;margin-bottom:4px}}#codex-screener .me-deriv-metrics{{display:flex;flex-wrap:wrap;gap:3px 10px;margin-bottom:6px}}#codex-screener .me-deriv-metrics b{{font-weight:500}}#codex-screener .me-deriv-read{{display:grid;gap:4px;padding-top:5px;border-top:1px solid var(--me-grid);color:var(--me-muted)}}#codex-screener .me-deriv-read span{{display:block}}#codex-screener .me-deriv-read b{{color:var(--me-fg);font-weight:500}}#codex-screener .me-deriv-limit{{font-size:10px}}#codex-screener .me-deriv-card.me-deriv-bullish{{box-shadow:inset 3px 0 var(--me-positive);background:color-mix(in srgb,var(--me-positive) 6%,var(--me-panel))}}#codex-screener .me-deriv-card.me-deriv-bearish{{box-shadow:inset 3px 0 var(--me-negative);background:color-mix(in srgb,var(--me-negative) 6%,var(--me-panel))}}#codex-screener .me-deriv-card.me-deriv-cautious{{box-shadow:inset 3px 0 var(--me-warning-fg);background:color-mix(in srgb,var(--me-warning-fg) 5%,var(--me-panel))}}#codex-screener .me-deriv-card.me-deriv-bullish .me-deriv-signal,#codex-screener .me-deriv-card.me-deriv-bullish .me-deriv-verdict{{color:var(--me-positive)}}#codex-screener .me-deriv-card.me-deriv-bearish .me-deriv-signal,#codex-screener .me-deriv-card.me-deriv-bearish .me-deriv-verdict{{color:var(--me-negative)}}#codex-screener .me-deriv-card.me-deriv-cautious .me-deriv-signal,#codex-screener .me-deriv-card.me-deriv-cautious .me-deriv-verdict{{color:var(--me-warning-fg)}}#codex-screener .me-change-read{{padding:8px 10px;background:var(--me-panel-2);font-size:11px;line-height:1.5}}#codex-screener .me-change-read b{{font-weight:500}}#codex-screener .me-whale-note{{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:3px 9px;color:var(--me-muted);font-size:11px}}#codex-screener .me-whale-key{{white-space:nowrap}}#codex-screener .me-whale-key::before{{content:'';display:inline-block;width:6px;height:6px;margin-right:4px;background:currentColor;vertical-align:1px}}#codex-screener .me-whale-bull{{color:var(--me-positive)}}#codex-screener .me-whale-bear{{color:var(--me-negative)}}#codex-screener .me-whale-caution{{color:var(--me-warning-fg)}}
#codex-screener .me-eval{{margin:8px 0;border-top:1px solid var(--me-border);border-bottom:1px solid var(--me-border);background:var(--me-panel)}}#codex-screener .me-eval-head{{display:flex;align-items:baseline;justify-content:space-between;gap:12px;padding:8px 12px;border-bottom:1px solid var(--me-border)}}#codex-screener .me-eval-title{{font-size:12px;font-weight:500}}#codex-screener .me-eval-status{{color:var(--me-warning-fg);font-size:10px;text-align:right}}#codex-screener .me-eval-grid{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr))}}#codex-screener .me-eval-item{{min-width:0;padding:8px 10px;border-right:1px solid var(--me-border)}}#codex-screener .me-eval-item:last-child{{border-right:0}}#codex-screener .me-eval-label{{color:var(--me-muted);font-size:10px}}#codex-screener .me-eval-value{{margin:2px 0;font-size:12px;font-weight:500}}#codex-screener .me-eval-detail{{color:var(--me-muted);font-size:10px;line-height:1.35}}#codex-screener .me-eval-boundary{{padding:6px 12px;border-top:1px solid var(--me-border);color:var(--me-muted);font-size:10px}}
#codex-screener .me-chart .me-analog{{stroke:var(--me-series-1);stroke-width:2;fill:none}}#codex-screener .me-chart .me-analog-band{{fill:var(--me-series-1);opacity:.12;stroke:none}}#codex-screener .me-chart .me-baseline{{stroke:var(--me-muted);stroke-width:1;stroke-dasharray:4 4}}#codex-screener .me-chart .me-zero{{stroke:var(--me-fg);stroke-width:1;opacity:.55}}#codex-screener .me-chart .me-ref-level{{stroke:var(--me-muted);stroke-width:1;opacity:.42}}#codex-screener .me-chart .me-end-connector{{stroke:var(--me-muted);stroke-width:1;opacity:.7}}#codex-screener .me-end-label{{fill:var(--me-fg)!important;font-weight:500}}#codex-screener .me-end-center{{fill:var(--me-warning-fg)!important}}#codex-screener .me-levels{{display:flex;flex-wrap:wrap;align-items:center;gap:5px 13px;margin-top:6px;padding:6px 9px;border-top:1px solid var(--me-border);border-bottom:1px solid var(--me-border);font-size:11px;color:var(--me-muted)}}#codex-screener .me-levels b{{color:var(--me-fg);font-weight:500}}#codex-screener .me-level-key{{color:var(--me-accent);font-weight:500}}#codex-screener .me-pattern-read{{margin:8px 0;padding:8px 10px;background:var(--me-panel-2);font-size:11px;line-height:1.5}}#codex-screener .me-pattern-read b{{font-weight:500}}#codex-screener .me-controls{{justify-content:flex-start}}#codex-screener #me-series{{margin-left:auto}}
#codex-screener .me-intel{{display:grid;grid-template-columns:1.22fr 1fr 1fr;margin:10px 0;border:1px solid var(--me-border);background:var(--me-panel)}}#codex-screener .me-intel-card{{min-width:0;padding:10px 11px;border-right:1px solid var(--me-border)}}#codex-screener .me-intel-card:last-child{{border-right:0}}#codex-screener .me-intel-head{{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:5px}}#codex-screener .me-intel-kicker{{color:var(--me-muted);font-size:10px;letter-spacing:.06em;text-transform:uppercase}}#codex-screener .me-intel-verdict{{font-size:12px;font-weight:600;line-height:1.3;margin-bottom:5px}}#codex-screener .me-intel-main{{font-size:11px;line-height:1.45}}#codex-screener .me-intel-main b{{font-weight:500}}#codex-screener .me-intel-meta{{margin-top:5px;color:var(--me-muted);font-size:10px;line-height:1.35}}#codex-screener .me-intel-action{{appearance:none;border:0;background:transparent;color:var(--me-accent);padding:0;cursor:pointer;font-size:10px;text-decoration:underline;text-underline-offset:3px}}#codex-screener .me-flow-hot{{box-shadow:inset 3px 0 var(--me-negative)}}#codex-screener .me-flow-positive{{box-shadow:inset 3px 0 var(--me-positive)}}#codex-screener .me-flow-summary{{font-size:10.5px;line-height:1.35}}#codex-screener .me-flow-viz{{position:relative;width:100%;height:126px;margin:5px 0 2px}}#codex-screener .me-flow-mini{{display:block;width:100%;height:100%;touch-action:pan-y;overflow:visible}}#codex-screener .me-flow-mini text{{fill:var(--me-muted);font-size:9px}}#codex-screener .me-flow-zero{{stroke:var(--me-grid);stroke-width:1}}#codex-screener .me-flow-mean{{fill:none;stroke:var(--me-accent);stroke-width:1.5}}#codex-screener .me-flow-guide{{stroke:var(--me-fg);stroke-width:1;opacity:.5}}#codex-screener .me-flow-bar-pos{{fill:var(--me-positive);opacity:.8}}#codex-screener .me-flow-bar-neg{{fill:var(--me-negative);opacity:.8}}#codex-screener .me-flow-outlier{{fill:var(--me-warning-fg);stroke:var(--me-bg);stroke-width:1.5}}#codex-screener .me-flow-hit{{fill:transparent;pointer-events:all;cursor:crosshair}}#codex-screener .me-flow-tip{{position:absolute;display:none;z-index:2;min-width:145px;padding:6px 7px;background:var(--me-popover);color:var(--me-popover-fg);border:1px solid var(--me-border);font-size:10px;line-height:1.35;pointer-events:none}}#codex-screener .me-flow-legend{{display:flex;flex-wrap:wrap;gap:4px 10px;color:var(--me-muted);font-size:9.5px}}#codex-screener .me-flow-key{{display:inline-block;width:10px;height:2px;margin-right:3px;vertical-align:middle;background:var(--key)}}#codex-screener .me-forecast-note{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;margin:7px 0;background:var(--me-border);border:1px solid var(--me-border)}}#codex-screener .me-forecast-note>div{{padding:7px 8px;background:var(--me-panel-2);font-size:10px;line-height:1.4}}#codex-screener .me-forecast-note b{{display:block;color:var(--me-fg);font-size:11px;font-weight:500;margin-bottom:2px}}
#codex-screener .me-challenger{{margin:8px 0;border:1px solid var(--me-border);background:var(--me-panel-2)}}#codex-screener .me-challenger-head{{display:flex;justify-content:space-between;gap:12px;padding:7px 9px;border-bottom:1px solid var(--me-border);font-size:10px}}#codex-screener .me-challenger-head span,#codex-screener .me-challenger p{{color:var(--me-muted)}}#codex-screener .me-challenger-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}}#codex-screener .me-challenger-grid>div{{padding:7px 9px;border-right:1px solid var(--me-border);border-bottom:1px solid var(--me-border)}}#codex-screener .me-challenger-grid>div:nth-child(3n){{border-right:0}}#codex-screener .me-challenger-grid span,#codex-screener .me-challenger-grid small{{display:block;color:var(--me-muted);font-size:9px}}#codex-screener .me-challenger-grid b{{display:block;margin:2px 0;font-size:11px}}#codex-screener .me-challenger p{{margin:0;padding:6px 9px;font-size:9px}}#codex-screener .me-flip-map{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));margin:8px 0;border:1px solid var(--me-border)}}#codex-screener .me-flip-map>div{{padding:7px 9px;border-right:1px solid var(--me-border)}}#codex-screener .me-flip-map>div:last-child{{border-right:0}}#codex-screener .me-flip-map span{{display:block;color:var(--me-muted);font-size:9px;text-transform:uppercase;letter-spacing:.06em}}#codex-screener .me-flip-map b{{display:block;margin-top:3px;font-size:10px;line-height:1.4}}
@media(min-width:761px){{#codex-screener .me-ranking{{position:sticky;top:0;align-self:start;z-index:1;background:var(--me-panel)}}}}
@media(max-width:760px){{#codex-screener .me-overview{{grid-template-columns:1fr}}#codex-screener .me-ranking{{position:static;border-right:0;border-bottom:1px solid var(--me-border)}}#codex-screener .me-chart-wrap{{height:330px}}#codex-screener .me-edge{{grid-template-columns:1fr 1fr}}#codex-screener .me-edge-block:nth-child(2){{border-right:0}}#codex-screener .me-edge-block:nth-child(-n+2){{border-bottom:1px solid var(--me-border)}}}}
@media(max-width:760px){{#codex-screener .me-eval-grid{{grid-template-columns:1fr 1fr}}#codex-screener .me-eval-item{{border-bottom:1px solid var(--me-border)}}#codex-screener .me-eval-item:nth-child(2n){{border-right:0}}#codex-screener .me-eval-item:last-child{{grid-column:1/-1;border-bottom:0}}}}
@media(max-width:460px){{#codex-screener .me-header{{display:block}}#codex-screener .me-mode{{text-align:left;margin-top:6px}}#codex-screener .me-rank-row{{grid-template-columns:30px 66px 1fr 58px}}#codex-screener .me-rank-meta{{display:none}}#codex-screener .me-focus-head{{display:block}}#codex-screener .me-ask{{margin-top:8px}}#codex-screener .me-edge{{grid-template-columns:1fr}}#codex-screener .me-edge-block{{border-right:0;border-bottom:1px solid var(--me-border)}}#codex-screener .me-detail-grid{{grid-template-columns:1fr}}#codex-screener .me-chart-wrap{{height:300px}}#codex-screener .me-deriv-head{{display:block}}#codex-screener .me-deriv-grid{{grid-template-columns:1fr}}#codex-screener .me-deriv-card{{border-right:0;border-bottom:1px solid var(--me-border)}}#codex-screener .me-deriv-card:nth-last-child(-n+3){{border-bottom:1px solid var(--me-border)}}#codex-screener .me-deriv-card:last-child{{border-bottom:0}}#codex-screener .me-intel{{grid-template-columns:1fr}}#codex-screener .me-intel-card{{border-right:0;border-bottom:1px solid var(--me-border)}}#codex-screener .me-intel-card:last-child{{border-bottom:0}}#codex-screener .me-forecast-note,#codex-screener .me-challenger-grid,#codex-screener .me-flip-map{{grid-template-columns:1fr}}#codex-screener .me-challenger-grid>div,#codex-screener .me-flip-map>div{{border-right:0;border-bottom:1px solid var(--me-border)}}}}
@media(max-width:900px) and (min-width:461px){{#codex-screener .me-deriv-grid{{grid-template-columns:1fr 1fr}}#codex-screener .me-deriv-card:nth-child(3n){{border-right:1px solid var(--me-border)}}#codex-screener .me-deriv-card:nth-child(2n){{border-right:0}}#codex-screener .me-deriv-card:nth-last-child(-n+3){{border-bottom:1px solid var(--me-border)}}#codex-screener .me-deriv-card:nth-last-child(-n+2){{border-bottom:0}}}}
@media(max-width:760px) and (min-width:461px){{#codex-screener .me-edge-block{{border-right:1px solid var(--me-border);border-bottom:1px solid var(--me-border)}}#codex-screener .me-edge-block:nth-child(2n){{border-right:0}}#codex-screener .me-edge-block:nth-last-child(-n+2){{border-bottom:0}}}}
@media(max-width:760px){{#codex-screener .me-thesis{{grid-template-columns:1fr 1fr}}}}
@media(max-width:900px) and (min-width:461px){{#codex-screener .me-intel{{grid-template-columns:1fr 1fr}}#codex-screener .me-intel-card{{border-bottom:1px solid var(--me-border)}}#codex-screener .me-intel-card:nth-child(2){{border-right:0}}#codex-screener .me-intel-card:last-child{{grid-column:1/-1;border-right:0;border-bottom:0}}}}
@media(max-width:460px){{#codex-screener .me-header{{padding:12px}}#codex-screener .me-alert{{padding:8px 12px;line-height:1.45}}#codex-screener .me-macro{{display:grid;grid-template-columns:1fr 1fr;gap:8px 12px;padding:10px 12px}}#codex-screener .me-macro span{{min-width:0;overflow-wrap:anywhere}}#codex-screener .me-macro span:last-child{{grid-column:1/-1}}#codex-screener .me-section-head{{padding:10px 12px}}#codex-screener .me-rank-row{{grid-template-columns:28px 54px minmax(92px,1fr) 52px;min-height:44px;padding:7px 10px}}#codex-screener .me-last{{font-size:11px}}#codex-screener .me-focus{{padding:10px}}#codex-screener .me-controls,#codex-screener .me-control-group{{align-items:flex-start;flex-wrap:wrap}}#codex-screener .me-control{{min-height:34px;padding:7px 2px}}#codex-screener .me-ask{{min-height:38px}}#codex-screener .me-quote{{display:grid;grid-template-columns:1fr 1fr;gap:6px 10px}}#codex-screener .me-chart-wrap{{height:320px}}#codex-screener .me-thesis,#codex-screener .me-eval-grid{{grid-template-columns:1fr}}#codex-screener .me-eval-item,#codex-screener .me-eval-item:nth-child(2n),#codex-screener .me-eval-item:last-child{{grid-column:auto;border-right:0;border-bottom:1px solid var(--me-border)}}#codex-screener .me-eval-item:last-child{{border-bottom:0}}#codex-screener .me-details summary{{display:block;min-height:44px;padding:11px 12px;overflow-wrap:anywhere}}#codex-screener .me-details summary span{{display:block;margin-top:4px;text-align:left}}#codex-screener .me-detail-body{{padding:0 12px 14px}}#codex-screener th,#codex-screener td{{min-width:92px;padding:7px 6px}}#codex-screener th:first-child,#codex-screener td:first-child{{min-width:112px}}}}
@media(max-width:760px){{#codex-screener .me-option-grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}#codex-screener .me-rank-option{{font-size:9px}}}}
@media(max-width:460px){{#codex-screener .me-rank-row{{grid-template-columns:28px 54px minmax(126px,1fr) 44px}}#codex-screener .me-rank-option{{display:block!important}}#codex-screener .me-option-head{{display:block}}#codex-screener .me-option-status{{margin-top:5px;text-align:left}}}}
</style>
<style>#codex-screener{{--me-positive:light-dark(#067647,#32d583);--me-negative:light-dark(#b42318,#ff6b6b)}}#codex-screener .me-warn{{color:var(--me-warning-fg)}}#codex-screener .me-detail-grid>section{{min-width:0}}#codex-screener .me-table-wrap{{max-width:100%}}#codex-screener .me-thesis-item b.me-tracker,#codex-screener .me-thesis-item b.me-option-summary{{white-space:normal;overflow:visible;text-overflow:clip}}#codex-screener .me-option-call{{color:var(--me-positive)}}#codex-screener .me-option-put{{color:var(--me-negative)}}#codex-screener .me-option-panel.me-option-call{{box-shadow:inset 3px 0 var(--me-positive)}}#codex-screener .me-option-panel.me-option-put{{box-shadow:inset 3px 0 var(--me-negative)}}#codex-screener .me-option-quote{{display:block;margin-top:3px;color:var(--me-fg);font-size:10px;line-height:1.4}}#codex-screener .me-rank-option{{display:block;font-size:10px;line-height:1.25;white-space:normal;overflow:visible;text-align:right}}#codex-screener .me-rank-option b{{font-weight:600}}#codex-screener .me-rank-quote{{color:var(--me-muted)}}#codex-screener .me-option-list{{display:grid;gap:8px}}#codex-screener .me-option-card{{border:1px solid var(--me-border);background:var(--me-panel-2);padding:9px 10px;min-width:0}}#codex-screener .me-option-card.me-option-call{{box-shadow:inset 3px 0 var(--me-positive)}}#codex-screener .me-option-card.me-option-put{{box-shadow:inset 3px 0 var(--me-negative)}}#codex-screener .me-option-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:7px}}#codex-screener .me-option-contract{{font-size:10px;color:var(--me-muted);overflow-wrap:anywhere}}#codex-screener .me-option-identity{{font-size:13px;font-weight:600}}#codex-screener .me-option-status{{text-align:right;color:var(--me-warning-fg);font-size:9px;letter-spacing:.05em}}#codex-screener .me-option-grid{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:1px;background:var(--me-border)}}#codex-screener .me-option-field{{min-width:0;padding:6px 7px;background:var(--me-panel)}}#codex-screener .me-option-field span{{display:block;color:var(--me-muted);font-size:9px;text-transform:uppercase;letter-spacing:.04em}}#codex-screener .me-option-field b{{display:block;margin-top:2px;color:var(--me-fg);font-size:11px;font-weight:500;overflow-wrap:anywhere}}
#codex-screener .me-signal-map{{grid-column:1/-1;padding:8px 9px;background:var(--me-panel)}}#codex-screener .me-signal-map-head{{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-bottom:5px;font-size:10px;color:var(--me-muted)}}#codex-screener .me-signal-map-head b{{color:var(--me-fg);font-size:11px;font-weight:500}}#codex-screener .me-signal-row{{display:grid;grid-template-columns:82px minmax(80px,1fr) 62px;align-items:center;gap:8px;min-height:20px;font-size:10px}}#codex-screener .me-signal-rail{{position:relative;height:4px;background:linear-gradient(90deg,color-mix(in srgb,var(--me-negative) 22%,transparent) 0 34%,var(--me-grid) 34% 66%,color-mix(in srgb,var(--me-positive) 22%,transparent) 66% 100%)}}#codex-screener .me-signal-dot{{position:absolute;top:50%;width:8px;height:8px;border-radius:50%;transform:translate(-50%,-50%);background:var(--me-muted)}}#codex-screener .me-signal-dot.me-align{{background:var(--me-positive)}}#codex-screener .me-signal-dot.me-conflict{{background:var(--me-negative)}}#codex-screener .me-signal-state{{text-align:right}}
#codex-screener .me-eval{{display:grid;grid-template-columns:minmax(210px,.8fr) minmax(0,1.2fr);margin:8px 0}}#codex-screener .me-eval-head{{grid-column:1/-1}}#codex-screener .me-eval-ledger{{padding:9px 12px;border-right:1px solid var(--me-border)}}#codex-screener .me-eval-ledger-title{{display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin-bottom:7px;font-size:10px;color:var(--me-muted)}}#codex-screener .me-eval-ledger-title b{{color:var(--me-fg);font-size:12px;font-weight:500}}#codex-screener .me-eval-tape{{display:flex;height:8px;background:var(--me-grid);overflow:hidden}}#codex-screener .me-eval-seg-prospective{{background:var(--me-positive)}}#codex-screener .me-eval-seg-seed{{background:var(--me-warning-fg)}}#codex-screener .me-eval-seg-pending{{background:var(--me-series-5);opacity:.55}}#codex-screener .me-eval-legend{{display:flex;flex-wrap:wrap;gap:4px 10px;margin-top:6px;color:var(--me-muted);font-size:10px}}#codex-screener .me-eval-key{{display:inline-block;width:8px;height:8px;margin-right:3px;vertical-align:-1px;background:var(--key)}}#codex-screener .me-eval-option{{margin-top:8px;font-size:10px;color:var(--me-muted)}}#codex-screener .me-eval-option b{{color:var(--me-fg);font-weight:500}}#codex-screener .me-eval-horizons{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr))}}#codex-screener .me-eval-horizon{{min-width:0;padding:9px 10px;border-right:1px solid var(--me-border)}}#codex-screener .me-eval-horizon:last-child{{border-right:0}}#codex-screener .me-eval-h-label{{color:var(--me-muted);font-size:10px}}#codex-screener .me-eval-h-value{{margin:2px 0 6px;font-size:12px;font-weight:500}}#codex-screener .me-eval-axis{{position:relative;height:6px;background:var(--me-grid)}}#codex-screener .me-eval-modelbar{{display:block;height:100%;background:var(--me-accent)}}#codex-screener .me-eval-modelbar.me-lift-pos{{background:var(--me-positive)}}#codex-screener .me-eval-modelbar.me-lift-neg{{background:var(--me-negative)}}#codex-screener .me-eval-baseline{{position:absolute;top:-2px;width:2px;height:10px;background:var(--me-fg)}}#codex-screener .me-eval-h-detail{{margin-top:6px;color:var(--me-muted);font-size:10px;line-height:1.35}}#codex-screener .me-eval-boundary{{grid-column:1/-1}}
#codex-screener .me-edge{{display:block}}#codex-screener .me-engine-head{{padding:9px 12px;border-bottom:1px solid var(--me-border);background:var(--me-panel)}}#codex-screener .me-engine-title{{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:7px;font-size:11px}}#codex-screener .me-engine-title b{{font-size:12px;font-weight:500}}#codex-screener .me-depth-grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px 14px}}#codex-screener .me-depth-item{{min-width:0;color:var(--me-muted);font-size:10px}}#codex-screener .me-depth-label{{display:flex;justify-content:space-between;gap:6px}}#codex-screener .me-depth-track{{height:3px;margin-top:4px;background:var(--me-grid)}}#codex-screener .me-depth-fill{{display:block;height:100%;background:var(--me-series-5)}}#codex-screener .me-edge-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}}#codex-screener .me-score-fill.me-score-good{{background:var(--me-positive)}}#codex-screener .me-score-fill.me-score-low{{background:var(--me-negative)}}#codex-screener .me-score-fill.me-score-risk{{background:var(--me-negative)}}#codex-screener .me-score-fill.me-score-caution{{background:var(--me-warning-fg)}}
#codex-screener .me-map{{margin:8px 0;border-top:1px solid var(--me-border);border-bottom:1px solid var(--me-border);background:var(--me-panel)}}#codex-screener .me-map-head,#codex-screener .me-tool-head,#codex-screener .me-track-head{{display:flex;align-items:baseline;justify-content:space-between;gap:10px;padding:8px 12px;border-bottom:1px solid var(--me-border)}}#codex-screener .me-map-wrap{{position:relative;height:248px;padding:4px 10px 8px}}#codex-screener .me-map-svg,#codex-screener .me-ladder-svg{{display:block;width:100%;height:100%}}#codex-screener .me-map-svg text,#codex-screener .me-ladder-svg text{{fill:var(--me-muted);font-size:10px}}#codex-screener .me-map-grid,#codex-screener .me-ladder-line{{stroke:var(--me-grid);stroke-width:1}}#codex-screener .me-map-dot{{stroke:var(--me-bg);stroke-width:1.5;cursor:pointer}}#codex-screener .me-map-selected{{fill:none;stroke:var(--me-accent);stroke-width:2}}#codex-screener .me-map-hit{{fill:transparent;cursor:pointer}}#codex-screener .me-map-tip{{position:absolute;display:none;z-index:2;pointer-events:none;min-width:155px;padding:7px 8px;background:var(--me-popover);border:1px solid var(--me-border);font-size:10px}}#codex-screener .me-decision-tools{{display:grid;grid-template-columns:.8fr 1.2fr;margin-top:8px;border:1px solid var(--me-border)}}#codex-screener .me-change{{border-right:1px solid var(--me-border)}}#codex-screener .me-change-body{{padding:8px 10px}}#codex-screener .me-change-summary{{margin-bottom:6px;font-size:11px}}#codex-screener .me-change-row{{display:grid;grid-template-columns:78px minmax(80px,1fr) 42px;align-items:center;gap:7px;min-height:20px;font-size:10px}}#codex-screener .me-change-track{{position:relative;height:5px;background:var(--me-grid)}}#codex-screener .me-change-track:after{{content:'';position:absolute;left:50%;top:-2px;height:9px;border-left:1px solid var(--me-muted)}}#codex-screener .me-change-bar{{position:absolute;top:0;height:100%}}#codex-screener .me-ladder-wrap{{position:relative;height:148px;padding:2px 8px 6px}}#codex-screener .me-ladder-band-model{{fill:color-mix(in srgb,var(--me-accent) 18%,transparent)}}#codex-screener .me-ladder-band-iv{{fill:color-mix(in srgb,var(--me-muted) 14%,transparent)}}#codex-screener .me-ladder-mark{{stroke-width:2}}#codex-screener .me-ladder-spot{{fill:var(--me-fg);stroke:var(--me-bg);stroke-width:1}}#codex-screener .me-tracking{{border-bottom:1px solid var(--me-border);background:var(--me-panel)}}#codex-screener .me-track-grid{{display:grid;grid-template-columns:1.15fr .85fr}}#codex-screener .me-heatmap{{padding:10px 12px;border-right:1px solid var(--me-border)}}#codex-screener .me-heat-grid{{display:grid;gap:3px;align-items:center;font-size:10px}}#codex-screener .me-heat-cell{{appearance:none;min-width:0;height:22px;border:0;background:var(--me-grid);color:var(--me-fg);font-size:9px;cursor:pointer}}#codex-screener .me-heat-cell.me-correct{{background:color-mix(in srgb,var(--me-positive) 70%,var(--me-panel))}}#codex-screener .me-heat-cell.me-wrong{{background:color-mix(in srgb,var(--me-negative) 70%,var(--me-panel))}}#codex-screener .me-heat-ticker{{appearance:none;border:0;background:transparent;color:var(--me-muted);text-align:left;cursor:pointer;font-size:10px}}#codex-screener .me-heat-ticker[aria-pressed=true]{{color:var(--me-accent)}}#codex-screener .me-heat-detail{{min-height:20px;margin-top:6px;color:var(--me-muted);font-size:10px}}#codex-screener .me-timeline{{padding:10px 12px}}#codex-screener .me-timeline-title{{margin-bottom:12px;font-size:11px}}#codex-screener .me-time-rail{{display:grid;grid-template-columns:repeat(4,1fr);position:relative}}#codex-screener .me-time-rail:before{{content:'';position:absolute;left:8%;right:8%;top:8px;border-top:2px solid var(--me-grid)}}#codex-screener .me-time-node{{position:relative;text-align:center;font-size:9px;color:var(--me-muted)}}#codex-screener .me-time-dot{{display:block;width:16px;height:16px;margin:0 auto 5px;border:3px solid var(--me-panel);border-radius:50%;background:var(--me-grid)}}#codex-screener .me-time-dot.me-correct{{background:var(--me-positive)}}#codex-screener .me-time-dot.me-wrong{{background:var(--me-negative)}}#codex-screener .me-time-node b{{display:block;color:var(--me-fg);font-weight:500}}
#codex-screener [hidden]{{display:none!important}}#codex-screener .me-scope-head{{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:7px 14px;background:var(--me-panel-2);border-top:1px solid var(--me-border);border-bottom:1px solid var(--me-border)}}#codex-screener .me-scope-name{{font-size:10px;font-weight:500;letter-spacing:.08em;color:var(--me-accent)}}#codex-screener .me-system-status{{display:flex;flex-wrap:wrap;gap:6px 18px;padding:8px 18px;border-bottom:1px solid var(--me-border);background:var(--me-panel);font-size:11px}}#codex-screener .me-system-status b{{font-weight:500}}#codex-screener .me-watchboard{{background:var(--me-panel)}}#codex-screener .me-watch-head{{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 14px;border-bottom:1px solid var(--me-border)}}#codex-screener .me-watch-controls{{display:flex;gap:12px}}#codex-screener .me-watch-control{{appearance:none;border:0;background:transparent;color:var(--me-muted);padding:3px 0;cursor:pointer;font-size:11px}}#codex-screener .me-watch-control[aria-pressed=true]{{color:var(--me-fg);text-decoration:underline;text-decoration-color:var(--me-accent);text-underline-offset:4px}}#codex-screener .me-watchboard .me-ranking{{position:static!important;border:0}}#codex-screener .me-watchboard .me-rank-list{{grid-template-columns:1fr 1fr}}#codex-screener .me-watchboard .me-rank-row{{min-height:44px}}#codex-screener .me-watchboard .me-rank-row:nth-child(odd){{border-right:1px solid var(--me-border)}}#codex-screener .me-watchboard .me-map{{margin:0;border:0}}#codex-screener .me-watch-alerts{{display:grid;grid-template-columns:1fr 1fr}}#codex-screener .me-watch-alert{{appearance:none;display:grid;grid-template-columns:54px 60px minmax(0,1fr);gap:8px;align-items:center;min-height:44px;padding:7px 12px;border:0;border-right:1px solid var(--me-border);border-bottom:1px solid var(--me-border);background:transparent;color:var(--me-fg);text-align:left;cursor:pointer;font-size:11px}}#codex-screener .me-watch-alert:nth-child(2n){{border-right:0}}#codex-screener .me-watch-alert-kind{{color:var(--me-muted);font-size:10px}}#codex-screener .me-stock{{background:var(--me-bg)}}#codex-screener .me-stock .me-focus{{padding:12px 14px}}#codex-screener .me-stock-timeline{{margin:8px 0;border:1px solid var(--me-border);background:var(--me-panel);padding:9px 11px}}#codex-screener .me-stock-timeline .me-timeline{{padding:0}}#codex-screener .me-stock-record details{{border-top:1px solid var(--me-border)}}#codex-screener .me-performance{{background:var(--me-panel)}}#codex-screener .me-performance .me-eval{{margin:0;border-top:0}}#codex-screener .me-performance .me-tracking{{border-top:1px solid var(--me-border)}}#codex-screener .me-performance .me-track-grid{{grid-template-columns:1fr}}#codex-screener .me-performance .me-heatmap{{border-right:0}}#codex-screener .me-platform-details details{{border-bottom:1px solid var(--me-border)}}
@media(max-width:760px){{#codex-screener{{height:auto;min-height:0;overflow:visible;overflow-anchor:auto;overscroll-behavior:auto;scrollbar-gutter:auto;contain:layout style}}#codex-screener .me-eval{{grid-template-columns:1fr}}#codex-screener .me-eval-ledger{{border-right:0;border-bottom:1px solid var(--me-border)}}#codex-screener .me-eval-horizons{{grid-template-columns:1fr 1fr}}#codex-screener .me-eval-horizon{{border-bottom:1px solid var(--me-border)}}#codex-screener .me-eval-horizon:nth-child(2n){{border-right:0}}#codex-screener .me-eval-horizon:nth-last-child(-n+2){{border-bottom:0}}#codex-screener .me-depth-grid{{grid-template-columns:1fr 1fr}}#codex-screener .me-edge-grid{{grid-template-columns:1fr 1fr}}#codex-screener .me-decision-tools,#codex-screener .me-track-grid{{grid-template-columns:1fr}}#codex-screener .me-change,#codex-screener .me-heatmap{{border-right:0;border-bottom:1px solid var(--me-border)}}#codex-screener .me-map-wrap{{height:224px}}#codex-screener .me-watchboard .me-rank-list,#codex-screener .me-watch-alerts{{grid-template-columns:1fr}}#codex-screener .me-watchboard .me-rank-row:nth-child(odd),#codex-screener .me-watch-alert{{border-right:0}}}}
@media(max-width:460px){{#codex-screener .me-eval-head{{display:block}}#codex-screener .me-eval-status{{margin-top:6px;text-align:left}}#codex-screener .me-chart text{{font-size:13px}}#codex-screener .me-signal-map-head,#codex-screener .me-engine-title{{display:block}}#codex-screener .me-signal-map-head span,#codex-screener .me-engine-title span{{display:block;margin-top:3px}}#codex-screener .me-signal-row{{grid-template-columns:70px minmax(70px,1fr) 54px;gap:6px;min-height:26px;font-size:11px}}#codex-screener .me-eval-ledger-title,#codex-screener .me-eval-legend,#codex-screener .me-eval-option,#codex-screener .me-eval-h-label,#codex-screener .me-eval-h-detail,#codex-screener .me-depth-item{{font-size:11px}}#codex-screener .me-eval-horizons,#codex-screener .me-depth-grid,#codex-screener .me-edge-grid{{grid-template-columns:1fr}}#codex-screener .me-eval-horizon,#codex-screener .me-eval-horizon:nth-child(2n),#codex-screener .me-eval-horizon:nth-last-child(-n+2){{border-right:0;border-bottom:1px solid var(--me-border)}}#codex-screener .me-eval-horizon:last-child{{border-bottom:0}}}}</style>
<style>
#codex-screener{{color-scheme:dark;--me-bg:#050706;--me-panel:#090d0b;--me-panel-2:#0f1511;--me-fg:#e5ebe5;--me-muted:#93a095;--me-border:#283329;--me-accent:#f0a83a;--me-positive:#49e07d;--me-negative:#ff5b5b;--me-warning-bg:#1b1609;--me-warning-fg:#f6df68;--me-series-1:#52c7ff;--me-series-2:#49e0bd;--me-series-3:#bd8cff;--me-series-4:#f0b94f;--me-series-5:#8f9b90;--me-grid:#26352b;--me-popover:#050706;--me-popover-fg:#e5ebe5;background:#050706;font-family:"SFMono-Regular",Consolas,"Liberation Mono",monospace;letter-spacing:0;border-color:#f07e12}}
#codex-screener .me-header{{min-height:54px;padding:10px 14px;background:#090909;border-bottom-color:#f07e12;align-items:center}}
#codex-screener .me-header-context{{min-width:0}}
#codex-screener .me-mode{{margin-top:3px;color:#f6df68;text-align:left;text-transform:uppercase;letter-spacing:.04em}}
#codex-screener .me-consensus-pulse{{flex:0 0 86px;width:86px;height:32px;color:var(--me-positive)}}
#codex-screener .me-consensus-pulse.me-consensus-down{{color:var(--me-negative)}}
#codex-screener .me-consensus-svg{{display:block;width:86px;height:32px;overflow:visible}}
#codex-screener .me-consensus-line{{fill:none;stroke:currentColor;stroke-width:2.7;stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:1;stroke-dashoffset:1;animation:cs-consensus-draw 3.6s linear infinite!important}}
#codex-screener .me-consensus-point{{fill:currentColor;opacity:0;animation:cs-consensus-point 3.6s linear infinite!important}}
@keyframes cs-consensus-draw{{0%,7%{{stroke-dashoffset:1;opacity:0}}9%{{stroke-dashoffset:1;opacity:1}}60%,70%{{stroke-dashoffset:0;opacity:1}}100%{{stroke-dashoffset:0;opacity:0}}}}
@keyframes cs-consensus-point{{0%,59%{{opacity:0}}60%,70%{{opacity:1}}100%{{opacity:0}}}}
@media(prefers-reduced-motion:reduce){{#codex-screener .me-consensus-line{{animation:none!important;stroke-dashoffset:0;opacity:1}}#codex-screener .me-consensus-point{{animation:none!important;opacity:1}}}}
#codex-screener .me-alert{{background:#171309;border-bottom-color:#5d461c;color:#f6df68}}
#codex-screener .me-scope-head{{padding:6px 12px;background:#10150f;border-top-color:#f07e12;border-bottom-color:#394439}}
#codex-screener .me-scope-name{{color:#f0a83a;letter-spacing:.1em}}
#codex-screener .me-system-status,#codex-screener .me-macro{{background:#070a08}}
#codex-screener .me-watchboard,#codex-screener .me-performance,#codex-screener .me-details details,#codex-screener .me-engine-head,#codex-screener .me-tracking,#codex-screener .me-map,#codex-screener .me-stock-timeline,#codex-screener .me-derivatives{{background:#090d0b}}
#codex-screener .me-watch-head,#codex-screener .me-section-head,#codex-screener .me-map-head,#codex-screener .me-tool-head,#codex-screener .me-track-head,#codex-screener .me-deriv-head,#codex-screener .me-eval-head{{background:#0b100d}}
#codex-screener .me-watch-head h2,#codex-screener .me-section-head h2,#codex-screener .me-map-head h2,#codex-screener .me-tool-head h2,#codex-screener .me-track-head h2,#codex-screener .me-deriv-head h2,#codex-screener .me-eval-title{{color:#f0a83a;letter-spacing:.04em;text-transform:uppercase}}
#codex-screener .me-rank-row{{border-bottom-color:#1b241d}}
#codex-screener .me-rank-row:hover{{background:#111813}}
#codex-screener .me-rank-row[aria-pressed="true"]{{background:#1b1609;box-shadow:inset 3px 0 #f0a83a}}
#codex-screener .me-rank{{color:#93a095}}
#codex-screener .me-symbol{{color:#f0a83a}}
#codex-screener .me-watch-control[aria-pressed=true],#codex-screener .me-control[aria-pressed="true"]{{color:#f0a83a;text-decoration-color:#f0a83a}}
#codex-screener .me-ask{{border-radius:0;border-color:#3c4a3e;color:#f0a83a;background:#090d0b}}
#codex-screener .me-ask:hover{{background:#171309;border-color:#f0a83a}}
#codex-screener .me-thesis,#codex-screener .me-forecast-note,#codex-screener .me-decision-tools,#codex-screener .me-derivatives,#codex-screener .me-stock-timeline{{border-radius:0}}
#codex-screener .me-thesis-item,#codex-screener .me-forecast-note>div,#codex-screener .me-quote,#codex-screener .me-change-read{{background:#0f1511}}
#codex-screener .me-thesis-item:first-child b{{color:#f0a83a}}
#codex-screener .me-intraday-read{{grid-column:1/-1;display:grid;grid-template-columns:130px 100px 150px minmax(0,1fr);gap:8px;align-items:center;padding:7px 9px;background:#0a100c;border-left:3px solid #f0a83a;font-size:10px;color:var(--me-muted)}}
#codex-screener .me-intraday-read b{{font-size:11px;font-weight:600}}
#codex-screener .me-chart-wrap{{background:#050807;border-top:1px solid #202a22;border-bottom:1px solid #202a22}}
#codex-screener .me-chart .me-frame{{stroke:#405044}}
#codex-screener .me-chart .me-grid-line{{stroke:#26352b}}
#codex-screener .me-chart .me-close{{stroke:#52c7ff}}
#codex-screener .me-chart .me-ema20{{stroke:#49e0bd}}
#codex-screener .me-chart .me-ema50{{stroke:#bd8cff}}
#codex-screener .me-chart .me-today,#codex-screener .me-chart .me-forecast{{stroke:#f0b94f}}
#codex-screener .me-chart .me-live-spot{{stroke:var(--me-positive);fill:var(--me-positive);stroke-width:1.5;stroke-dasharray:4 3}}
#codex-screener .me-chart .me-live-spot.me-live-down{{stroke:var(--me-negative);fill:var(--me-negative)}}
#codex-screener .me-chart .me-live-label{{fill:var(--me-fg);font-size:10px}}
#codex-screener .me-chart .me-forecast-band{{fill:#52c7ff;opacity:.1}}
#codex-screener .me-tooltip,#codex-screener .me-flow-tip,#codex-screener .me-map-tip{{border-radius:0;border-color:#f0a83a;background:#050706}}
#codex-screener .me-intel-card,#codex-screener .me-deriv-card,#codex-screener .me-edge-block,#codex-screener .me-eval-horizon,#codex-screener .me-eval-item{{background:#090d0b}}
#codex-screener .me-intel-kicker,#codex-screener .me-deriv-title,#codex-screener .me-edge-label,#codex-screener .me-eval-label,#codex-screener th{{color:#93a095;text-transform:uppercase;letter-spacing:.05em}}
#codex-screener .me-intel-verdict,#codex-screener .me-deriv-verdict,#codex-screener .me-edge-value,#codex-screener .me-eval-value{{color:#e5ebe5}}
#codex-screener details[open]>summary{{color:#f0a83a;background:#0d120f}}
#codex-screener summary:hover{{background:#111813}}
#codex-screener table tbody tr:hover{{background:#0f1511}}
#codex-screener .me-score-track,#codex-screener .me-depth-track,#codex-screener .me-eval-axis,#codex-screener .me-change-track,#codex-screener .me-time-rail:before{{background:#1d281f}}
#codex-screener .me-score-fill,#codex-screener .me-eval-modelbar{{background:#52c7ff}}
#codex-screener .me-heat-cell{{border-radius:0}}
#codex-screener .me-product-mode{{display:flex;gap:8px;margin-top:7px}}#codex-screener .me-product-mode button{{appearance:none;border:0;border-bottom:1px solid transparent;background:transparent;color:var(--me-muted);padding:2px 0;font-size:10px;cursor:pointer}}#codex-screener .me-product-mode button[aria-pressed=true]{{color:var(--me-fg);border-color:var(--me-accent)}}
#codex-screener .me-freshness-strip{{display:flex;flex-wrap:wrap;gap:5px 12px;padding:7px 14px;border-bottom:1px solid var(--me-border);background:var(--me-panel-2);font-size:10px}}#codex-screener .me-freshness-strip b{{font-weight:500}}#codex-screener .me-fresh-current{{color:var(--me-positive)}}#codex-screener .me-fresh-warning{{color:var(--me-warning-fg)}}#codex-screener .me-fresh-stale{{color:var(--me-negative)}}
#codex-screener .me-focus-mode .me-performance,#codex-screener .me-focus-mode .me-stock-record,#codex-screener .me-focus-mode .me-details{{display:none}}#codex-screener .me-mobile-nav{{display:none}}
#codex-screener .me-chart .me-challenger-center{{stroke:#ff8ad8;stroke-width:1.3;stroke-dasharray:5 4;fill:none;opacity:.9}}#codex-screener .me-chart .me-challenger-dot{{fill:#ff8ad8;stroke:var(--me-bg);stroke-width:1}}
@media(max-width:760px){{#codex-screener .me-header{{padding:9px 12px}}#codex-screener .me-consensus-pulse{{flex-basis:78px;width:78px}}#codex-screener .me-consensus-svg{{width:78px}}#codex-screener .me-scope-head{{padding:7px 10px}}#codex-screener .me-intraday-read{{grid-template-columns:1fr 1fr}}}}
@media(max-width:760px){{#codex-screener{{padding-bottom:48px}}#codex-screener .me-mobile-nav{{position:fixed;display:grid;grid-template-columns:48px 1fr 48px;left:0;right:0;bottom:0;z-index:20;background:var(--me-panel);border-top:1px solid var(--me-border);padding-bottom:env(safe-area-inset-bottom)}}#codex-screener .me-mobile-nav button{{min-height:44px;border:0;border-right:1px solid var(--me-border);background:transparent;color:var(--me-fg)}}#codex-screener .me-mobile-nav button:last-child{{border-right:0}}}}
</style>
<header class="me-header"><div class="me-header-context"><div class="me-sub" id="me-run-meta"></div><div class="me-mode">CONDITIONAL THESIS · conviction is not probability</div><div class="me-product-mode" id="me-product-mode" aria-label="Dashboard density"></div><select id="me-replay" hidden aria-label="Replay published run"></select></div><div class="me-consensus-pulse" id="me-consensus-pulse"></div></header>
<div class="me-alert" id="me-alert" role="alert"></div>
<div class="me-freshness-strip" id="me-freshness" aria-label="Selected stock dataset freshness"></div>
<section class="me-system" aria-label="Market and data status"><div class="me-scope-head"><span class="me-scope-name">MARKET CONTEXT</span><span class="me-muted">applies to every ticker</span></div><div class="me-system-status" id="me-system-status"></div><div class="me-macro" id="me-macro" aria-label="Market context"></div></section>
<section class="me-watchboard" aria-label="Watchlist decisions"><div class="me-scope-head"><span class="me-scope-name">WATCHLIST DECISIONS</span><span class="me-muted">cross-stock comparison</span></div><div class="me-watch-head"><h2>Ranked theses</h2><div class="me-watch-controls" id="me-watch-controls" aria-label="Watchlist view"></div></div><div id="me-watch-ranking"><section class="me-ranking" aria-label="Directional evidence ranking"><div class="me-rank-list" id="me-ranking"></div></section></div><div id="me-watch-map" hidden><section class="me-map" aria-label="Cross-ticker opportunity map"><div class="me-map-head"><h2>Opportunity map</h2><span class="me-muted">evidence quality → · thesis evidence ↑ · size = tradeability</span></div><div class="me-map-wrap" id="me-map-wrap"><svg class="me-map-svg" id="me-map-svg" role="img" aria-label="Watchlist evidence quality and thesis evidence map"></svg><div class="me-map-tip" id="me-map-tip" role="tooltip"></div></div></section></div><div class="me-watch-alerts" id="me-watch-alerts" hidden></div></section>
<section class="me-stock" aria-label="Selected stock analysis"><div class="me-scope-head"><span class="me-scope-name" id="me-stock-scope">SELECTED STOCK</span><span class="me-muted">ticker-specific evidence</span></div><section class="me-focus" aria-label="Selected ticker analysis">
    <div class="me-focus-head"><div><h2 id="me-selected-title"></h2><div class="me-selected-price" id="me-selected-price"></div></div><button type="button" class="me-ask" id="me-ask">Stress-test with Codex</button></div>
    <div class="me-outlook" id="me-outlook"></div>
    <div class="me-thesis" id="me-thesis" aria-label="Selected directional thesis summary"></div>
    <section class="me-intel" id="me-intel" aria-label="News catalyst, unusual flow alert, and forecast explanation"></section>
    <div class="me-decision-tools"><section class="me-change" aria-label="Daily score changes"><div class="me-tool-head"><h2>Daily score change</h2><span class="me-muted">supportive input = green</span></div><div class="me-change-body" id="me-change"></div></section><section class="me-ladder" aria-label="Price decision ladder"><div class="me-tool-head"><h2>Price decision ladder</h2><span class="me-muted">stored references</span></div><div class="me-ladder-wrap" id="me-ladder-wrap"><svg class="me-ladder-svg" id="me-ladder-svg" role="img" aria-label="Selected ticker price, model range, implied range, and positioning levels"></svg><div class="me-map-tip" id="me-ladder-tip" role="tooltip"></div></div></section></div>
    <div class="me-controls"><div class="me-control-group" id="me-views" aria-label="Chart view"></div><div class="me-control-group" id="me-ranges" aria-label="Chart range"></div><div class="me-control-group" id="me-series" aria-label="Chart series"></div></div>
    <div class="me-quote" id="me-quote" aria-live="polite"></div>
    <div class="me-levels" id="me-levels" aria-label="Positioning price levels"></div>
    <div class="me-chart-wrap" id="me-chart-wrap"><svg class="me-chart" id="me-chart" role="img" aria-label="Price history with moving averages and positioning references"></svg><div class="me-tooltip" id="me-tooltip" role="tooltip"></div></div>
    <section class="me-stock-timeline" id="me-stock-timeline" aria-label="Selected stock forecast record"></section>
    <section class="me-derivatives" id="me-derivatives" aria-label="GEX, unusual flow, option strike, and supporting context"></section>
  </section><div class="me-edge" id="me-edge" aria-label="Selected stock evidence profile"></div><div class="me-stock-record me-details" id="me-stock-record"></div><div class="me-details" id="me-details"></div></section>
<section class="me-performance" aria-label="Model results"><div class="me-scope-head"><span class="me-scope-name">MODEL RESULTS</span><span class="me-muted">all tickers · frozen forecasts</span></div><section class="me-eval" id="me-evaluation" aria-label="Out-of-sample model accountability"></section><section class="me-tracking" id="me-tracking" aria-label="Forecast result tracking"></section><div class="me-platform-details me-details" id="me-platform-details"></div></section>
<nav class="me-mobile-nav" aria-label="Ticker navigation"><button type="button" id="me-prev" aria-label="Previous ticker">←</button><button type="button" id="me-current-ticker"></button><button type="button" id="me-next" aria-label="Next ticker">→</button></nav>
<script>
(()=>{{
const DATA=__DATA__;
const root=document.getElementById('codex-screener');
let publicationLoader=null;
const byId=id=>document.getElementById(id);
const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const n=v=>typeof v==='number'&&Number.isFinite(v);
const money=v=>n(v)?'$'+v.toLocaleString(undefined,{{maximumFractionDigits:2}}):'—';
const pct=v=>n(v)?v.toFixed(1)+'%':'—';
const pp=v=>n(v)?(v>0?'+':v<0?'−':'')+Math.abs(v).toFixed(1)+' pp':'—';
const ret=v=>n(v)?(v*100).toFixed(1)+'%':'—';
const compact=v=>n(v)?'$'+Intl.NumberFormat(undefined,{{notation:'compact',maximumFractionDigits:1}}).format(v):'—';
const compactNum=v=>n(v)?Intl.NumberFormat(undefined,{{notation:'compact',maximumFractionDigits:1,signDisplay:'exceptZero'}}).format(v):'—';
const signedCompact=v=>n(v)?(v>0?'+':v<0?'−':'')+'$'+Intl.NumberFormat(undefined,{{notation:'compact',maximumFractionDigits:1}}).format(Math.abs(v)):'—';
const signedCount=v=>n(v)?(v>0?'+':v<0?'−':'')+Math.abs(v).toLocaleString(undefined,{{maximumFractionDigits:0}}):'—';
const signedPrice=v=>n(v)?(v>0?'+':v<0?'−':'')+'$'+Math.abs(v).toLocaleString(undefined,{{maximumFractionDigits:2}}):'—';
const signedPct=v=>n(v)?(v>0?'+':v<0?'−':'')+Math.abs(v).toFixed(1)+'%':'—';
const dateLabel=v=>{{const source=typeof v==='string'&&/^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$/.test(v)?v+'T12:00:00Z':v,d=new Date(source);return Number.isNaN(d.valueOf())?String(v??'—'):new Intl.DateTimeFormat(undefined,{{timeZone:'America/New_York',month:'short',day:'numeric',year:'numeric'}}).format(d)}};
const timeLabel=v=>{{const d=new Date(v);return Number.isNaN(d.valueOf())?String(v??'—'):new Intl.DateTimeFormat(undefined,{{timeZone:'America/New_York',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short'}}).format(d)}};
const tone=v=>n(v)&&v>0?'me-pos':n(v)&&v<0?'me-neg':'';
const directionTone=v=>v==='BULLISH'?'me-direction-bull':v==='BEARISH'?'me-direction-bear':'';
const intradayTone=v=>v==='CONFIRMING'?'me-pos':v==='WEAKENING'?'me-neg':v==='MIXED'?'me-warn':'me-muted';
const humanStatus=v=>({{
 'PROVISIONAL_UNCONFIRMED':'Early sample; not OI-confirmed',
 'INSUFFICIENT_FLOW_HISTORY':'Needs a 20-session baseline',
 'INSUFFICIENT_HISTORY':'Not enough history',
 'INSUFFICIENT_INDEPENDENT_ANALOGS':'Too few independent matches',
 'INSUFFICIENT_COMPARABLE_HISTORY':'Too few comparable states',
 'MODELED_POSITIONING_REFERENCE':'Modeled positioning reference',
 'PRICE_RESPONSE_CONTEXT_ONLY':'Price-location context only',
 'DERIVED_FROM_CONSECUTIVE_CHAINS':'Confirmed from consecutive chains',
 'CONTEXT_ONLY_UNCALIBRATED':'Context only; not calibrated',
 'DESCRIPTIVE_NOT_CALIBRATED':'Descriptive; not calibrated',
 'EXPERIMENTAL_UNCALIBRATED':'Experimental; not calibrated',
 'ABOVE_FLIP':'Above gamma flip','BELOW_FLIP':'Below gamma flip','UNKNOWN':'Unknown'
}}[String(v)]||String(v??'Unavailable').replaceAll('_',' ').toLowerCase());
const posture=e=>e.analysis.posture.replaceAll('_',' ');
const trend=e=>{{const p=e.price,a=e.technical.ema20,b=e.technical.ema50;if(!n(p)||!n(a)||!n(b))return 'Trend incomplete';if(p>a&&p>b)return 'Above EMA20/50';if(p<a&&p<b)return 'Below EMA20/50';return 'Between EMA20/50'}};
const distance=(p,l)=>n(p)&&n(l)&&p!==0?(l/p-1)*100:null;
const optionMini=o=>o&&o.type&&o.type!=='—'?`${{o.type}} ${{money(o.strike)}} · ${{o.expiry}}`:(o?.contract||'—');
const optionTone=o=>o?.type==='CALL'?'me-option-call':o?.type==='PUT'?'me-option-put':'';
const optionIdentity=o=>o&&o.type&&o.type!=='—'?`${{o.type}} ${{money(o.strike)}} · ${{o.expiry}}`:(o?.contract||'—');
const rangeMap={{'1M':22,'3M':66,'6M':126,'ALL':180}};
const initialTicker=(()=>{{try{{return new URL(location.href).searchParams.get('ticker')?.toUpperCase()}}catch{{return null}}}})();
let selected=Math.max(0,DATA.entries.findIndex(e=>e.ticker===initialTicker)),range='6M',chartView='HISTORY',watchView='RANKING',lockedIndex=null,lastWidth=0,productMode=(()=>{{try{{return new URL(location.href).searchParams.get('mode')==='focus'?'FOCUS':'RESEARCH'}}catch{{return 'RESEARCH'}}}})();
const visible={{c:true,e20:true,e50:true}};
const forecastVisible={{v4:true,v3:true,iv:true,challengers:false}};

function setRunHeader(){{const r=DATA.refresh||{{}},live=r.mode==='INTRADAY';byId('me-run-meta').textContent=`Cutoff ${{timeLabel(DATA.asOf)}}${{live&&r.lastCycleAt?` · refreshed ${{timeLabel(r.lastCycleAt)}}`:''}}`;byId('me-alert').textContent=live?`INTRADAY ${{esc(r.marketStatus)}} · ${{esc(r.analysisMode)}} · daily forecast frozen`:'NO TRADE SIGNAL · stored prices and option quotes · V4 tracked in shadow.'}}
function setProductMode(mode){{productMode=mode==='RESEARCH'?'RESEARCH':'FOCUS';root.classList.toggle('me-focus-mode',productMode==='FOCUS');byId('me-product-mode').innerHTML=['FOCUS','RESEARCH'].map(x=>`<button type="button" data-mode="${{x}}" aria-pressed="${{x===productMode}}">${{x==='FOCUS'?'Decision view':'Research detail'}}</button>`).join('');byId('me-product-mode').querySelectorAll('button').forEach(button=>button.addEventListener('click',()=>setProductMode(button.dataset.mode)));try{{const url=new URL(location.href);url.searchParams.set('mode',productMode.toLowerCase());history.replaceState(null,'',url)}}catch{{}}}}
function renderReplay(){{const select=byId('me-replay'),entries=DATA.publications?.entries||[];if(!entries.length||!publicationLoader)return;select.hidden=false;select.innerHTML=`<option value="">As-published replay</option>${{entries.map(row=>`<option value="${{esc(row.url)}}">${{esc(row.date)}} · ${{esc(row.kind?.replaceAll('_',' '))}}</option>`).join('')}}`;select.onchange=async()=>{{if(!select.value)return;const ticker=DATA.entries[selected]?.ticker,next=await publicationLoader(select.value),publications=DATA.publications;DATA=next;DATA.publications=publications;selected=Math.max(0,DATA.entries.findIndex(x=>x.ticker===ticker));setRunHeader();renderConsensusPulse();renderSystemStatus();renderMacro();renderRanking();renderWatchAlerts();renderWatchControls();renderControls();renderEvaluation();renderPlatformTracking();renderPlatformDetails();renderSelected()}}}}
function renderFreshness(e){{const f=e.freshness||{{}},rows=Object.entries(f.datasets||{{}}),statusClass=s=>s==='CURRENT_COMPLETE'?'me-fresh-current':s==='STALE'||s==='UNAVAILABLE'?'me-fresh-stale':'me-fresh-warning';byId('me-freshness').innerHTML=`<span><b class="${{statusClass(f.overall)}}">${{esc(f.overall.replaceAll('_',' '))}}</b> · latest complete ${{esc(f.completeSession)}}</span>${{rows.map(([name,x])=>`<span class="${{statusClass(x.status)}}">${{esc(name.replaceAll('_',' '))}} ${{esc(x.session)}} · ${{esc(x.status.replaceAll('_',' '))}}</span>`).join('')}}`;}}
function renderConsensusPulse(){{const bullish=DATA.entries.filter(e=>e.thesis.direction==='BULLISH'),bearish=DATA.entries.filter(e=>e.thesis.direction==='BEARISH'),mixed=DATA.entries.length-bullish.length-bearish.length,weighted=DATA.entries.reduce((sum,e)=>sum+(e.thesis.direction==='BULLISH'?1:e.thesis.direction==='BEARISH'?-1:0)*(n(e.thesis.conviction)?e.thesis.conviction:0),0),changes=DATA.entries.map(e=>e.change).filter(n).sort((a,b)=>a-b),median=changes.length?changes[Math.floor(changes.length/2)]:0,isUp=weighted===0?median>=0:weighted>0,path=isUp?'M2 25 L13 20 L23 22 L34 14 L44 17 L56 8 L66 11 L84 2':'M2 2 L13 7 L23 5 L34 14 L44 11 L56 21 L66 18 L84 29',endY=isUp?2:29,label=`Engine consensus ${{isUp?'higher':'lower'}}: ${{bullish.length}} bullish, ${{bearish.length}} bearish, ${{mixed}} mixed`;const node=byId('me-consensus-pulse');node.classList.toggle('me-consensus-down',!isUp);node.innerHTML=`<svg class="me-consensus-svg" viewBox="0 0 86 32" role="img" aria-label="${{esc(label)}}"><title>${{esc(label)}}</title><path class="me-consensus-line" pathLength="1" d="${{path}}"></path><circle class="me-consensus-point" cx="84" cy="${{endY}}" r="2.5"></circle></svg>`}}
function eventDays(e){{if(!e.signals.earningsDate||e.signals.earningsDate==='—')return null;const event=new Date(e.signals.earningsDate+'T12:00:00Z');const asof=new Date(DATA.asOf);return Number.isNaN(event.valueOf())?null:Math.ceil((event-asof)/86400000)}}
function tideRead(context){{if(!context.available||!n(context.callMinusPut))return 'unavailable';return `${{context.callMinusPut>0?'call-led':'put-led'}} ${{signedCompact(context.callMinusPut)}}`}}
function renderSystemStatus(){{const entries=DATA.entries,validated=entries.filter(e=>e.analysis.validated).length,dates=[...new Set(entries.map(e=>e.signals.chainDate).filter(x=>x&&x!=='—'))],h=DATA.evaluation.horizons['1'],resolved=n(h.prospectiveEvaluated)?h.prospectiveEvaluated:0;byId('me-system-status').innerHTML=`<span><b>Data cutoff</b> ${{timeLabel(DATA.asOf)}}</span><span><b>Validated analyses</b> <span class="${{validated===entries.length?'me-pos':'me-neg'}}">${{validated}}/${{entries.length}}</span></span><span><b>Derivatives date</b> ${{esc(dates.join(', ')||'unavailable')}}</span><span><b>Calibration</b> <span class="${{DATA.evaluation.calibrated?'me-pos':'me-warn'}}">${{DATA.evaluation.calibrated?'passed':'blocked'}}</span></span><span><b>Prospective 1D results</b> ${{resolved.toFixed(0)}}</span>`}}
function renderMacro(){{const entries=DATA.entries,positive=entries.filter(e=>n(e.change)&&e.change>0).length,events=entries.filter(e=>{{const d=eventDays(e);return d!==null&&d>=0&&d<=7}}).length,changes=entries.map(e=>e.change).filter(n).sort((a,b)=>a-b),median=changes.length?changes[Math.floor(changes.length/2)]:null,c=DATA.contexts,cal=c.calendar||{{}},calendarRows=Array.isArray(cal.events)?cal.events:[],cutoff=new Date(DATA.enhancedAt),upcoming=calendarRows.find(row=>new Date(row.time)>=cutoff)||calendarRows.at(-1),semi=[c.smh.callMinusPut,c.soxx.callMinusPut].filter(n),semiState=semi.length<2?'incomplete':semi.every(v=>v>0)?'call-led':semi.every(v=>v<0)?'put-led':'mixed';byId('me-macro').innerHTML=`<span><b>Breadth</b> ${{positive}}/${{entries.length}} higher · median <span class="${{tone(median)}}">${{pct(median)}}</span></span><span><b>Market tide</b> <span class="${{tone(c.market.callMinusPut)}}">${{tideRead(c.market)}}</span></span><span><b>Semiconductor tide</b> ${{semiState}} · SMH <span class="${{tone(c.smh.callMinusPut)}}">${{signedCompact(c.smh.callMinusPut)}}</span> · SOXX <span class="${{tone(c.soxx.callMinusPut)}}">${{signedCompact(c.soxx.callMinusPut)}}</span></span><span><b>Company events ≤7D</b> ${{events}}</span><span><b>Next macro event</b> ${{esc(upcoming?.event||'none scheduled')}}${{upcoming?.time?' · '+esc(timeLabel(upcoming.time)):''}}</span>`}}
function evaluationHorizon(label,h){{const prospective=n(h.prospectiveDirectionCount)&&h.prospectiveDirectionCount>0,accuracy=prospective?h.prospectiveAccuracy:h.accuracy,baseline=prospective?h.prospectiveBaseline:h.baseline,lift=prospective?h.prospectiveLift:h.lift,count=prospective?h.prospectiveDirectionCount:h.directionCount,pending=n(h.pending)?h.pending:0;if(!n(accuracy)||!n(count)||count===0)return `<div class="me-eval-horizon"><div class="me-eval-h-label">${{esc(label)}}</div><div class="me-eval-h-value">Awaiting outcomes</div><div class="me-eval-axis" aria-hidden="true"></div><div class="me-eval-h-detail">${{pending.toFixed(0)}} frozen forecasts pending</div></div>`;const barClass=n(lift)&&lift>0?'me-lift-pos':n(lift)&&lift<0?'me-lift-neg':'',scope=prospective?'prospective':'diagnostic seed',interval=n(h.prospectiveAccuracyLow)&&n(h.prospectiveAccuracyHigh)?`${{pct(h.prospectiveAccuracyLow*100)}}–${{pct(h.prospectiveAccuracyHigh*100)}} row-level 95% interval`:'interval pending',origins=n(h.originSessions)?h.originSessions.toFixed(0):'0';return `<div class="me-eval-horizon"><div class="me-eval-h-label">${{esc(label)}} · ${{scope}}</div><div class="me-eval-h-value ${{n(lift)?tone(lift):''}}">${{pct(accuracy*100)}} correct</div><div class="me-eval-axis" role="img" aria-label="Model accuracy ${{pct(accuracy*100)}} versus majority baseline ${{n(baseline)?pct(baseline*100):'unavailable'}}"><span class="me-eval-modelbar ${{barClass}}" style="width:${{Math.max(0,Math.min(100,accuracy*100))}}%"></span>${{n(baseline)?`<i class="me-eval-baseline" style="left:${{Math.max(0,Math.min(100,baseline*100))}}%"></i>`:''}}</div><div class="me-eval-h-detail">baseline ${{n(baseline)?pct(baseline*100):'—'}} · lift ${{n(lift)?pp(lift*100):'—'}} · ${{origins}} origin dates<br>${{interval}}${{n(h.balancedAccuracy)?` · balanced ${{pct(h.balancedAccuracy*100)}}`:''}}</div></div>`}}
function renderEvaluation(){{const e=DATA.evaluation;if(!e.available){{byId('me-evaluation').innerHTML=`<div class="me-eval-head"><div class="me-eval-title">Model accountability</div><div class="me-eval-status">NOT INITIALIZED</div></div><div class="me-eval-boundary">No frozen evaluation report is attached.</div>`;return}}const h=e.horizons,option=h['1'],prospectiveOption=n(option.prospectiveOptionCount)&&option.prospectiveOptionCount>0,optionReturn=prospectiveOption?option.prospectiveMedianOptionReturn:option.medianOptionReturn,optionCount=prospectiveOption?option.prospectiveOptionCount:option.optionCount,registered=Math.max(1,n(e.registered)?e.registered:0),prospective=n(e.prospectiveEvaluated)?e.prospectiveEvaluated:0,seed=n(e.artifactEvaluated)?e.artifactEvaluated:0,pending=n(e.pending)?e.pending:0,width=value=>Math.max(0,Math.min(100,value/registered*100)),origins=n(h['1'].originSessions)?h['1'].originSessions:0;byId('me-evaluation').innerHTML=`<div class="me-eval-head"><div class="me-eval-title">Model accountability</div><div class="me-eval-status">${{e.calibrated?'CALIBRATED':'CALIBRATION BLOCKED'}} · ${{origins.toFixed(0)}} distinct 1D origin sessions</div></div><div class="me-eval-ledger"><div class="me-eval-ledger-title"><b>${{registered.toFixed(0)}} frozen forecasts</b><span>${{prospective.toFixed(0)}} prospective resolved · ${{pending.toFixed(0)}} pending</span></div><div class="me-eval-tape" role="img" aria-label="${{prospective.toFixed(0)}} prospective resolved, ${{seed.toFixed(0)}} diagnostic seed resolved, and ${{pending.toFixed(0)}} pending"><span class="me-eval-seg-prospective" style="width:${{width(prospective)}}%"></span><span class="me-eval-seg-seed" style="width:${{width(seed)}}%"></span><span class="me-eval-seg-pending" style="width:${{width(pending)}}%"></span></div><div class="me-eval-legend"><span><i class="me-eval-key" style="--key:var(--me-positive)"></i>${{prospective.toFixed(0)}} prospective resolved</span><span><i class="me-eval-key" style="--key:var(--me-warning-fg)"></i>${{seed.toFixed(0)}} diagnostic seed</span><span><i class="me-eval-key" style="--key:var(--me-series-5)"></i>${{pending.toFixed(0)}} pending</span></div><div class="me-eval-option">1D paper option mark: <b class="${{tone(optionReturn)}}">${{n(optionReturn)?pct(optionReturn):'unavailable'}}</b> · n=${{n(optionCount)?optionCount.toFixed(0):'0'}} · stored ask to later bid</div></div><div class="me-eval-horizons">${{evaluationHorizon('1 session',h['1'])}}${{evaluationHorizon('5 sessions',h['5'])}}${{evaluationHorizon('10 sessions',h['10'])}}${{evaluationHorizon('20 sessions',h['20'])}}</div><div class="me-eval-boundary">Early read only. Pending rows are not misses. Accuracy must beat the majority baseline across more independent origin dates before the engine can claim predictive lift.</div>`}}
function svgEl(tag,attrs={{}}){{const node=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attrs).forEach(([k,v])=>node.setAttribute(k,v));return node}}
function drawOpportunityMap(){{const svg=byId('me-map-svg'),wrap=byId('me-map-wrap'),tip=byId('me-map-tip');if(!svg||!wrap||byId('me-watch-map').hidden)return;const width=Math.max(300,Math.round(wrap.clientWidth-20)),height=Math.max(190,Math.round(wrap.clientHeight-12)),m={{l:38,r:22,t:18,b:28}},x=v=>m.l+(width-m.l-m.r)*Math.max(0,Math.min(100,v||0))/100,y=v=>height-m.b-(height-m.t-m.b)*Math.max(0,Math.min(100,v||0))/100;svg.innerHTML='';svg.setAttribute('viewBox',`0 0 ${{width}} ${{height}}`);[25,50,75].forEach(v=>{{svg.append(svgEl('line',{{x1:x(v),y1:m.t,x2:x(v),y2:height-m.b,class:'me-map-grid'}}),svgEl('line',{{x1:m.l,y1:y(v),x2:width-m.r,y2:y(v),class:'me-map-grid'}}))}});const xt=svgEl('text',{{x:width-m.r,y:height-5,'text-anchor':'end'}});xt.textContent='Evidence quality';const yt=svgEl('text',{{x:4,y:m.t+5}});yt.textContent='Thesis evidence';svg.append(xt,yt);DATA.entries.forEach((e,i)=>{{const q=e.edge.dimensions.evidenceQuality,c=e.thesis.conviction,r=4+Math.max(0,Math.min(100,e.edge.dimensions.tradeability||0))/22,cx=x(q),cy=y(c),fill=e.thesis.direction==='BULLISH'?'var(--me-positive)':e.thesis.direction==='BEARISH'?'var(--me-negative)':'var(--me-muted)';if(i===selected)svg.appendChild(svgEl('circle',{{cx,cy,r:r+4,class:'me-map-selected'}}));const dot=svgEl('circle',{{cx,cy,r,class:'me-map-dot',fill}}),label=svgEl('text',{{x:cx,y:cy-r-3,'text-anchor':'middle'}}),hit=svgEl('circle',{{cx,cy,r:Math.max(16,r+7),class:'me-map-hit',role:'button','aria-label':`${{e.ticker}}, ${{e.thesis.direction}}, evidence quality ${{q}}, thesis evidence ${{c}}`}});label.textContent=e.ticker;const show=event=>{{const box=wrap.getBoundingClientRect();tip.innerHTML=`<b>${{esc(e.ticker)}} · <span class="${{directionTone(e.thesis.direction)}}">${{esc(e.thesis.direction)}}</span></b><br>evidence quality ${{n(q)?q.toFixed(0):'—'}} · thesis evidence ${{n(c)?c.toFixed(0):'—'}}<br>tradeability ${{n(e.edge.dimensions.tradeability)?e.edge.dimensions.tradeability.toFixed(0):'—'}} · catalyst risk ${{n(e.edge.dimensions.catalystRisk)?e.edge.dimensions.catalystRisk.toFixed(0):'—'}}`;tip.style.left=Math.max(4,Math.min(wrap.clientWidth-165,event.clientX-box.left+8))+'px';tip.style.top=Math.max(4,Math.min(wrap.clientHeight-70,event.clientY-box.top+8))+'px';tip.style.display='block'}};hit.addEventListener('pointermove',show);hit.addEventListener('pointerleave',()=>tip.style.display='none');hit.addEventListener('click',()=>selectTicker(i));svg.append(dot,label,hit)}})}}
function renderScoreChange(e){{const host=byId('me-change'),p=e.previous;if(!p){{host.innerHTML='<div class="me-empty">No prior prepared run is attached.</div>';return}}const d=e.edge.dimensions,old=p.dimensions,bear=e.thesis.direction==='BEARISH',rows=[['Direction',d.directional,old.directional,bear?-1:1],['Volatility',d.volatility,old.volatility,1],['Positioning',d.positioning,old.positioning,bear?-1:1],['Tradeability',d.tradeability,old.tradeability,1],['Catalyst risk',d.catalystRisk,old.catalystRisk,-1],['Evidence',d.evidenceQuality,old.evidenceQuality,1]].map(([label,now,prior,sign])=>({{label,delta:n(now)&&n(prior)?now-prior:null,support:n(now)&&n(prior)?(now-prior)*sign:null}})),valid=rows.filter(row=>n(row.support)),scale=Math.max(5,...valid.map(row=>Math.abs(row.support))),scoreDelta=n(e.thesis.conviction)&&n(p.conviction)?e.thesis.conviction-p.conviction:null,rankDelta=n(e.rank)&&n(p.rank)?p.rank-e.rank:null,lead=valid.slice().sort((a,b)=>Math.abs(b.support)-Math.abs(a.support))[0];host.innerHTML=`<div class="me-change-summary"><b class="${{tone(scoreDelta)}}">Evidence ${{n(p.conviction)?p.conviction.toFixed(0):'—'}} → ${{n(e.thesis.conviction)?e.thesis.conviction.toFixed(0):'—'}} (${{n(scoreDelta)?pp(scoreDelta):'—'}})</b>${{n(rankDelta)?` · rank ${{rankDelta>0?'rose':'fell'}} ${{Math.abs(rankDelta).toFixed(0)}`:''}}<br><span class="me-muted">Largest supportive-input move: ${{lead?esc(lead.label)+' '+pp(lead.delta):'unavailable'}}.</span></div>${{rows.map(row=>{{const w=n(row.support)?Math.min(50,Math.abs(row.support)/scale*50):0,left=n(row.support)&&row.support<0?50-w:50,cls=n(row.support)&&row.support>0?'me-pos':'me-neg';return `<div class="me-change-row"><span>${{esc(row.label)}}</span><span class="me-change-track"><i class="me-change-bar ${{cls}}" style="left:${{left}}%;width:${{w}}%;background:currentColor"></i></span><span class="${{cls}}">${{n(row.delta)?pp(row.delta):'—'}}</span></div>`}}).join('')}}<div class="me-muted" style="margin-top:5px;font-size:9px">Input movement, not causal attribution. Analog and robustness penalties also affect the total.</div>`}}
function ladderLevels(e){{return [['Put wall',e.signals.putWall,'var(--me-negative)'],['GEX flip',e.signals.gammaFlip,'var(--me-warning-fg)'],['EMA20',e.technical.ema20,'var(--me-series-2)'],['Dark shelf',e.whale.dark.level,'var(--me-series-5)'],['Spot',e.price,'var(--me-fg)'],['Call wall',e.signals.callWall,'var(--me-positive)'],['Option B/E',e.thesis.option.breakeven,'var(--me-series-3)']].filter(row=>n(row[1]))}}
function drawDecisionLadder(e){{const svg=byId('me-ladder-svg'),wrap=byId('me-ladder-wrap'),tip=byId('me-ladder-tip');if(!svg||!wrap)return;const levels=ladderLevels(e),spot=e.price,model=n(spot)&&n(e.thesis.low20)&&n(e.thesis.high20)?[spot*(1+e.thesis.low20),spot*(1+e.thesis.high20)]:null,move=e.whale.volatility.move30,iv=n(spot)&&n(move)?[spot*(1-move),spot*(1+move)]:null,values=levels.map(x=>x[1]).concat(model||[],iv||[]),lo=Math.min(...values),hi=Math.max(...values),pad=(hi-lo||1)*.05,width=Math.max(280,Math.round(wrap.clientWidth-16)),height=136,m={{l:18,r:18}},x=v=>m.l+(width-m.l-m.r)*(v-(lo-pad))/(hi-lo+2*pad);svg.innerHTML='';svg.setAttribute('viewBox',`0 0 ${{width}} ${{height}}`);if(model)svg.appendChild(svgEl('rect',{{x:x(model[0]),y:54,width:Math.max(1,x(model[1])-x(model[0])),height:18,class:'me-ladder-band-model'}}));if(iv)svg.appendChild(svgEl('rect',{{x:x(iv[0]),y:78,width:Math.max(1,x(iv[1])-x(iv[0])),height:12,class:'me-ladder-band-iv'}}));svg.appendChild(svgEl('line',{{x1:m.l,y1:72,x2:width-m.r,y2:72,class:'me-ladder-line'}}));const labelRows=[18,34,116,132],lastX=[-999,-999,-999,-999];levels.slice().sort((a,b)=>a[1]-b[1]).forEach(row=>{{const [label,value,color]=row,cx=x(value),preferred=label==='Spot'?[0,1,2,3]:[0,2,1,3],rowIndex=preferred.find(index=>cx-lastX[index]>82)??preferred.toSorted((a,b)=>lastX[a]-lastX[b])[0],labelY=labelRows[rowIndex],upper=rowIndex<2;lastX[rowIndex]=cx;const mark=svgEl('line',{{x1:cx,y1:upper?labelY+4:70,x2:cx,y2:upper?70:labelY-10,class:'me-ladder-mark',stroke:color}}),anchor=cx<65?'start':cx>width-65?'end':'middle',text=svgEl('text',{{x:cx,y:labelY,'text-anchor':anchor}});text.textContent=`${{label}} ${{money(value)}}`;svg.append(mark,text);if(label==='Spot')svg.appendChild(svgEl('circle',{{cx,cy:72,r:5,class:'me-ladder-spot'}}))}});const modelText=svgEl('text',{{x:m.l,y:67}}),ivText=svgEl('text',{{x:m.l,y:91}});modelText.textContent='20D observed';ivText.textContent='30D IV';svg.append(modelText,ivText);svg.setAttribute('aria-label',`${{e.ticker}} stored price ladder from ${{money(lo)}} to ${{money(hi)}}`);svg.onpointermove=event=>{{const box=svg.getBoundingClientRect(),value=(lo-pad)+Math.max(0,Math.min(1,(event.clientX-box.left)/box.width))*(hi-lo+2*pad),nearest=levels.slice().sort((a,b)=>Math.abs(a[1]-value)-Math.abs(b[1]-value))[0];tip.innerHTML=`<b>${{esc(nearest[0])}} ${{money(nearest[1])}}</b><br>${{n(spot)?signedPct((nearest[1]/spot-1)*100):'—'}} from spot`;tip.style.left=Math.max(4,Math.min(wrap.clientWidth-155,event.clientX-box.left+8))+'px';tip.style.top='4px';tip.style.display='block'}};svg.onpointerleave=()=>tip.style.display='none'}}
function renderTracking(e){{const ev=DATA.evaluation,rows=ev.heatmap||[],dates=[...new Set(rows.map(r=>r.origin))].sort(),tickers=DATA.entries.map(x=>x.ticker),cell=new Map(rows.map(r=>[r.ticker+'|'+r.origin,r])),columns=`68px repeat(${{Math.max(1,dates.length)}},minmax(42px,1fr))`,head=`<span></span>${{dates.map(d=>`<span style="text-align:center">${{esc(d.slice(5))}}</span>`).join('')}}`,body=tickers.map(t=>`<button class="me-heat-ticker" data-ticker="${{esc(t)}}" aria-pressed="${{t===e.ticker}}">${{esc(t)}}</button>${{dates.map(d=>{{const r=cell.get(t+'|'+d);return r?`<button class="me-heat-cell ${{r.correct?'me-correct':'me-wrong'}}" data-key="${{esc(t+'|'+d)}}" aria-label="${{esc(t)}} ${{esc(d)}} ${{r.correct?'correct':'wrong'}}">${{r.correct?'HIT':'MISS'}}</button>`:'<span class="me-heat-cell" aria-hidden="true"></span>'}}).join('')}}`).join(''),timeline=(ev.timelines||{{}})[e.ticker]||[];byId('me-tracking').innerHTML=`<div class="me-track-head"><h2>Forecast tracking</h2><span class="me-muted">prospective outcomes only · descriptive until calibration passes</span></div><div class="me-track-grid"><div class="me-heatmap"><div class="me-heat-grid" style="grid-template-columns:${{columns}}">${{head}}${{body}}</div><div class="me-heat-detail" id="me-heat-detail" aria-live="polite">Select a result for realized return and paper option mark.</div></div><div class="me-timeline"><div class="me-timeline-title"><b>${{esc(e.ticker)}} latest resolved-origin timeline</b>${{timeline[0]?` · published ${{esc(timeline[0].origin)}}`:''}}</div><div class="me-time-rail">${{[1,5,10,20].map(h=>{{const r=timeline.find(x=>x.horizon===h),state=!r||r.status!=='EVALUATED'?'pending':r.correct?'correct':'wrong';return `<div class="me-time-node"><i class="me-time-dot me-${{state}}"></i><b>${{h}}D · ${{state}}</b>${{r&&n(r.return)?pct(r.return):'awaiting'}}${{r&&n(r.optionReturn)?`<br>option ${{pct(r.optionReturn)}}`:''}}</div>`}}).join('')}}</div></div></div>`;byId('me-tracking').querySelectorAll('.me-heat-ticker').forEach(button=>button.addEventListener('click',()=>{{const i=DATA.entries.findIndex(x=>x.ticker===button.dataset.ticker);if(i>=0)selectTicker(i)}}));byId('me-tracking').querySelectorAll('.me-heat-cell[data-key]').forEach(button=>button.addEventListener('click',()=>{{const r=cell.get(button.dataset.key);byId('me-heat-detail').innerHTML=`<b class="${{r.correct?'me-pos':'me-neg'}}">${{esc(r.ticker)}} ${{r.correct?'hit':'miss'}}</b> · ${{esc(r.direction)}} · underlying <span class="${{tone(r.return)}}">${{n(r.return)?pct(r.return):'—'}}</span> · paper option <span class="${{tone(r.optionReturn)}}">${{n(r.optionReturn)?pct(r.optionReturn):'unavailable'}}</span>`}}))}}
function renderStockTimeline(e){{const timeline=(DATA.evaluation.timelines||{{}})[e.ticker]||[];byId('me-stock-timeline').innerHTML=`<div class="me-timeline"><div class="me-timeline-title"><b>${{esc(e.ticker)}} forecast record</b>${{timeline[0]?` · origin ${{esc(timeline[0].origin)}}`:''}} <span class="me-muted">· prospective</span></div><div class="me-time-rail">${{[1,5,10,20].map(h=>{{const r=timeline.find(x=>x.horizon===h),state=!r||r.status!=='EVALUATED'?'pending':r.correct?'correct':'wrong';return `<div class="me-time-node"><i class="me-time-dot me-${{state}}"></i><b>${{h}}D · ${{state}}</b>${{r&&n(r.return)?pct(r.return):'awaiting'}}${{r&&n(r.optionReturn)?`<br>option ${{pct(r.optionReturn)}}`:''}}</div>`}}).join('')}}</div></div>`}}
function renderPlatformTracking(){{const ev=DATA.evaluation,rows=ev.heatmap||[],dates=[...new Set(rows.map(r=>r.origin))].sort(),tickers=DATA.entries.map(x=>x.ticker),cell=new Map(rows.map(r=>[r.ticker+'|'+r.origin,r])),columns=`68px repeat(${{Math.max(1,dates.length)}},minmax(42px,1fr))`,head=`<span></span>${{dates.map(d=>`<span style="text-align:center">${{esc(d.slice(5))}}</span>`).join('')}}`,body=tickers.map(t=>`<button class="me-heat-ticker" data-ticker="${{esc(t)}}" aria-pressed="${{t===DATA.entries[selected].ticker}}">${{esc(t)}}</button>${{dates.map(d=>{{const r=cell.get(t+'|'+d);return r?`<button class="me-heat-cell ${{r.correct?'me-correct':'me-wrong'}}" data-key="${{esc(t+'|'+d)}}" aria-label="${{esc(t)}} ${{esc(d)}} ${{r.correct?'correct':'wrong'}}">${{r.correct?'HIT':'MISS'}}</button>`:'<span class="me-heat-cell" aria-hidden="true"></span>'}}).join('')}}`).join('');byId('me-tracking').innerHTML=`<div class="me-track-head"><h2>Prospective 1-session results</h2><span class="me-muted">observed outcomes · not calibrated probabilities</span></div><div class="me-track-grid"><div class="me-heatmap"><div class="me-heat-grid" style="grid-template-columns:${{columns}}">${{head}}${{body}}</div><div class="me-heat-detail" id="me-heat-detail" aria-live="polite">Select a result for the realized return and paper option mark.</div></div></div>`;byId('me-tracking').querySelectorAll('.me-heat-ticker').forEach(button=>button.addEventListener('click',()=>{{const i=DATA.entries.findIndex(x=>x.ticker===button.dataset.ticker);if(i>=0)selectTicker(i)}}));byId('me-tracking').querySelectorAll('.me-heat-cell[data-key]').forEach(button=>button.addEventListener('click',()=>{{const r=cell.get(button.dataset.key);byId('me-heat-detail').innerHTML=`<b class="${{r.correct?'me-pos':'me-neg'}}">${{esc(r.ticker)}} ${{r.correct?'hit':'miss'}}</b> · ${{esc(r.direction)}} · underlying <span class="${{tone(r.return)}}">${{n(r.return)?pct(r.return):'—'}}</span> · paper option <span class="${{tone(r.optionReturn)}}">${{n(r.optionReturn)?pct(r.optionReturn):'unavailable'}}</span>`}}))}}
function updateTrackingSelection(){{const ticker=DATA.entries[selected].ticker;byId('me-tracking').querySelectorAll('.me-heat-ticker').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.ticker===ticker)))}}
function rankRow(e,i){{const t=e.thesis,o=t.option,w=e.whale,g=w.gex,flow=w.flow,whaleMeta=w.available?`γ ${{g.regime.toLowerCase()}} · Δ ${{compactNum(flow.delta)}}`:'whale data unavailable';return `<button type="button" class="me-rank-row" data-index="${{i}}" aria-pressed="${{i===selected}}" aria-label="Select ${{esc(e.ticker)}} ${{esc(t.direction)}} thesis"><span class="me-rank">#${{n(e.rank)?e.rank.toFixed(0):'—'}}</span><span><span class="me-symbol">${{esc(e.ticker)}}</span><span class="me-rank-meta ${{directionTone(t.direction)}}">${{esc(t.direction)}} · ${{esc(whaleMeta)}}</span></span><span><span class="me-last">${{money(e.price)}} <span class="${{tone(e.change)}}">${{pct(e.change)}}</span></span><span class="me-rank-option ${{optionTone(o)}}"><b>${{esc(optionIdentity(o))}}</b><br><span class="me-rank-quote">bid ${{money(o.bid)}} · ask ${{money(o.ask)}}</span></span></span><span class="me-score ${{directionTone(t.direction)}}">${{n(t.conviction)?t.conviction.toFixed(0):'—'}} / 100</span></button>`}}
function renderRanking(){{byId('me-ranking').innerHTML=DATA.entries.map(rankRow).join('');byId('me-ranking').querySelectorAll('button').forEach(button=>button.addEventListener('click',()=>selectTicker(Number(button.dataset.index))))}}
function updateRankingState(){{byId('me-ranking').querySelectorAll('button').forEach((button,i)=>button.setAttribute('aria-pressed',String(i===selected)))}}
function watchAlerts(){{const rows=[];DATA.entries.forEach((e,index)=>{{const f=e.edge.flow,p=e.previous,scoreDelta=p&&n(p.conviction)&&n(e.thesis.conviction)?e.thesis.conviction-p.conviction:null,impact=newsImpact(e);if((n(f.percentile)&&f.percentile>=.9)||(n(f.zscore)&&Math.abs(f.zscore)>=2))rows.push({{index,kind:'FLOW',tone:n(f.directionalPremium)&&f.directionalPremium<0?'me-neg':'me-pos',priority:Math.abs(f.zscore||0)+2,text:`${{signedCompact(f.directionalPremium)}} directional premium · ${{n(f.percentile)?Math.round(f.percentile*100)+'th percentile':'outlier'}} · ${{f.oiConfirmed?'OI confirmed':'OI unconfirmed'}}`}});if(n(e.edge.news.major)&&e.edge.news.major>0)rows.push({{index,kind:'NEWS',tone:impact.direction==='NEGATIVE'?'me-neg':impact.direction==='POSITIVE'?'me-pos':'',priority:e.edge.news.major+1,text:`${{e.edge.news.major.toFixed(0)}} major headline${{e.edge.news.major===1?'':'s'}} · ${{impact.direction.toLowerCase()}} ${{impact.scope.toLowerCase()}} read`}});if(n(scoreDelta)&&Math.abs(scoreDelta)>=5)rows.push({{index,kind:'SCORE',tone:scoreDelta>0?'me-pos':'me-neg',priority:Math.abs(scoreDelta)/5,text:`evidence ${{scoreDelta>0?'rose':'fell'}} ${{Math.abs(scoreDelta).toFixed(1)}} points · now ${{e.thesis.conviction.toFixed(0)}}/100`}});if(n(e.change)&&Math.abs(e.change)>=3)rows.push({{index,kind:'PRICE',tone:tone(e.change),priority:Math.abs(e.change)/4,text:`${{e.change>0?'rose':'fell'}} ${{Math.abs(e.change).toFixed(1)}}% in the latest regular session`}})}});return rows.sort((a,b)=>b.priority-a.priority).slice(0,10)}}
function renderWatchAlerts(){{const rows=watchAlerts();byId('me-watch-alerts').innerHTML=rows.length?rows.map(row=>{{const e=DATA.entries[row.index];return `<button type="button" class="me-watch-alert" data-index="${{row.index}}"><b>${{esc(e.ticker)}}</b><span class="me-watch-alert-kind ${{row.tone}}">${{row.kind}}</span><span>${{esc(row.text)}}</span></button>`}}).join(''):'<div class="me-empty" style="padding:12px">No material cross-watchlist alert met the displayed thresholds.</div>';byId('me-watch-alerts').querySelectorAll('button').forEach(button=>button.addEventListener('click',()=>selectTicker(Number(button.dataset.index))))}}
function setWatchView(next){{watchView=next;byId('me-watch-ranking').hidden=next!=='RANKING';byId('me-watch-map').hidden=next!=='MAP';byId('me-watch-alerts').hidden=next!=='ALERTS';byId('me-watch-controls').querySelectorAll('button').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.view===next)));if(next==='MAP')requestAnimationFrame(drawOpportunityMap)}}
function renderWatchControls(){{const views=[['RANKING','Ranking'],['MAP','Map'],['ALERTS','Alerts']];byId('me-watch-controls').innerHTML=views.map(([key,label])=>`<button type="button" class="me-watch-control" data-view="${{key}}" aria-pressed="${{key===watchView}}">${{label}}</button>`).join('');byId('me-watch-controls').querySelectorAll('button').forEach(button=>button.addEventListener('click',()=>setWatchView(button.dataset.view)));setWatchView(watchView)}}
function renderControls(){{const views=[['HISTORY','Price history'],['FORECAST','Forecast'],['OUTCOMES','State outcomes']];byId('me-views').innerHTML=views.map(([key,label])=>`<button type="button" class="me-control" data-view="${{key}}" aria-pressed="${{key===chartView}}">${{label}}</button>`).join('');byId('me-ranges').innerHTML=chartView==='OUTCOMES'?'':Object.keys(rangeMap).map(key=>`<button type="button" class="me-control" data-range="${{key}}" aria-pressed="${{key===range}}">${{key}}</button>`).join('');const historySeries=[['c','Close','var(--me-series-1)'],['e20','EMA20','var(--me-series-2)'],['e50','EMA50','var(--me-series-3)']],forecastSeries=[['v4','V4 volatility fan','var(--me-series-4)'],['v3','V3 baseline','var(--me-series-3)'],['iv','IV envelope','var(--me-series-5)'],['challengers','Shadow models','#ff8ad8']],series=chartView==='FORECAST'?forecastSeries:historySeries,state=chartView==='FORECAST'?forecastVisible:visible;byId('me-series').innerHTML=chartView==='OUTCOMES'?'':series.map(([key,label,color])=>`<button type="button" class="me-control" data-series="${{key}}" aria-pressed="${{state[key]}}"><span class="me-swatch" style="--swatch:${{color}}"></span>${{label}}</button>`).join('');byId('me-views').querySelectorAll('button').forEach(button=>button.addEventListener('click',()=>{{chartView=button.dataset.view;if(chartView==='FORECAST')range='1M';lockedIndex=null;renderControls();drawActiveChart()}}));byId('me-ranges').querySelectorAll('button').forEach(button=>button.addEventListener('click',()=>{{range=button.dataset.range;lockedIndex=null;renderControls();drawActiveChart()}}));byId('me-series').querySelectorAll('button').forEach(button=>button.addEventListener('click',()=>{{const target=chartView==='FORECAST'?forecastVisible:visible;target[button.dataset.series]=!target[button.dataset.series];if(!Object.values(target).some(Boolean))target[Object.keys(target)[0]]=true;lockedIndex=null;renderControls();drawActiveChart()}}))}}
function currentRows(){{const bars=DATA.entries[selected].bars;const count=rangeMap[range]||bars.length;return bars.slice(-Math.min(count,bars.length))}}
function showQuote(row,previous){{if(!row){{byId('me-quote').innerHTML='<span>No chart observation available.</span>';return}}const delta=n(previous?.c)&&previous.c!==0?(row.c/previous.c-1)*100:null;byId('me-quote').innerHTML=`<span><b>${{esc(dateLabel(row.d))}}</b></span><span>Close <b>${{money(row.c)}}</b></span><span>Session <b class="${{tone(delta)}}">${{pct(delta)}}</b></span><span>EMA20 <b>${{money(row.e20)}}</b></span><span>EMA50 <b>${{money(row.e50)}}</b></span>`}}
function renderLevels(e){{const refs=[['P','Put wall',e.signals.putWall],['F','Gamma flip',e.signals.gammaFlip],['M','Magnet',e.signals.gammaMagnet],['C','Call wall',e.signals.callWall]].filter(([, ,value])=>n(value)).sort((a,b)=>b[2]-a[2]);byId('me-levels').innerHTML=refs.length?`<span><b>Positioning levels vs spot</b></span>${{refs.map(([key,label,value])=>{{const d=distance(e.price,value);return `<span><span class="me-level-key">${{key}}</span> ${{esc(label)}} <b>${{money(value)}}</b> <span class="${{tone(d)}}">${{n(d)?'('+pct(d)+')':''}}</span></span>`}}).join('')}}`:'<span>No positioning levels available.</span>'}}
function showForecastSummary(e,future){{const v4=e.edge.forecastV4,v4End=v4.path.at(-1),v4Week=v4.path[4],v3End=future.at(-1),spot=e.price,iv=e.whale.volatility.move30;if((!v4End&&!v3End)||!n(spot)||spot===0){{const latest=currentRows().at(-1);byId('me-quote').innerHTML=`<span><b>Forecast unavailable</b></span><span>${{esc(v4.reason||e.edge.forecast.reason)}}</span><span>Latest close <b>${{money(latest?.c)}}</b></span>`;return}}const move=value=>n(value)?(value/spot-1)*100:null;byId('me-quote').innerHTML=`<span><b>V4 shadow forecast</b></span><span>1W center <b class="${{tone(move(v4Week?.center))}}">${{money(v4Week?.center)}} · ${{pct(move(v4Week?.center))}}</b></span><span>1W p10–p90 <b>${{pct(move(v4Week?.p10))}} to ${{pct(move(v4Week?.p90))}}</b></span><span>20D center <b class="${{tone(move(v4End?.center))}}">${{money(v4End?.center)}} · ${{pct(move(v4End?.center))}}</b></span><span>20D p10–p90 <b>${{pct(move(v4End?.p10))}} to ${{pct(move(v4End?.p90))}}</b></span><span>V3 center <b class="${{tone(move(v3End?.center))}}">${{pct(move(v3End?.center))}}</b></span><span>30D IV move <b>±${{n(iv)?pct(iv*100):'—'}}</b></span><span class="me-muted">shadow · n=${{n(v4.sample)?v4.sample.toFixed(0):'—'}} · not calibrated</span>`}}
function drawChart(){{const e=DATA.entries[selected],rows=currentRows(),svg=byId('me-chart'),wrap=byId('me-chart-wrap'),tip=byId('me-tooltip');tip.style.display='none';renderLevels(e);if(!rows.length){{svg.innerHTML='<text x="16" y="28">No valid chart data.</text>';showQuote(null);return}}const w=Math.max(320,Math.round(wrap.getBoundingClientRect().width)),h=360,m={{l:58,r:16,t:25,b:42}},pw=w-m.l-m.r,ph=h-m.t-m.b;svg.setAttribute('viewBox',`0 0 ${{w}} ${{h}}`);const vals=[];rows.forEach(row=>{{if(visible.c&&n(row.c))vals.push(row.c);if(visible.e20&&n(row.e20))vals.push(row.e20);if(visible.e50&&n(row.e50))vals.push(row.e50)}});if(!vals.length)vals.push(...rows.map(row=>row.c).filter(n));let lo=Math.min(...vals),hi=Math.max(...vals),pad=(hi-lo||Math.abs(hi)||1)*.08;lo-=pad;hi+=pad;const x=i=>m.l+pw*(i/Math.max(1,rows.length-1)),y=v=>m.t+ph*(1-(v-lo)/(hi-lo));const pathFor=key=>{{let started=false;return rows.map((row,i)=>{{if(!n(row[key]))return '';const cmd=started?'L':'M';started=true;return `${{cmd}}${{x(i).toFixed(1)}},${{y(row[key]).toFixed(1)}}`}}).join(' ')}};let parts=[`<title>${{esc(e.ticker)}} price history</title><desc>Daily close with optional EMA20 and EMA50, exact hover values, range high and low, and positioning reference lines keyed above the chart.</desc><rect class="me-frame" data-chart-frame x="${{m.l}}" y="${{m.t}}" width="${{pw}}" height="${{ph}}"/>`];for(let i=0;i<5;i++){{const value=lo+(hi-lo)*i/4,yy=y(value);parts.push(`<line class="me-grid-line" x1="${{m.l}}" x2="${{w-m.r}}" y1="${{yy}}" y2="${{yy}}"/><text x="${{m.l-7}}" y="${{yy+4}}" text-anchor="end">${{money(value)}}</text>`)}}const tickIndexes=[0,Math.round((rows.length-1)/3),Math.round((rows.length-1)*2/3),rows.length-1].filter((v,i,a)=>a.indexOf(v)===i);tickIndexes.forEach((idx,j)=>{{const anchor=j===0?'start':j===tickIndexes.length-1?'end':'middle';parts.push(`<text x="${{x(idx)}}" y="${{h-18}}" text-anchor="${{anchor}}">${{esc(dateLabel(rows[idx].d).replace(', '+new Date(rows[idx].d+'T12:00:00Z').getUTCFullYear(),''))}}</text>`)}});parts.push(`<text class="me-axis-title" data-axis="y" x="12" y="14">Price ($)</text><text class="me-axis-title" data-axis="x" x="${{w-m.r}}" y="${{h-4}}" text-anchor="end">Session date</text>`);const refs=[e.signals.putWall,e.signals.gammaFlip,e.signals.gammaMagnet,e.signals.callWall].filter(value=>n(value)&&value>=lo&&value<=hi);refs.forEach(value=>{{const yy=y(value);parts.push(`<line class="me-ref-level" x1="${{m.l}}" x2="${{w-m.r}}" y1="${{yy}}" y2="${{yy}}"/>`)}});if(visible.c)parts.push(`<path class="me-close" d="${{pathFor('c')}}"/>`);if(visible.e20)parts.push(`<path class="me-ema20" d="${{pathFor('e20')}}"/>`);if(visible.e50)parts.push(`<path class="me-ema50" d="${{pathFor('e50')}}"/>`);const closes=rows.map((row,i)=>({{row,i}})).filter(item=>n(item.row.c)),high=closes.reduce((a,b)=>a.row.c>b.row.c?a:b),low=closes.reduce((a,b)=>a.row.c<b.row.c?a:b);[[high,'High'],[low,'Low']].forEach(([item,label])=>{{const xx=x(item.i),yy=y(item.row.c),anchor=xx>w*.72?'end':'start',dx=anchor==='end'?-6:6,dy=label==='High'?-8:15;parts.push(`<circle cx="${{xx}}" cy="${{yy}}" r="3" fill="var(--me-series-1)"/><text x="${{xx+dx}}" y="${{yy+dy}}" text-anchor="${{anchor}}">${{label}} ${{money(item.row.c)}} · ${{esc(dateLabel(item.row.d).replace(', '+new Date(item.row.d+'T12:00:00Z').getUTCFullYear(),''))}}</text>`)}});parts.push(`<g id="me-hover" style="display:none"><line class="me-guide" id="me-guide" y1="${{m.t}}" y2="${{h-m.b}}"/><circle class="me-marker" id="me-marker-c" r="4" fill="var(--me-series-1)"/><circle class="me-marker" id="me-marker-e20" r="4" fill="var(--me-series-2)"/><circle class="me-marker" id="me-marker-e50" r="4" fill="var(--me-series-3)"/></g><rect class="me-hit" data-chart-hit data-chart-hover-overlay="cross-series" x="${{m.l}}" y="${{m.t}}" width="${{pw}}" height="${{ph}}"/>`);svg.innerHTML=parts.join('');const overlay=svg.querySelector('[data-chart-hit]'),hover=svg.querySelector('#me-hover'),guide=svg.querySelector('#me-guide');const inspect=index=>{{const idx=Math.max(0,Math.min(rows.length-1,index)),row=rows[idx],xx=x(idx);hover.style.display='';guide.setAttribute('x1',xx);guide.setAttribute('x2',xx);[['c','me-marker-c'],['e20','me-marker-e20'],['e50','me-marker-e50']].forEach(([key,id])=>{{const marker=svg.querySelector('#'+id);if(visible[key]&&n(row[key])){{marker.style.display='';marker.setAttribute('cx',xx);marker.setAttribute('cy',y(row[key]))}}else marker.style.display='none'}});showQuote(row,rows[idx-1]);tip.innerHTML=`<b>${{esc(dateLabel(row.d))}}</b><br>Close ${{money(row.c)}}<br>${{visible.e20?'EMA20 '+money(row.e20)+'<br>':''}}${{visible.e50?'EMA50 '+money(row.e50):''}}`;tip.style.display='block';tip.style.left=Math.max(4,Math.min(w-170,xx+10))+'px';tip.style.top=Math.max(4,y(row.c)-62)+'px';return idx}};overlay.addEventListener('pointermove',event=>{{const rect=svg.getBoundingClientRect(),local=(event.clientX-rect.left)*(w/rect.width),idx=Math.round((local-m.l)/pw*(rows.length-1));if(lockedIndex===null)inspect(idx)}});overlay.addEventListener('pointerdown',event=>{{const rect=svg.getBoundingClientRect(),local=(event.clientX-rect.left)*(w/rect.width),idx=Math.round((local-m.l)/pw*(rows.length-1));lockedIndex=lockedIndex===Math.max(0,Math.min(rows.length-1,idx))?null:inspect(idx);if(lockedIndex===null){{hover.style.display='none';tip.style.display='none';showQuote(rows.at(-1),rows.at(-2))}}}});overlay.addEventListener('pointerleave',()=>{{if(lockedIndex===null){{hover.style.display='none';tip.style.display='none';showQuote(rows.at(-1),rows.at(-2))}}}});if(lockedIndex!==null)inspect(Math.min(lockedIndex,rows.length-1));else showQuote(rows.at(-1),rows.at(-2));lastWidth=w}}
function drawPredictionChart(){{
 const e=DATA.entries[selected],rows=currentRows(),future=e.edge.forecast.path.filter(p=>n(p.center)&&n(p.low)&&n(p.high)),v4=e.edge.forecastV4,v4Future=v4.path.filter(p=>n(p.center)&&n(p.p10)&&n(p.p25)&&n(p.p75)&&n(p.p90)),ivMove=e.whale.volatility.move30,svg=byId('me-chart'),wrap=byId('me-chart-wrap'),tip=byId('me-tooltip');tip.style.display='none';
 byId('me-levels').innerHTML=`<span><b>Forecast read</b></span><span>Gold + blue fan: frozen V4 volatility-scaled paths</span><span>Purple: V3 baseline center</span><span>Gray: option-priced move, no direction</span><span>Green/red dashed: live observation</span><span>1W = session 5 · 20D = session 20</span>`;
 if(!rows.length){{svg.innerHTML='<text x="16" y="28">No valid chart data.</text>';showQuote(null);return}}
 const horizon=Math.max(future.length,v4Future.length),w=Math.max(320,Math.round(wrap.getBoundingClientRect().width)),h=360,m={{l:58,r:w<470?84:92,t:25,b:42}},pw=w-m.l-m.r,ph=h-m.t-m.b,actualEnd=rows.length-1,total=Math.max(2,rows.length+horizon);svg.setAttribute('viewBox',`0 0 ${{w}} ${{h}}`);
 const spot=rows.at(-1).c,liveSpot=DATA.refresh?.mode==='INTRADAY'?e.intraday?.observedPrice:null,ivRows=Array.from({{length:horizon}},(_,index)=>{{const scale=Math.sqrt((index+1)/21),move=n(ivMove)?ivMove*scale:null;return {{low:n(move)?spot*(1-move):null,high:n(move)?spot*(1+move):null}}}}),challengerRows=(e.edge.challengers?.models||[]).filter(row=>n(row.center)&&n(row.horizon)&&row.horizon<=horizon),vals=rows.map(row=>row.c).filter(n);if(forecastVisible.v4)v4Future.forEach(row=>vals.push(row.p10,row.p90));if(forecastVisible.v3)future.forEach(row=>vals.push(row.center));if(forecastVisible.iv)ivRows.forEach(row=>{{if(n(row.low))vals.push(row.low,row.high)}});if(forecastVisible.challengers)challengerRows.forEach(row=>vals.push(spot*(1+row.center)));if(n(liveSpot))vals.push(liveSpot);if(!vals.length)vals.push(spot);
 let lo=Math.min(...vals),hi=Math.max(...vals),pad=(hi-lo||Math.abs(hi)||1)*.08;lo-=pad;hi+=pad;const x=i=>m.l+pw*(i/Math.max(1,total-1)),y=v=>m.t+ph*(1-(v-lo)/(hi-lo));
 const pathFor=key=>{{let started=false;return rows.map((row,i)=>{{if(!n(row[key]))return '';const cmd=started?'L':'M';started=true;return `${{cmd}}${{x(i).toFixed(1)}},${{y(row[key]).toFixed(1)}}`}}).join(' ')}};
 let parts=[`<title>${{esc(e.ticker)}} V4 shadow forecast</title><desc>Daily close followed by volatility-scaled matched paths at p10, p25, center, p75, and p90; the V3 center; and a nondirectional option-implied movement envelope.</desc><rect class="me-frame" data-chart-frame x="${{m.l}}" y="${{m.t}}" width="${{pw}}" height="${{ph}}"/>`];
 for(let i=0;i<5;i++){{const value=lo+(hi-lo)*i/4,yy=y(value);parts.push(`<line class="me-grid-line" x1="${{m.l}}" x2="${{w-m.r}}" y1="${{yy}}" y2="${{yy}}"/><text x="${{m.l-7}}" y="${{yy+4}}" text-anchor="end">${{money(value)}}</text>`)}}
 const labelRows=[...rows.map((row,i)=>({{d:row.d,i}})),...(v4Future.length?v4Future:future).map((row,i)=>({{d:row.date,i:rows.length+i}}))],tickIndexes=[0,Math.round((total-1)/3),Math.round((total-1)*2/3),total-1].filter((v,i,a)=>a.indexOf(v)===i);tickIndexes.forEach((idx,j)=>{{const item=labelRows[Math.min(idx,labelRows.length-1)],anchor=j===0?'start':j===tickIndexes.length-1?'end':'middle';parts.push(`<text x="${{x(idx)}}" y="${{h-18}}" text-anchor="${{anchor}}">${{esc(dateLabel(item.d).replace(', '+new Date(item.d+'T12:00:00Z').getUTCFullYear(),''))}}</text>`)}});
 parts.push(`<text class="me-axis-title" data-axis="y" x="12" y="14">Price ($)</text><text class="me-axis-title" data-axis="x" x="${{w-m.r}}" y="${{h-4}}" text-anchor="end">Session date</text>`);
 const refs=[e.signals.putWall,e.signals.gammaFlip,e.signals.gammaMagnet,e.signals.callWall].filter(value=>n(value)&&value>=lo&&value<=hi);refs.forEach(value=>{{const yy=y(value);parts.push(`<line class="me-ref-level" x1="${{m.l}}" x2="${{w-m.r}}" y1="${{yy}}" y2="${{yy}}"/>`)}});if(n(liveSpot)){{const yy=y(liveSpot),liveClass=n(e.intraday?.changeFromAnchor)&&e.intraday.changeFromAnchor<0?'me-live-spot me-live-down':'me-live-spot';parts.push(`<line class="${{liveClass}}" x1="${{x(actualEnd)}}" x2="${{w-m.r}}" y1="${{yy}}" y2="${{yy}}"/><circle class="${{liveClass}}" cx="${{x(actualEnd)}}" cy="${{yy}}" r="4"/><text class="me-live-label" x="${{Math.min(w-m.r-4,x(actualEnd)+7)}}" y="${{Math.max(m.t+12,yy-6)}}">Live ${{money(liveSpot)}}</text>`)}}
 parts.push(`<path class="me-close" d="${{pathFor('c')}}"/>`);
 if(horizon){{
  const v4Path=[{{date:rows.at(-1).d,center:spot,p10:spot,p25:spot,p50:spot,p75:spot,p90:spot}},...v4Future],v3Path=[{{date:rows.at(-1).d,center:spot}},...future],iv=[{{low:spot,high:spot}},...ivRows.filter(row=>n(row.low)&&n(row.high))],fx=j=>x(actualEnd+j),path=values=>values.map((p,j)=>`${{j?'L':'M'}}${{fx(j).toFixed(1)}},${{y(p).toFixed(1)}}`).join(' '),band=(values,highKey,lowKey)=>[...values.map((p,j)=>`${{j?'L':'M'}}${{fx(j).toFixed(1)}},${{y(p[highKey]).toFixed(1)}}`),...values.slice().reverse().map((p,j)=>`L${{fx(values.length-1-j).toFixed(1)}},${{y(p[lowKey]).toFixed(1)}}`),'Z'].join(' '),ivBand=band(iv,'high','low'),end=v4Path.at(-1),endX=fx(Math.max(v4Path.length,v3Path.length)-1);
  if(forecastVisible.iv&&iv.length>1)parts.push(`<path class="me-iv-band" d="${{ivBand}}"/><path class="me-iv-edge" d="${{path(iv.map(p=>p.high))}}"/><path class="me-iv-edge" d="${{path(iv.map(p=>p.low))}}"/>`);
  if(forecastVisible.v4&&v4Path.length>1)parts.push(`<path class="me-v4-band-outer" d="${{band(v4Path,'p90','p10')}}"/><path class="me-v4-band-inner" d="${{band(v4Path,'p75','p25')}}"/><path class="me-v4-center" d="${{path(v4Path.map(p=>p.center))}}"/>`);
  if(forecastVisible.v3&&v3Path.length>1)parts.push(`<path class="me-v3-center" d="${{path(v3Path.map(p=>p.center))}}"/>`);
  if(forecastVisible.challengers)challengerRows.forEach(row=>{{const endpoint=spot*(1+row.center),xx=fx(row.horizon);parts.push(`<path class="me-challenger-center" d="M${{fx(0).toFixed(1)}},${{y(spot).toFixed(1)}} L${{xx.toFixed(1)}},${{y(endpoint).toFixed(1)}}"/><circle class="me-challenger-dot" cx="${{xx.toFixed(1)}}" cy="${{y(endpoint).toFixed(1)}}" r="3"><title>${{esc(row.version)}} · ${{row.horizon.toFixed(0)}} sessions · ${{pct(row.center*100)}}</title></circle>`)}});
  parts.push(`<line class="me-today" x1="${{x(actualEnd)}}" x2="${{x(actualEnd)}}" y1="${{m.t}}" y2="${{h-m.b}}"/><text x="${{Math.min(w-m.r-4,x(actualEnd)+6)}}" y="${{m.t+14}}">Today</text>`);
  const labels=forecastVisible.v4&&v4Path.length>1?[['V4 p90',end.p90],['V4 center',end.center],['V4 p10',end.p10]].sort((a,b)=>b[1]-a[1]):forecastVisible.v3&&v3Path.length>1?[['V3 center',v3Path.at(-1).center]]:[],labelYs=labels.map(([,value])=>y(value));for(let i=1;i<labelYs.length;i++)labelYs[i]=Math.max(labelYs[i],labelYs[i-1]+14);const overflow=Math.max(0,(labelYs.at(-1)||0)-(h-m.b-4));for(let i=0;i<labelYs.length;i++)labelYs[i]-=overflow;labels.forEach(([label,value],index)=>parts.push(`<line class="me-end-connector" x1="${{endX}}" x2="${{endX+5}}" y1="${{y(value)}}" y2="${{labelYs[index]}}"/><text class="me-end-label ${{label==='V4 center'?'me-end-center':''}}" x="${{endX+8}}" y="${{labelYs[index]+4}}">${{label}} ${{money(value)}}</text>`))
 }}
 parts.push(`<g id="me-hover" style="display:none"><line class="me-guide" id="me-guide" y1="${{m.t}}" y2="${{h-m.b}}"/><circle class="me-marker" id="me-marker-c" r="4" fill="var(--me-series-1)"/><circle class="me-marker" id="me-marker-forecast" r="4" fill="var(--me-series-4)"/></g><rect class="me-hit" data-chart-hit data-chart-hover-overlay="cross-series" x="${{m.l}}" y="${{m.t}}" width="${{pw}}" height="${{ph}}"/>`);svg.innerHTML=parts.join('');
 const interactionFuture=v4Future.length?v4Future:future,combined=[...rows.map((row,i)=>({{kind:'actual',index:i,...row}})),...interactionFuture.map((row,i)=>({{kind:'forecast',index:rows.length+i,sessionIndex:i,...row}}))],overlay=svg.querySelector('[data-chart-hit]'),hover=svg.querySelector('#me-hover'),guide=svg.querySelector('#me-guide');
 const inspect=index=>{{const idx=Math.max(0,Math.min(combined.length-1,index)),row=combined[idx],xx=x(idx),cm=svg.querySelector('#me-marker-c'),fm=svg.querySelector('#me-marker-forecast');hover.style.display='';guide.setAttribute('x1',xx);guide.setAttribute('x2',xx);if(row.kind==='forecast'){{const v4Row=v4Future[row.sessionIndex],v3Row=future[row.sessionIndex],iv=ivRows[row.sessionIndex],center=v4Row?.center??v3Row?.center;cm.style.display='none';fm.style.display='';fm.setAttribute('cx',xx);fm.setAttribute('cy',y(center));byId('me-quote').innerHTML=`<span><b>${{esc(dateLabel(row.date))}} · +${{row.session||row.sessionIndex+1}} sessions</b></span><span>V4 center <b>${{money(v4Row?.center)}}</b></span><span>V4 p25–p75 <b>${{money(v4Row?.p25)}}–${{money(v4Row?.p75)}}</b></span><span>V4 p10–p90 <b>${{money(v4Row?.p10)}}–${{money(v4Row?.p90)}}</b></span><span>V3 center <b>${{money(v3Row?.center)}}</b></span><span>IV envelope <b>${{money(iv?.low)}}–${{money(iv?.high)}}</b></span>`;tip.innerHTML=`<b>+${{row.session||row.sessionIndex+1}} sessions · ${{esc(dateLabel(row.date))}}</b><br>V4 ${{money(v4Row?.center)}}<br>middle 50% ${{money(v4Row?.p25)}}–${{money(v4Row?.p75)}}<br>outer range ${{money(v4Row?.p10)}}–${{money(v4Row?.p90)}}<br>V3 ${{money(v3Row?.center)}}`;tip.style.top=Math.max(4,y(center)-92)+'px'}else{{fm.style.display='none';cm.style.display='';cm.setAttribute('cx',xx);cm.setAttribute('cy',y(row.c));showQuote(row,rows[row.index-1]);tip.innerHTML=`<b>${{esc(dateLabel(row.d))}}</b><br>Close ${{money(row.c)}}`;tip.style.top=Math.max(4,y(row.c)-48)+'px'}}tip.style.display='block';tip.style.left=Math.max(4,Math.min(w-220,xx+10))+'px';return idx}};
 overlay.addEventListener('pointermove',event=>{{const rect=svg.getBoundingClientRect(),local=(event.clientX-rect.left)*(w/rect.width),idx=Math.round((local-m.l)/pw*(total-1));if(lockedIndex===null)inspect(idx)}});overlay.addEventListener('pointerdown',event=>{{const rect=svg.getBoundingClientRect(),local=(event.clientX-rect.left)*(w/rect.width),idx=Math.round((local-m.l)/pw*(total-1));lockedIndex=lockedIndex===Math.max(0,Math.min(combined.length-1,idx))?null:inspect(idx);if(lockedIndex===null){{hover.style.display='none';tip.style.display='none';showForecastSummary(e,future)}}}});overlay.addEventListener('pointerleave',()=>{{if(lockedIndex===null){{hover.style.display='none';tip.style.display='none';showForecastSummary(e,future)}}}});if(lockedIndex!==null)inspect(Math.min(lockedIndex,combined.length-1));else showForecastSummary(e,future);lastWidth=w;
}}
function drawOutcomeChart(){{
 const e=DATA.entries[selected],a=e.edge.analogs,rows=a.path.filter(row=>n(row.session)&&n(row.median)&&n(row.p10)&&n(row.p90)),svg=byId('me-chart'),wrap=byId('me-chart-wrap'),tip=byId('me-tooltip');tip.style.display='none';
 byId('me-levels').innerHTML='<span><b>Historical-state path</b></span><span>Empirical p10 / median / p90 from matched states · observed outcomes, not probabilities</span>';
 if(!rows.length){{svg.innerHTML='<text x="16" y="28">Comparable-state path data are unavailable.</text>';byId('me-quote').innerHTML='<span>Historical-state outcomes unavailable.</span>';return}}
 const w=Math.max(320,Math.round(wrap.getBoundingClientRect().width)),h=360,m={{l:58,r:w<470?70:94,t:25,b:42}},pw=w-m.l-m.r,ph=h-m.t-m.b,values=[0,...rows.flatMap(row=>[row.p10,row.median,row.p90])];if(n(a.baseline.median))values.push(a.baseline.median);let lo=Math.min(...values),hi=Math.max(...values),pad=(hi-lo||.02)*.10;lo-=pad;hi+=pad;const x=session=>m.l+pw*((session-1)/Math.max(1,rows.length-1)),y=value=>m.t+ph*(1-(value-lo)/(hi-lo));svg.setAttribute('viewBox',`0 0 ${{w}} ${{h}}`);
 const band=[...rows.map((row,index)=>`${{index?'L':'M'}}${{x(row.session).toFixed(1)}},${{y(row.p90).toFixed(1)}}`),...rows.slice().reverse().map(row=>`L${{x(row.session).toFixed(1)}},${{y(row.p10).toFixed(1)}}`),'Z'].join(' '),medianPath=rows.map((row,index)=>`${{index?'L':'M'}}${{x(row.session).toFixed(1)}},${{y(row.median).toFixed(1)}}`).join(' ');let parts=[`<title>${{esc(e.ticker)}} comparable historical-state outcomes</title><desc>Session-by-session empirical p10, median, and p90 returns after the nearest embargoed historical states. Frequencies are observations, not probabilities.</desc><rect class="me-frame" data-chart-frame x="${{m.l}}" y="${{m.t}}" width="${{pw}}" height="${{ph}}"/>`];
 for(let i=0;i<5;i++){{const value=lo+(hi-lo)*i/4,yy=y(value);parts.push(`<line class="me-grid-line" x1="${{m.l}}" x2="${{w-m.r}}" y1="${{yy}}" y2="${{yy}}"/><text x="${{m.l-7}}" y="${{yy+4}}" text-anchor="end">${{pct(value*100)}}</text>`)}}if(lo<=0&&hi>=0)parts.push(`<line class="me-zero" x1="${{m.l}}" x2="${{w-m.r}}" y1="${{y(0)}}" y2="${{y(0)}}"/>`);if(n(a.baseline.median))parts.push(`<line class="me-baseline" x1="${{m.l}}" x2="${{w-m.r}}" y1="${{y(a.baseline.median)}}" y2="${{y(a.baseline.median)}}"/><text x="${{m.l+5}}" y="${{y(a.baseline.median)-5}}">Base-rate median ${{ret(a.baseline.median)}}</text>`);parts.push(`<path class="me-analog-band" d="${{band}}"/><path class="me-analog" d="${{medianPath}}"/>`);
 [1,5,10,20].forEach((session,index)=>{{const anchor=index===0?'start':index===3?'end':'middle';parts.push(`<text x="${{x(session)}}" y="${{h-18}}" text-anchor="${{anchor}}">${{session}}D</text>`)}});parts.push(`<text class="me-axis-title" data-axis="y" x="12" y="14">Forward return</text><text class="me-axis-title" data-axis="x" x="${{w-m.r}}" y="${{h-4}}" text-anchor="end">Sessions after matched state</text>`);
 const end=rows.at(-1),endX=x(end.session),labels=[['p90',end.p90],['Median',end.median],['p10',end.p10]].sort((left,right)=>right[1]-left[1]),labelYs=labels.map(([,value])=>y(value));for(let i=1;i<labelYs.length;i++)labelYs[i]=Math.max(labelYs[i],labelYs[i-1]+14);const overflow=Math.max(0,labelYs.at(-1)-(h-m.b-4));for(let i=0;i<labelYs.length;i++)labelYs[i]-=overflow;labels.forEach(([label,value],index)=>parts.push(`<line class="me-ref" x1="${{endX}}" x2="${{endX+5}}" y1="${{y(value)}}" y2="${{labelYs[index]}}"/><text class="me-end-label" x="${{endX+8}}" y="${{labelYs[index]+4}}">${{label}} ${{ret(value)}}</text>`));
 parts.push(`<g id="me-hover" style="display:none"><line class="me-guide" id="me-guide" y1="${{m.t}}" y2="${{h-m.b}}"/><circle class="me-marker" id="me-marker-forecast" r="4" fill="var(--me-series-1)"/></g><rect class="me-hit" data-chart-hit data-chart-hover-overlay="cross-series" x="${{m.l}}" y="${{m.t}}" width="${{pw}}" height="${{ph}}"/>`);svg.innerHTML=parts.join('');const overlay=svg.querySelector('[data-chart-hit]'),hover=svg.querySelector('#me-hover'),guide=svg.querySelector('#me-guide'),marker=svg.querySelector('#me-marker-forecast');
 const inspect=index=>{{const idx=Math.max(0,Math.min(rows.length-1,index)),row=rows[idx],xx=x(row.session);hover.style.display='';guide.setAttribute('x1',xx);guide.setAttribute('x2',xx);marker.setAttribute('cx',xx);marker.setAttribute('cy',y(row.median));byId('me-quote').innerHTML=`<span><b>Comparable state +${{row.session}} sessions</b></span><span>Observed up frequency <b>${{n(row.upRate)?pct(row.upRate*100):'—'}}</b></span><span>Median <b>${{ret(row.median)}}</b></span><span>p10–p90 <b>${{ret(row.p10)}} to ${{ret(row.p90)}}</b></span><span class="me-muted">n=${{n(row.sample)?row.sample.toFixed(0):'—'}} · descriptive</span>`;tip.innerHTML=`<b>+${{row.session}} sessions</b><br>Median ${{ret(row.median)}}<br>p10–p90 ${{ret(row.p10)}} to ${{ret(row.p90)}}<br>Observed up ${{n(row.upRate)?pct(row.upRate*100):'—'}}`;tip.style.display='block';tip.style.left=Math.max(4,Math.min(w-210,xx+10))+'px';tip.style.top=Math.max(4,y(row.median)-76)+'px';return idx}};
 overlay.addEventListener('pointermove',event=>{{const rect=svg.getBoundingClientRect(),local=(event.clientX-rect.left)*(w/rect.width),idx=Math.round((local-m.l)/pw*(rows.length-1));if(lockedIndex===null)inspect(idx)}});overlay.addEventListener('pointerdown',event=>{{const rect=svg.getBoundingClientRect(),local=(event.clientX-rect.left)*(w/rect.width),idx=Math.round((local-m.l)/pw*(rows.length-1));lockedIndex=lockedIndex===Math.max(0,Math.min(rows.length-1,idx))?null:inspect(idx);if(lockedIndex===null){{inspect(rows.length-1);hover.style.display='none';tip.style.display='none'}}}});overlay.addEventListener('pointerleave',()=>{{if(lockedIndex===null){{inspect(rows.length-1);hover.style.display='none';tip.style.display='none'}}}});inspect(lockedIndex===null?rows.length-1:Math.min(lockedIndex,rows.length-1));if(lockedIndex===null){{hover.style.display='none';tip.style.display='none'}}lastWidth=w;
}}
function drawActiveChart(){{if(chartView==='OUTCOMES')drawOutcomeChart();else if(chartView==='FORECAST')drawPredictionChart();else drawChart()}}
function edgeBlock(label,value,detail,risk=false){{const width=n(value)?Math.max(0,Math.min(100,value)):0,fill=risk?width>=50?'me-score-risk':'me-score-caution':width>=65?'me-score-good':width<40?'me-score-low':'';return `<div class="me-edge-block"><div class="me-edge-label">${{esc(label)}}</div><div class="me-edge-value">${{n(value)?value.toFixed(0)+' / 100':'—'}}</div><div class="me-score-track" role="progressbar" aria-label="${{esc(label)}}" aria-valuenow="${{width}}" aria-valuemin="0" aria-valuemax="100"><span class="me-score-fill ${{fill}}" style="width:${{width}}%"></span></div><div class="me-muted">${{esc(detail)}}</div></div>`}}
function renderEdge(e){{const d=e.edge.dimensions,x=e.edge,g=x.gex,f=x.flow,oi=x.oi,er=x.earnings,an=x.analogs.horizons['20']||{{}},dimensions=[['Directional edge',d.directional],['Long-vol fit',d.volatility],['Positioning',d.positioning],['Tradeability',d.tradeability],['Evidence quality',d.evidenceQuality]].filter(([,value])=>n(value)),bottleneck=dimensions.length?dimensions.reduce((low,row)=>row[1]<low[1]?row:low):['Unavailable',null],depth=[['Price',e.bars.length],['Flow',n(f.historySessions)?f.historySessions:0],['IV surface',n(x.surface.historySessions)?x.surface.historySessions:0],['GEX same-method',n(g.comparableHistorySessions)?g.comparableHistorySessions:0]],maxDepth=Math.max(1,...depth.map(([,value])=>value)),depthHtml=depth.map(([label,value])=>`<div class="me-depth-item"><div class="me-depth-label"><span>${{esc(label)}}</span><b>${{value.toFixed(0)}} sessions</b></div><div class="me-depth-track" role="img" aria-label="${{esc(label)}} ${{value.toFixed(0)}} sessions"><span class="me-depth-fill" style="width:${{value/maxDepth*100}}%"></span></div></div>`).join(''),blocks=[edgeBlock('Directional edge',d.directional,`${{trend(e)}} · 20D return ${{ret(e.technical.return20)}} · matched median ${{ret(an.median)}}`),edgeBlock('Long-vol fit',d.volatility,`IV rank ${{n(x.surface.ivPercentile)?pct(x.surface.ivPercentile*100):'—'}} · IV–RV ${{n(x.surface.ivRvGap)?pct(x.surface.ivRvGap*100):'—'}}`),edgeBlock('Positioning context',d.positioning,`${{esc(g.regime)}} · flow quality ${{n(f.quality)?pct(f.quality*100):'—'}} · OI confirmation ${{f.oiConfirmed?'yes':'no'}}`),edgeBlock('Tradeability',d.tradeability,`median spread ${{n(x.surface.medianSpread)?pct(x.surface.medianSpread*100):'—'}} · matched OI ${{n(oi.matchedContracts)?oi.matchedContracts.toLocaleString():'—'}}`),edgeBlock('Catalyst risk',d.catalystRisk,`${{n(er.eventCount)?er.eventCount.toFixed(0):'0'}} prior events · ${{n(x.news.major)?x.news.major.toFixed(0):'0'}} major headlines`,true),edgeBlock('Evidence quality',d.evidenceQuality,`${{n(x.analogs.sample)?x.analogs.sample.toFixed(0):'0'}} independent matches · stability ${{esc(x.analogs.stability.status)}}`)].join('');byId('me-edge').innerHTML=`<div class="me-engine-head"><div class="me-engine-title"><b>Evidence quality</b><span>Weakest input: ${{esc(bottleneck[0])}} ${{n(bottleneck[1])?bottleneck[1].toFixed(0)+'/100':'—'}}</span></div><div class="me-depth-grid">${{depthHtml}}</div></div><div class="me-edge-grid">${{blocks}}</div>`}}
function signalAgreement(e){{const dir=e.thesis.direction==='BULLISH'?1:e.thesis.direction==='BEARISH'?-1:0,signals=[];const trendSign=trend(e).startsWith('Above')?1:trend(e).startsWith('Below')?-1:0;signals.push(['Price trend',trendSign]);signals.push(['Matched states',e.edge.analogs.disposition==='BULLISH'?1:e.edge.analogs.disposition==='BEARISH'?-1:0]);signals.push(['Option flow',n(e.edge.flow.directionalPremium)?Math.sign(e.edge.flow.directionalPremium):0]);signals.push(['GEX regime',e.edge.gex.regime==='ABOVE_FLIP'?1:e.edge.gex.regime==='BELOW_FLIP'?-1:0]);const news=newsImpact(e).direction;signals.push(['News',news==='POSITIVE'?1:news==='NEGATIVE'?-1:0]);const mapped=signals.map(([label,value])=>({{label,value,state:value&&value===dir?'aligned':value?'conflict':'neutral'}})),aligned=mapped.filter(row=>row.state==='aligned').length,conflicting=mapped.filter(row=>row.state==='conflict').length,neutral=mapped.length-aligned-conflicting;return {{aligned,conflicting,neutral,signals:mapped}}}}
function signalMap(a){{const rows=a.signals.map(row=>{{const left=row.state==='aligned'?84:row.state==='conflict'?16:50,cls=row.state==='aligned'?'me-align':row.state==='conflict'?'me-conflict':'',toneClass=row.state==='aligned'?'me-pos':row.state==='conflict'?'me-neg':'me-muted',state=row.state==='aligned'?'supports':row.state==='conflict'?'conflicts':'neutral';return `<div class="me-signal-row"><span>${{esc(row.label)}}</span><span class="me-signal-rail" role="img" aria-label="${{esc(row.label)}} ${{state}} the thesis"><i class="me-signal-dot ${{cls}}" style="left:${{left}}%"></i></span><span class="me-signal-state ${{toneClass}}">${{state}}</span></div>`}}).join('');return `<div class="me-signal-map"><div class="me-signal-map-head"><b>Evidence alignment · ${{a.aligned}} supports / ${{a.conflicting}} conflicts / ${{a.neutral}} neutral</b><span>conflicts ← current-state context → supports</span></div>${{rows}}</div>`}}
function trackerRead(e){{const row=DATA.evaluation.tickers?.[e.ticker],count=row?.prospectiveDirectionCount,accuracy=row?.prospectiveAccuracy,baseline=row?.prospectiveBaseline,pending=row?.pending,lift=row?.prospectiveLift;if(!n(count)||count===0||!n(accuracy))return {{value:'Awaiting outcomes',detail:`${{n(pending)?pending.toFixed(0):'0'}} pending`,tone:''}};return {{value:`${{Math.round(accuracy*count)}} / ${{count.toFixed(0)}} correct`,detail:`baseline ${{n(baseline)?Math.round(baseline*count)+' / '+count.toFixed(0):'—'}} · ${{n(pending)?pending.toFixed(0):'0'}} pending`,tone:n(lift)?tone(lift):''}}}}
function renderThesis(e){{const t=e.thesis,o=t.option,a=signalAgreement(e),tracker=trackerRead(e),v4=e.edge.forecastV4,i=e.intraday||{{}},live=DATA.refresh?.mode==='INTRADAY',driver=a.signals.find(x=>x.state==='aligned'),conflict=a.signals.find(x=>x.state==='conflict'),quality=e.edge.dimensions.evidenceQuality,tickerEval=DATA.evaluation.tickers?.[e.ticker],lift=n(tickerEval?.prospectiveLift)?tickerEval.prospectiveLift:null;byId('me-thesis').innerHTML=`<div class="me-thesis-item">Trade direction<b class="${{directionTone(t.direction)}}">${{esc(t.direction)}} · evidence ${{n(t.conviction)?t.conviction.toFixed(0):'—'}}/100</b></div><div class="me-thesis-item">V4 price path · shadow<b>1W ${{n(v4.center5)?ret(v4.center5):'—'}} · 20D ${{n(v4.center20)?ret(v4.center20):'—'}}</b><span class="me-option-quote">20D p10–p90 ${{n(v4.p10)?ret(v4.p10):'—'}} to ${{n(v4.p90)?ret(v4.p90):'—'}}</span></div><div class="me-thesis-item me-option-panel ${{optionTone(o)}}">Option reference<b class="me-option-summary">${{esc(optionIdentity(o))}}</b><span class="me-option-quote">Bid ${{money(o.bid)}} · Ask ${{money(o.ask)}} · B/E ${{money(o.breakeven)}}</span></div><div class="me-thesis-item">Confidence check<b>Data ${{n(quality)?quality.toFixed(0):'—'}}/100 · agreement ${{a.aligned}}–${{a.conflicting}}</b><span class="me-option-quote">demonstrated ticker lift ${{n(lift)?signedPct(lift*100):'not established'}}</span></div>${{live?`<div class="me-intraday-read"><span>Intraday condition</span><b class="${{intradayTone(i.status)}}">${{esc(i.status)}}</b><span>${{n(i.observedPrice)?money(i.observedPrice)+' · '+signedPct((i.changeFromAnchor||0)*100):'price unavailable'}}</span><span>${{esc(i.drivers?.[0]||i.conflicts?.[0]||i.neutral?.[0]||'No qualified directional update.')}}</span></div>`:''}}${{signalMap(a)}}<div class="me-thesis-rules"><strong>Strongest aligned:</strong> ${{esc(driver?.label||'none')}} &nbsp;·&nbsp; <strong>Strongest conflict:</strong> ${{esc(conflict?.label||'none')}}<br><strong>Confirm:</strong> ${{esc(t.trigger)}} &nbsp;·&nbsp; <strong>Fail:</strong> ${{esc(t.invalidation)}}<br><span class="me-muted">V4 is frozen and uncalibrated. The option is a stored research reference, not an order.</span></div>`}}
function newsImpact(e){{const joined=e.headlines.map(row=>row.headline.toLowerCase()).join(' '),latest=e.headlines[0]?.headline||'No current provider headline.',negative=['fell','drop','down','selloff','cut','miss','weak','lower','delay','ban','risk','lawsuit','underperform','decline'],positive=['beat','raise','growth','expand','partnership','agreement','launch','win','upgrade','demand','record','rally','strong'];let score=0;negative.forEach(word=>{{if(joined.includes(word))score--}});positive.forEach(word=>{{if(joined.includes(word))score++}});const symbols=(latest.match(/\\$[A-Z]{{1,6}}/g)||[]).length,scope=symbols>2?'SECTOR / PEER TAPE':'COMPANY-SPECIFIC',direction=score>1?'POSITIVE':score<-1?'NEGATIVE':'MIXED',reaction=n(e.change)?`${{e.change>0?'rose':'fell'}} ${{Math.abs(e.change).toFixed(1)}}% in the latest regular session`:'has no verified session reaction';const mechanism=direction==='NEGATIVE'?'Headlines raise the confirmation bar for a bullish break.':direction==='POSITIVE'?'Headlines support the thesis only if price confirms.':'Headlines have no clear directional read.';return {{direction,scope,latest,reaction,mechanism,score}}}}
function flowAlert(e){{const f=e.edge.flow,w=e.whale.flow,deltaSign=n(w.delta)?Math.sign(w.delta):0,premiumSign=n(f.directionalPremium)?Math.sign(f.directionalPremium):0,agreement=deltaSign&&premiumSign&&deltaSign===premiumSign,disagreement=deltaSign&&premiumSign&&deltaSign!==premiumSign,sign=disagreement?0:(deltaSign||premiumSign),history=n(f.historySessions)?f.historySessions:0,qualified=history>=20&&n(f.percentile)&&f.percentile>=.9,side=sign>0?'POSITIVE':sign<0?'NEGATIVE':'MIXED',label=qualified?`UNUSUAL ${{side}} FLOW`:history>=20?`${{side}} FLOW · NORMAL RANGE`:`${{side}} FLOW · EARLY SAMPLE`,cls=side==='POSITIVE'?'me-flow-positive':side==='NEGATIVE'?'me-flow-hot':'',agreementText=agreement?'Greek delta agrees':disagreement?'Greek delta conflicts':'Greek delta is inconclusive',percentile=n(f.percentile)?`${{Math.round(f.percentile*100)}}th magnitude percentile`:'magnitude rank unavailable',read=`${{signedCompact(f.directionalPremium)}} directional premium · ${{percentile}} · ${{agreementText}}`;return {{label,cls,read,qualified,history}}}}
function flowDateLabel(value){{if(!value)return '—';const date=new Date(value+'T12:00:00Z');return Number.isNaN(date.valueOf())?value:date.toLocaleDateString(undefined,{{month:'short',day:'numeric'}})}}
function flowSeries(rows){{return rows.map((row,index)=>{{const prior=rows.slice(Math.max(0,index-20),index).map(item=>Math.abs(item.premium)).filter(n),magnitude=Math.abs(row.premium),rank=prior.length?prior.filter(value=>value<=magnitude).length/prior.length:null,start=Math.max(0,index-4),window=rows.slice(start,index+1).map(item=>item.premium).filter(n),mean=window.length?window.reduce((sum,value)=>sum+value,0)/window.length:null;return {{...row,rank,mean,outlier:prior.length>=20&&rank>=.9}}}})}}
function drawFlowMini(e){{const host=byId('me-flow-mini'),tip=byId('me-flow-tip');if(!host||!tip)return;const rows=flowSeries((e.edge.flow.history||[]).filter(row=>row.date&&n(row.premium)).slice(-60));host.innerHTML='';tip.style.display='none';if(rows.length<2){{host.innerHTML='<div class="me-empty">Daily flow history is not available.</div>';return}}const width=Math.max(250,Math.round(host.getBoundingClientRect().width||360)),height=126,margin={{top:8,right:3,bottom:20,left:3}},plotW=width-margin.left-margin.right,plotH=height-margin.top-margin.bottom,zero=margin.top+plotH/2,abs=rows.map(row=>Math.abs(row.premium)).sort((a,b)=>a-b),p95=abs[Math.floor((abs.length-1)*.95)]||1,latest=Math.abs(rows.at(-1).premium),limit=Math.max(p95,latest,1),barStep=plotW/rows.length,barWidth=Math.max(1.5,barStep*.68),clamp=value=>Math.max(-limit,Math.min(limit,value)),y=value=>zero-(clamp(value)/limit)*(plotH/2-4),x=index=>margin.left+(index+.5)*barStep,ns='http://www.w3.org/2000/svg',make=(tag,attrs={{}})=>{{const node=document.createElementNS(ns,tag);Object.entries(attrs).forEach(([key,value])=>node.setAttribute(key,value));return node}},svg=make('svg',{{class:'me-flow-mini',viewBox:`0 0 ${{width}} ${{height}}`,role:'img','aria-label':`${{e.ticker}} daily signed directional option premium history`}}),title=make('title'),desc=make('desc');title.textContent=`${{e.ticker}} daily option-flow history`;desc.textContent='Green bars are positive signed directional premium. Red bars are negative. The blue line is the five-session mean. Gold dots mark top-decile magnitude versus the prior twenty sessions.';svg.append(title,desc,make('line',{{x1:margin.left,y1:zero,x2:width-margin.right,y2:zero,class:'me-flow-zero'}}));rows.forEach((row,index)=>{{const barY=y(row.premium),bar=make('rect',{{x:x(index)-barWidth/2,y:Math.min(zero,barY),width:barWidth,height:Math.max(1,Math.abs(zero-barY)),class:row.premium>=0?'me-flow-bar-pos':'me-flow-bar-neg'}});svg.appendChild(bar);if(row.outlier)svg.appendChild(make('circle',{{cx:x(index),cy:barY,r:2.7,class:'me-flow-outlier'}}))}});const points=rows.map((row,index)=>n(row.mean)?`${{x(index)}},${{y(row.mean)}}`:null).filter(Boolean);if(points.length>1)svg.appendChild(make('polyline',{{points:points.join(' '),class:'me-flow-mean'}}));[0,Math.floor((rows.length-1)/2),rows.length-1].forEach((index,labelIndex)=>{{const text=make('text',{{x:x(index),y:height-4,'text-anchor':labelIndex===0?'start':labelIndex===2?'end':'middle'}});text.textContent=flowDateLabel(rows[index].date);svg.appendChild(text)}});const guide=make('line',{{x1:0,y1:margin.top,x2:0,y2:margin.top+plotH,class:'me-flow-guide',visibility:'hidden'}}),hit=make('rect',{{x:margin.left,y:margin.top,width:plotW,height:plotH,class:'me-flow-hit'}});svg.append(guide,hit);let pinned=false;const show=(event,force=false)=>{{const box=svg.getBoundingClientRect(),local=Math.max(0,Math.min(plotW,(event.clientX-box.left)*(width/box.width)-margin.left)),index=Math.max(0,Math.min(rows.length-1,Math.floor(local/barStep))),row=rows[index],left=Math.max(4,Math.min(host.clientWidth-151,(x(index)/width)*host.clientWidth-72));guide.setAttribute('x1',x(index));guide.setAttribute('x2',x(index));guide.setAttribute('visibility','visible');tip.innerHTML=`<b>${{esc(flowDateLabel(row.date))}}</b><br><span class="${{row.premium>=0?'me-pos':'me-neg'}}">${{signedCompact(row.premium)}}</span> directional premium<br>${{n(row.rank)?Math.round(row.rank*100)+'th prior-20 magnitude percentile':'prior history building'}} · ${{n(row.alerts)?row.alerts.toFixed(0):'—'}} alerts<br>5D mean ${{signedCompact(row.mean)}}`;tip.style.left=left+'px';tip.style.top='3px';tip.style.display='block';if(force)pinned=!pinned}};hit.addEventListener('pointermove',event=>{{if(!pinned)show(event)}});hit.addEventListener('pointerdown',event=>{{event.preventDefault();show(event,true)}});hit.addEventListener('pointerleave',()=>{{if(!pinned){{guide.setAttribute('visibility','hidden');tip.style.display='none'}}}});host.append(svg,tip)}}
function forecastRead(e){{const f=e.edge.forecastV4,week=f.path[4],end=f.path.at(-1),move=e.whale.volatility.move30,center=n(end?.center)&&n(e.price)?(end.center/e.price-1)*100:null,weekCenter=n(week?.center)&&n(e.price)?(week.center/e.price-1)*100:null,spread=n(end?.p90)&&n(end?.p10)&&n(e.price)?(end.p90-end.p10)/e.price*100:null,weekSpread=n(week?.p90)&&n(week?.p10)&&n(e.price)?(week.p90-week.p10)/e.price*100:null,flat=n(center)&&Math.abs(center)<3;return {{verdict:flat?'20D CENTER NEAR SPOT':n(center)&&center>0?'20D CENTER LEANS HIGHER':n(center)?'20D CENTER LEANS LOWER':'V4 PATH UNAVAILABLE',center,weekCenter,spread,weekSpread,move:n(move)?move*100:null,read:`1W ${{n(weekCenter)?signedPct(weekCenter):'—'}} with a ${{n(weekSpread)?pct(weekSpread):'—'}} outer range. 20D ${{n(center)?signedPct(center):'—'}} with a ${{n(spread)?pct(spread):'—'}} outer range. Paths are scaled to current realized and implied volatility.`}}}}
function renderIntel(e){{const news=newsImpact(e),flow=flowAlert(e),fc=forecastRead(e),headlineMeta=e.headlines[0]?`${{e.headlines[0].source}} · ${{timeLabel(e.headlines[0].published)}}`:'No provider headline in this capture',flowTone=flow.label.includes('POSITIVE')?'me-pos':flow.label.includes('NEGATIVE')?'me-neg':'';byId('me-intel').innerHTML=`<article class="me-intel-card"><div class="me-intel-head"><span class="me-intel-kicker">Price catalyst</span><button type="button" class="me-intel-action" id="me-news-ask">Stress-test</button></div><div class="me-intel-verdict ${{news.direction==='POSITIVE'?'me-pos':news.direction==='NEGATIVE'?'me-neg':''}}">${{news.direction}} · ${{news.scope}}</div><div class="me-intel-main"><b>${{esc(news.latest)}}</b><br>${{esc(news.mechanism)}} Price ${{esc(news.reaction)}}.</div><div class="me-intel-meta">${{esc(headlineMeta)}} · ${{n(e.edge.news.count)?e.edge.news.count.toFixed(0):e.headlines.length}} headlines · rule-based impact label</div></article><article class="me-intel-card ${{flow.cls}}"><div class="me-intel-head"><span class="me-intel-kicker">Option flow · 60 sessions</span><span class="me-intel-kicker">${{flow.qualified?'outlier':'normal range'}}</span></div><div class="me-intel-verdict ${{flowTone}}">${{esc(flow.label)}} · ${{e.edge.flow.oiConfirmed?'OI confirmed':'OI not confirmed'}}</div><div class="me-flow-summary">${{esc(flow.read)}}</div><div class="me-flow-viz" id="me-flow-mini"><div class="me-flow-tip" id="me-flow-tip" role="tooltip"></div></div><div class="me-flow-legend"><span><i class="me-flow-key" style="--key:var(--me-positive)"></i>positive</span><span><i class="me-flow-key" style="--key:var(--me-negative)"></i>negative</span><span><i class="me-flow-key" style="--key:var(--me-accent)"></i>5D mean</span><span>● prior-20 top decile</span></div><div class="me-intel-meta">${{flow.history}} sessions · select a bar for premium and rank</div></article><article class="me-intel-card"><div class="me-intel-head"><span class="me-intel-kicker">V4 shadow forecast</span><button type="button" class="me-intel-action" id="me-open-forecast">Open chart</button></div><div class="me-intel-verdict ${{tone(fc.center)}}">${{esc(fc.verdict)}} · ${{n(fc.center)?signedPct(fc.center):'—'}}</div><div class="me-intel-main">${{esc(fc.read)}}</div><div class="me-intel-meta">1W center <span class="${{tone(fc.weekCenter)}}">${{n(fc.weekCenter)?signedPct(fc.weekCenter):'—'}}</span> · 20D p10–p90 width ${{n(fc.spread)?pct(fc.spread):'—'}} · 30D IV move ±${{n(fc.move)?pct(fc.move):'—'}} · uncalibrated</div></article>`;drawFlowMini(e);byId('me-open-forecast').addEventListener('click',()=>{{chartView='FORECAST';range='1M';lockedIndex=null;renderControls();drawActiveChart()}});byId('me-news-ask').addEventListener('click',async()=>{{const prompt=`Stress-test how the stored headlines could affect ${{e.ticker}} over 1, 5, and 20 sessions. Use only the displayed evidence and provenance. Separate company news from sector effects. Reconcile price reaction, option flow, volatility, GEX, matched states, and market tide. State the mechanism, strongest counterevidence, confirmation, and invalidation. Do not infer causation or call an observed frequency a probability. Headlines: ${{e.headlines.map(row=>row.headline+' ['+row.source+' '+row.published+']').join(' | ')}}`;if(window.openai?.sendFollowUpMessage)await window.openai.sendFollowUpMessage({{prompt,title:`Stress-test ${{e.ticker}} catalyst`}})}})}}
function whyValue(e,factor){{return e.edge.whyToday.find(row=>row.factor===factor)?.change}}
function dailyDerivativesRead(e){{const x=e.edge,g=x.gex,f=x.flow,oi=x.oi,dp=x.dark;const iv=whyValue(e,'front_iv'),flip=whyValue(e,'gamma_flip'),cw=whyValue(e,'call_wall'),pw=whyValue(e,'put_wall'),dark=whyValue(e,'dark_pool_level'),oiDelta=whyValue(e,'call_minus_put_oi');const priceVerb=n(e.change)?e.change>0?'rose':e.change<0?'fell':'was flat':'was unavailable';const ivText=n(iv)?`front IV ${{iv>0?'rose':'fell'}} ${{Math.abs(iv*100).toFixed(1)}} vol points`:'front-IV change was unavailable';const rangeChange=n(cw)&&n(pw)?cw-pw:null;const comparable=g.comparisonStatus==='COMPARABLE';const rangeText=comparable&&n(rangeChange)?`The mapped wall range ${{rangeChange>0?'widened':'narrowed'}} by ${{money(Math.abs(rangeChange))}} (call ${{signedPrice(cw)}}; put ${{signedPrice(pw)}}).`:comparable?'Wall-range migration was unavailable.':`GEX migration is suppressed across the ${{esc(g.methodBoundary)}} provider-method boundary.`;const flipText=comparable?`The GEX flip moved ${{signedPrice(flip)}}.`:'The GEX flip is shown only as a current-state reference.';const gexText=g.regime==='ABOVE_FLIP'?'spot remains above the GEX flip':g.regime==='BELOW_FLIP'?'spot remains below the GEX flip':'spot is near the GEX flip';const oiText=n(oiDelta)?oiDelta<0?'put-over-call OI delta':oiDelta>0?'call-over-put OI delta':'balanced OI delta':'OI tilt unavailable';const flowText=f.oiConfirmed&&n(f.quality)&&f.quality>=.5?'flow has OI confirmation':'unusual flow is not confirmed';const premiumText=n(iv)&&iv>0?' Rising front IV raises long-premium entry cost.':n(iv)&&iv<0?' Falling front IV reduces long-premium cost but can signal volatility compression.':'';return `Price ${{priceVerb}} ${{n(e.change)?Math.abs(e.change).toFixed(1)+'%':'—'}} while ${{ivText}}. ${{flipText}} ${{rangeText}} The dominant dark-pool level moved ${{signedPrice(dark)}} to ${{money(dp.level)}}; call-minus-put OI changed ${{signedCount(oiDelta)}} contracts. Combined read: ${{gexText}}, with a ${{oiText}}; ${{flowText}}.${{premiumText}} Treat this as positioning context, not a standalone entry signal.`}}
function derivCard(title,verdict,metrics,why,confirm,limit,signal='CAUTIOUS'){{const semantic=['BULLISH','BEARISH','CAUTIOUS'].includes(signal)?signal:'CAUTIOUS',cls=`me-deriv-${{semantic.toLowerCase()}}`;return `<section class="me-deriv-card ${{cls}}" aria-label="${{esc(title)}} · ${{semantic.toLowerCase()}} evidence"><div class="me-deriv-top"><div class="me-deriv-title">${{esc(title)}}</div><span class="me-deriv-signal">${{semantic}}</span></div><div class="me-deriv-verdict">${{esc(verdict)}}</div><div class="me-deriv-metrics">${{metrics.join('')}}</div><div class="me-deriv-read"><span><b>Why it matters:</b> ${{esc(why)}}</span><span><b>Confirmation:</b> ${{esc(confirm)}}</span><span class="me-deriv-limit"><b>Limit:</b> ${{esc(limit)}}</span></div></section>`}}
function renderDerivatives(e){{
 const x=e.edge,g=x.gex,f=x.flow,oi=x.oi,s=e.signals,o=e.thesis.option,w=e.whale,wg=w.gex,wf=w.flow,wv=w.volatility,wd=w.dark,ws=w.short;
 const flipPct=distance(e.price,s.gammaFlip),flipGap=n(flipPct)?Math.abs(flipPct).toFixed(1)+'%':'—',rangeChange=n(g.callWallChange)&&n(g.putWallChange)?g.callWallChange-g.putWallChange:null;
 const gexVerdict=`${{wg.regime==='UNAVAILABLE'?'GEX SURFACE UNAVAILABLE':wg.regime+' NEAR SPOT'}} · flip ${{flipGap}}`;
 const deltaWord=n(wf.delta)?wf.delta>0?'POSITIVE DELTA FLOW':wf.delta<0?'NEGATIVE DELTA FLOW':'FLAT DELTA FLOW':'DELTA FLOW UNAVAILABLE',vegaWord=n(wf.vega)?wf.vega>0?'positive vega':'negative vega':'vega unavailable';
 const flowVerdict=`${{deltaWord}} · ${{vegaWord}}`;
 const volVerdict=n(wv.gap)?`${{wv.gap>0?'IV ABOVE RV':'IV BELOW RV'}} · rank ${{n(wv.rank)?wv.rank.toFixed(0):'—'}}`: 'VOLATILITY STATE UNAVAILABLE';
 const strikePct=distance(e.price,o.strike),wall=o.type==='PUT'?s.putWall:s.callWall,beWall=n(o.breakeven)&&n(wall)&&wall!==0?(o.breakeven/wall-1)*100:null,strikeVerdict=o.status==='CANDIDATE'?'CANDIDATE':'REFERENCE · NOT EXECUTABLE',beDirection=n(o.breakevenMove)?Math.abs(o.breakevenMove*100).toFixed(1)+'% '+(o.breakevenMove<0?'downside':'upside'):'—';
 const darkPct=n(wd.distance)?wd.distance*100:distance(e.price,wd.level),darkVerdict=n(darkPct)&&Math.abs(darkPct)<=.5?'AT DOMINANT SHELF':n(darkPct)&&darkPct>0?'SHELF ABOVE SPOT':n(darkPct)?'SHELF BELOW SPOT':'SHELF UNAVAILABLE';
 const shortVerdict=n(ws.siFloat)?`${{pct(ws.siFloat*100)}} SHORT FLOAT · ${{n(ws.daysCover)?ws.daysCover.toFixed(1):'—'}} DTC`:'SHORT CROWDING UNAVAILABLE';
 const gammaSignal=g.regime==='ABOVE_FLIP'?'BULLISH':g.regime==='BELOW_FLIP'?'BEARISH':'CAUTIOUS';
 const flowSignal=f.oiConfirmed&&n(wf.delta)?wf.delta>0?'BULLISH':wf.delta<0?'BEARISH':'CAUTIOUS':'CAUTIOUS';
 const optionSignal=o.type==='CALL'?'BULLISH':o.type==='PUT'?'BEARISH':'CAUTIOUS';
 const cardSignals=[gammaSignal,flowSignal,'CAUTIOUS',optionSignal,'CAUTIOUS','CAUTIOUS'];
 const signalCount=signal=>cardSignals.filter(value=>value===signal).length;
 const cards=[
  derivCard('Gamma topology',gexVerdict,[`<span>positive shelf <b>${{money(wg.positiveStrike)}}</b></span>`,`<span>negative shelf <b>${{money(wg.negativeStrike)}}</b></span>`,`<span>near GEX <b>${{compactNum(wg.nearNet)}}</b></span>`,`<span>nearby flips <b>${{g.nearbyFlips.length?g.nearbyFlips.map(money).join(' / '):'—'}}</b></span>`],`Above the stored flip is bullish context; below it is bearish context. Positive modeled gamma near spot can coincide with damped movement or pinning. Negative gamma can coincide with larger moves.`,`Look for repeated rejection, acceptance, or compression around the flip and shelves while the mapped levels remain stable.`,`The color describes spot relative to the stored flip, not dealer inventory or hedge direction. Current levels use ${{g.methodVersion==='spot_directionalized_volume_v2'?'spot directionalized volume':'the legacy provider method'}}. Only ${{n(g.comparableHistorySessions)?g.comparableHistorySessions.toFixed(0):'0'}} same-method sessions are comparable.`,gammaSignal),
  derivCard('Greek flow + OI',flowVerdict,[`<span>delta <b>${{compactNum(wf.delta)}}</b></span>`,`<span>vega <b>${{compactNum(wf.vega)}}</b></span>`,`<span>OTM share <b>${{n(wf.otmShare)?pct(wf.otmShare*100):'—'}}</b></span>`,`<span>call−put OI Δ <b>${{signedCount(oi.netChange)}}</b></span>`],`Same-sign delta flow that persists and is followed by OI growth is stronger evidence than premium alone. Positive vega shows demand for volatility exposure, not necessarily higher stock price.`,`Require price acceptance in the same direction and a consecutive-chain OI change at the active strikes; current OI confirmation is ${{f.oiConfirmed?'present':'absent'}}.`,`Without consecutive-chain OI confirmation, the card stays cautious. Spreads, closing trades, and dealer-side transactions can reverse apparent intent.`,flowSignal),
  derivCard('Volatility price',volVerdict,[`<span>IV / RV <b>${{n(wv.iv)?pct(wv.iv*100):'—'}} / ${{n(wv.rv)?pct(wv.rv*100):'—'}}</b></span>`,`<span>IV−RV <b>${{n(wv.gap)?signedPct(wv.gap*100):'—'}}</b></span>`,`<span>30D / 60D <b>${{n(wv.iv30)?pct(wv.iv30*100):'—'}} / ${{n(wv.iv60)?pct(wv.iv60*100):'—'}}</b></span>`,`<span>term slope <b>${{n(wv.slope)?signedPct(wv.slope*100):'—'}}</b></span>`],`IV below recent RV can improve relative long-premium value; IV above RV raises the move hurdle. The term slope shows where the market concentrates event risk.`,`Compare the gap with catalyst timing, skew, liquidity, and whether IV stays low or high across several captures.`,`Realized volatility can mean-revert. IV prices movement and insurance demand, not direction.`),
  derivCard('Option reference',strikeVerdict,[`<span>${{esc(o.type)}} <b>${{money(o.strike)}}</b> (${{signedPct(strikePct)}})</span>`,`<span>expiry <b>${{esc(o.expiry)}} · ${{n(o.dte)?o.dte.toFixed(0):'—'}}D</b></span>`,`<span>bid / ask <b>${{money(o.bid)}} / ${{money(o.ask)}}</b></span>`,`<span>spread / B/E <b>${{n(o.spread)?pct(o.spread*100):'—'}} / ${{money(o.breakeven)}}</b></span>`,`<span>Δ / OI / fit <b>${{n(o.delta)?o.delta.toFixed(2):'—'}} / ${{n(o.oi)?o.oi.toLocaleString():'—'}} / ${{n(o.fit)?o.fit.toFixed(0):'—'}}</b></span>`],`The color identifies the contract's directional expression: calls are bullish and puts are bearish. Breakeven ${{money(o.breakeven)}} requires a ${{beDirection}} move.`,`Reprice from a current NBBO only after the thesis trigger; verify the required move is plausible before expiry and relative to the ${{money(wall)}} wall.`,`Direction color is not an endorsement. Fit is not chance of profit. This stored quote is a paper reference, not an executable order.`,optionSignal),
  derivCard('Dark-pool shelf',darkVerdict,[`<span>level <b>${{money(wd.level)}}</b> (${{signedPct(darkPct)}})</span>`,`<span>dark share <b>${{n(wd.share)?pct(wd.share*100):'—'}}</b></span>`,`<span>dark volume <b>${{compactNum(wd.darkVolume)}}</b></span>`,`<span>reported levels <b>${{n(wd.levels)?wd.levels.toFixed(0):'—'}}</b></span>`],`A repeated off-exchange volume shelf can become a liquidity and price-response zone. Its value comes from subsequent behavior at ${{money(wd.level)}}, not from the print total alone.`,`Watch regular-session volume and closes for rejection, support, or clean acceptance through the shelf; compare with raw-print level ${{money(x.dark.level)}}.`,`The feed does not identify the owner, trade direction, accumulation, or distribution.`),
  derivCard('Short crowding',shortVerdict,[`<span>borrow fee <b>${{n(ws.borrowFee)?pct(ws.borrowFee):'—'}}</b></span>`,`<span>shares available <b>${{compactNum(ws.sharesAvailable)}}</b></span>`,`<span>availability Δ <b>${{compactNum(ws.sharesChange)}}</b></span>`,`<span>short vol / 20D <b>${{n(ws.shortVolumeRatio)?pct(ws.shortVolumeRatio*100):'—'}} / ${{n(ws.shortVolumeMean20)?pct(ws.shortVolumeMean20*100):'—'}}</b></span>`],`High short interest, rising borrow cost, and falling availability can amplify a price break because covering demand becomes more urgent. Low crowding removes that potential accelerant.`,`Require persistent borrow tightening or short-interest crowding plus a price break; use short volume only as transaction context.`,`Short volume is not a daily change in outstanding short interest, and field dates can differ.`)
 ];
 byId('me-derivatives').innerHTML=`<div class="me-deriv-head"><h3>Whale evidence</h3><span class="me-whale-note"><span>${{w.available?w.sourceCount+' current source snapshots':'enhanced data unavailable'}} · history varies by feature</span><span class="me-whale-key me-whale-bull">${{signalCount('BULLISH')}} bullish</span><span class="me-whale-key me-whale-bear">${{signalCount('BEARISH')}} bearish</span><span class="me-whale-key me-whale-caution">${{signalCount('CAUTIOUS')}} cautious</span></span></div><div class="me-deriv-grid">${{cards.join('')}}</div><div class="me-change-read"><b>What changed:</b> ${{esc(dailyDerivativesRead(e))}}</div>`
}}
function list(items,empty='Not supplied'){{return items.length?`<ul>${{items.map(item=>`<li>${{esc(item)}}</li>`).join('')}}</ul>`:`<div class="me-empty">${{esc(empty)}}</div>`}}
function scenarioTable(rows){{return rows.length?`<div class="me-table-wrap"><table><thead><tr><th>Case</th><th>Conditions</th><th>Conditional read</th><th>Invalidation</th></tr></thead><tbody>${{rows.map(row=>`<tr><td class="me-nowrap">${{esc(row.name)}}</td><td>${{esc(row.conditions.join('; '))}}</td><td>${{esc(row.outcome)}}</td><td>${{esc(row.invalidation.join('; '))}}</td></tr>`).join('')}}</tbody></table></div>`:'<div class="me-empty">No validated scenarios.</div>'}}
function optionResearchTable(e){{const r=e.optionResearch||{{}},rows=r.rows||[];if(!rows.length)return '<div class="me-empty">No direction-matched scenario row is available.</div>';const scenario=(row,key)=>{{const item=row.scenarios?.[key]||{{}},value=item.modeled_return_on_stored_ask;return n(value)?signedPct(value*100):'—'}};return `<div class="me-table-wrap"><table><thead><tr><th>Stored contract</th><th>Research fit</th><th>p10 path</th><th>Center path</th><th>p90 path</th><th>Gate</th></tr></thead><tbody>${{rows.map(row=>`<tr><td><b>${{esc(row.contract)}}</b></td><td>${{n(row.fit)?row.fit.toFixed(0):'—'}}/100</td><td class="${{tone(row.scenarios?.p10?.modeled_return_on_stored_ask)}}">${{scenario(row,'p10')}}</td><td class="${{tone(row.scenarios?.center?.modeled_return_on_stored_ask)}}">${{scenario(row,'center')}}</td><td class="${{tone(row.scenarios?.p90?.modeled_return_on_stored_ask)}}">${{scenario(row,'p90')}}</td><td>${{esc(row.status)}}</td></tr>`).join('')}}</tbody></table><p class="me-table-note">Scenario marks use stored ask, constant stored IV, and the nearest displayed price grid point. Fit is not chance of profit; no row is executable.</p></div>`}}
function optionTable(e){{return e.options.length?`<div class="me-option-list">${{e.options.map(row=>`<article class="me-option-card ${{optionTone(row)}}"><div class="me-option-head"><div><div class="me-option-identity ${{optionTone(row)}}">${{esc(row.type)}} ${{money(row.strike)}} · ${{esc(row.expiry)}}</div><div class="me-option-contract">${{esc(row.contract)}}</div></div><div class="me-option-status">${{esc(row.status)}}<br><span class="me-muted">${{esc(row.quoteFresh?'fresh quote':'stale/reference')}}</span></div></div><div class="me-option-grid"><div class="me-option-field"><span>Bid</span><b>${{money(row.bid)}}</b></div><div class="me-option-field"><span>Ask</span><b>${{money(row.ask)}}</b></div><div class="me-option-field"><span>Mid / spread</span><b>${{money(row.mid)}} · ${{n(row.spreadPct)?pct(row.spreadPct*100):'—'}}</b></div><div class="me-option-field"><span>IV / delta</span><b>${{n(row.iv)?pct(row.iv*100):'—'}} · ${{n(row.delta)?row.delta.toFixed(2):'—'}}</b></div><div class="me-option-field"><span>OI / volume</span><b>${{n(row.oi)?row.oi.toLocaleString():'—'}} · ${{n(row.volume)?row.volume.toLocaleString():'—'}}</b></div><div class="me-option-field"><span>Breakeven</span><b>${{money(row.mechanics.breakeven)}}</b></div><div class="me-option-field"><span>Required move</span><b>${{n(row.mechanics.breakevenMove)?signedPct(row.mechanics.breakevenMove*100):'—'}}</b></div><div class="me-option-field"><span>Risk-neutral B/E</span><b>${{n(row.mechanics.riskNeutralBreakeven)?pct(row.mechanics.riskNeutralBreakeven*100):'—'}}</b></div><div class="me-option-field"><span>DTE</span><b>${{n(row.dte)?row.dte.toFixed(0):'—'}}</b></div><div class="me-option-field"><span>Quote time</span><b>${{esc(timeLabel(row.quoteAt))}}</b></div></div></article>`).join('')}}</div>`:'<div class="me-empty">No reference contracts.</div>'}}
function signalTable(s){{return `<div class="me-table-wrap"><table><tbody><tr><th>Chain</th><td>${{esc(s.chainDate)}} · ${{n(s.validContracts)?s.validContracts.toLocaleString():'—'}} valid · median IV ${{n(s.medianIv)?pct(s.medianIv*100):'—'}} · put/call OI ${{n(s.putCallOi)?s.putCallOi.toFixed(2):'—'}}</td></tr><tr><th>GEX</th><td>${{esc(s.gexDate)}} · put ${{money(s.putWall)}} · flip ${{money(s.gammaFlip)}} · magnet ${{money(s.gammaMagnet)}} · call ${{money(s.callWall)}}</td></tr><tr><th>Flow</th><td>${{esc(s.flowDate)}} · ${{n(s.flowAlerts)?s.flowAlerts.toLocaleString():'—'}} rows · reported premium ${{compact(s.flowPremium)}} · direction not inferred</td></tr><tr><th>OI page</th><td>${{esc(s.oiDate)}} · ${{n(s.oiRows)?s.oiRows.toLocaleString():'—'}} rows · explicit change ${{n(s.oiChange)?s.oiChange.toLocaleString():'—'}}</td></tr><tr><th>Dark pool</th><td>${{esc(s.darkDate)}} · ${{n(s.darkPrints)?s.darkPrints.toLocaleString():'—'}} prints · ${{compact(s.darkPremium)}} · owner/direction unknown</td></tr></tbody></table></div>`}}
function providerSignalTable(e){{const w=e.whale,s=w.stockState,a=w.activity,v=w.volatilityDiagnostics;return `<div class="me-table-wrap"><table><thead><tr><th>Added signal</th><th>Current read</th><th>Why it matters</th></tr></thead><tbody><tr><th>Price-state check</th><td>${{esc(humanStatus(s.marketTime))}} · ${{money(s.price)}} · <span class="${{tone(n(s.change)?s.change*100:null)}}">${{n(s.change)?signedPct(s.change*100):'—'}}</span><br><span class="me-muted">tape ${{esc(s.tapeTime)}} · volume ${{compactNum(s.volume)}}</span></td><td>Confirms whether the provider is showing premarket, regular, or after-hours state. The completed regular-session close remains the model anchor.</td></tr><tr><th>Option activity by price</th><td>dominant ${{money(a.dominantPrice)}} · ${{n(a.dominantShare)?pct(a.dominantShare*100):'—'}} of mapped volume<br><span class="me-muted">call peak ${{money(a.callPeak)}} · put peak ${{money(a.putPeak)}} · near-spot balance <span class="${{tone(n(a.nearBalance)?a.nearBalance*100:null)}}">${{n(a.nearBalance)?signedPct(a.nearBalance*100):'—'}}</span></span></td><td>Locates concentrated option activity. Balance is call versus put volume, not buyer direction or a proven price wall.</td></tr><tr><th>Volatility regime</th><td>anomaly ${{esc(humanStatus(v.anomalyDirection))}} ${{n(v.anomalyScore)?v.anomalyScore.toFixed(2):'—'}} · character ${{esc(humanStatus(v.character))}}<br><span class="me-muted">half-life ${{n(v.halfLife)?v.halfLife.toFixed(1)+'D':'—'}} · Hurst ${{n(v.hurst)?v.hurst.toFixed(2):'—'}} · variance premium rank ${{n(v.premiumRank)?v.premiumRank.toFixed(0):'—'}} (${{esc(v.premiumDate)}})</span></td><td>Helps choose mean-reversion versus persistence assumptions and whether volatility is richly priced. It does not supply direction.</td></tr></tbody></table></div>`}}
function whaleTable(e){{const w=e.whale,g=w.gex,f=w.flow,v=w.volatility,d=w.dark,s=w.short;if(!w.available)return '<div class="me-empty">No enhanced Unusual Whales snapshot was attached to this run.</div>';return `<div class="me-table-wrap"><table><thead><tr><th>Native evidence</th><th>Observed state</th><th>Interpretation boundary</th></tr></thead><tbody><tr><th>Gamma exposure</th><td>${{esc(g.date)}} · regime ${{esc(g.regime)}} · near ±5% net ${{compactNum(g.nearNet)}} · concentration ${{n(g.concentration)?pct(g.concentration*100):'—'}}<br><span class="me-muted">positive ${{money(g.positiveStrike)}} / ${{compactNum(g.positiveExposure)}} · negative ${{money(g.negativeStrike)}} / ${{compactNum(g.negativeExposure)}} · vanna ${{compactNum(g.vanna)}} · charm ${{compactNum(g.charm)}}</span></td><td>Modeled contract exposure. It does not identify dealer inventory, hedge direction, or a guaranteed pin.</td></tr><tr><th>Greek option flow</th><td>${{esc(timeLabel(f.timestamp))}} · delta ${{compactNum(f.delta)}} · vega ${{compactNum(f.vega)}} · OTM delta share ${{n(f.otmShare)?pct(f.otmShare*100):'—'}}<br><span class="me-muted">sign persistence ${{n(f.persistence)?pct(f.persistence*100):'—'}} · late-quarter delta change ${{compactNum(f.lateChange)}} · ${{n(f.transactions)?f.transactions.toLocaleString():'—'}} transactions / ${{n(f.volume)?f.volume.toLocaleString():'—'}} contracts</span></td><td>Transaction-level classification is evidence of activity, not ownership or opening/closing intent unless separately confirmed.</td></tr><tr><th>Volatility</th><td>${{esc(v.date)}} · IV ${{n(v.iv)?pct(v.iv*100):'—'}} · RV ${{n(v.rv)?pct(v.rv*100):'—'}} · spread ${{n(v.gap)?signedPct(v.gap*100):'—'}}<br><span class="me-muted">front ${{n(v.frontIv)?pct(v.frontIv*100):'—'}} / ${{n(v.frontDte)?v.frontDte.toFixed(0):'—'}}D / ${{n(v.frontMove)?pct(v.frontMove*100):'—'}} move · fixed 30D ${{n(v.iv30)?pct(v.iv30*100):'—'}} IV / ${{n(v.move30)?pct(v.move30*100):'—'}} move · 60D ${{n(v.iv60)?pct(v.iv60*100):'—'}}</span></td><td>Implied move is the option market's volatility price, not a directional target or a physical probability interval.</td></tr><tr><th>Dark-pool levels</th><td>${{money(d.level)}} · distance ${{n(d.distance)?signedPct(d.distance*100):'—'}} · dark share ${{n(d.share)?pct(d.share*100):'—'}}<br><span class="me-muted">dark volume ${{compactNum(d.darkVolume)}} · regular volume ${{compactNum(d.regularVolume)}} · ${{n(d.levels)?d.levels.toFixed(0):'—'}} reported levels</span></td><td>Shows where reported off-exchange volume clustered. It does not reveal buyer/seller identity or intent.</td></tr><tr><th>Short crowding</th><td>SI ${{n(s.siFloat)?pct(s.siFloat*100):'—'}} of float · ${{n(s.daysCover)?s.daysCover.toFixed(1):'—'}} days to cover · fee ${{n(s.borrowFee)?pct(s.borrowFee):'—'}}<br><span class="me-muted">available ${{compactNum(s.sharesAvailable)}} · 1D availability change ${{compactNum(s.sharesChange)}} · short-volume ratio ${{n(s.shortVolumeRatio)?pct(s.shortVolumeRatio*100):'—'}} vs 20D ${{n(s.shortVolumeMean20)?pct(s.shortVolumeMean20*100):'—'}}</span></td><td>Short volume is transaction classification, not a daily change in reported short interest. Dates can differ across fields.</td></tr></tbody></table></div><p class="me-table-note">Enhanced capture: ${{w.sourceCount}} current source snapshots. Historical depth varies by feature; persistence matters only where the stored-session count is shown.</p>`}}
function whyFactor(v){{return ({{price:'Price',front_iv:'Front IV',gamma_flip:'Gamma flip',call_wall:'Call wall',put_wall:'Put wall',dark_pool_level:'Dark-pool shelf',call_minus_put_oi:'Call-minus-put OI'}}[v]||String(v).replaceAll('_',' '))}}
function whyDelta(row){{if(!n(row.change))return '—';if(row.factor==='price')return signedPct(row.change);if(row.factor==='front_iv')return `${{Math.abs(row.change)<.0005?'':row.change>0?'+':'−'}}${{Math.abs(row.change*100).toFixed(1)}} vol pts`;if(row.unit==='contracts')return signedCount(row.change);if(row.unit==='price')return signedPrice(row.change);return row.change.toLocaleString(undefined,{{maximumFractionDigits:2}})}}
function whyImpact(e,row){{const change=row.change;if(!n(change))return row.note;const unchanged=row.factor==='front_iv'?Math.abs(change)<.0005:row.unit==='contracts'?Math.abs(change)<.5:row.unit==='price'?Math.abs(change)<.005:false;if(unchanged)return row.factor==='call_minus_put_oi'?'Call-versus-put contract count was unchanged; no new OI tilt was established.':row.factor==='front_iv'?'Front IV was effectively unchanged, so option-cost repricing did not add a material new signal.':'The mapped level was unchanged; its price-response importance depends on later interaction, not migration.';if(row.factor==='price')return change>0?'Improves bullish price confirmation, but one session does not establish persistence.':'Weakens bullish acceptance and strengthens bearish pressure; check whether support and positioning levels also broke.';if(row.factor==='front_iv')return change>0?'Raises long-option entry cost and signals more movement demand; it does not identify direction.':'Reduces long-option cost but can mark volatility compression; cheaper is not automatically underpriced.';if(row.factor==='gamma_flip')return change>0?'Raises the price threshold for an above-flip regime; spot must reclaim the migrated level for confirmation.':'Lowers the regime threshold, but the effect depends on spot holding above it and the map remaining stable.';if(row.factor==='call_wall')return change>0?'Moves the mapped upside concentration higher, increasing room before that reference if price confirms.':'Brings the mapped upside concentration closer, which can reduce room or increase near-term pin risk.';if(row.factor==='put_wall')return change>0?'Moves the mapped downside concentration higher and closer to spot; monitor whether it acts as support or a failure point.':'Moves the downside reference lower, widening the mapped range and increasing room for adverse movement.';if(row.factor==='dark_pool_level')return `Moves the off-exchange response zone ${{change>0?'higher':'lower'}}; only later price rejection or acceptance establishes usefulness.`;if(row.factor==='call_minus_put_oi')return change>0?'Tilts new contract count toward calls relative to puts; opening direction and owner intent remain unknown.':'Tilts new contract count toward puts relative to calls; it is not proof of bearish buying.';return row.note}}
function whyTable(e){{return e.edge.whyToday.length?`<div class="me-table-wrap"><table><thead><tr><th>What changed</th><th>Change</th><th>Why it matters / limit</th></tr></thead><tbody>${{e.edge.whyToday.map(row=>`<tr><td>${{esc(whyFactor(row.factor))}}</td><td>${{esc(whyDelta(row))}}</td><td>${{esc(whyImpact(e,row))}}</td></tr>`).join('')}}</tbody></table></div>`:'<div class="me-empty">A prior comparable snapshot is not available, so no day-over-day change is shown.</div>'}}
function analogTable(e){{const a=e.edge.analogs,h=a.horizons,b=a.baseline;return `<div class="me-table-wrap"><table><thead><tr><th>Forward horizon</th><th>Matched states</th><th>Observed up frequency</th><th>p10 / median / p90</th></tr></thead><tbody>${{['1','5','10','20'].map(k=>{{const row=h[k]||{{}};return `<tr><td>${{k}} session${{k==='1'?'':'s'}}</td><td>${{n(row.sample)?row.sample.toFixed(0):'—'}}</td><td>${{n(row.upRate)?pct(row.upRate*100):'—'}}</td><td>${{ret(row.p10)}} / ${{ret(row.median)}} / ${{ret(row.p90)}}</td></tr>`}}).join('')}}</tbody></table></div><p class="me-table-note">20-session matched-state versus non-overlapping base rate: up ${{n(h['20']?.upRate)?pct(h['20'].upRate*100):'—'}} vs ${{n(b.upRate)?pct(b.upRate*100):'—'}} (lift ${{n(b.upLift)?pct(b.upLift*100):'—'}}); median ${{ret(h['20']?.median)}} vs ${{ret(b.median)}} (lift ${{ret(b.medianLift)}}). Observed frequencies are not probabilities.</p>`}}
function patternRead(e){{const a=e.edge.analogs,m=a.match,h=a.horizons['20']||{{}},x=a.excursion,s=a.stability,derivatives=m.derivativeStatus==='ENABLED'?'included in matching':'held out as context';return `<div class="me-pattern-read"><b>Matched-history read: <span class="${{directionTone(a.disposition)}}">${{esc(a.disposition)}}</span>.</b> ${{n(a.sample)?a.sample.toFixed(0):'—'}} independent matches came from ${{n(m.candidateCount)?m.candidateCount.toFixed(0):'—'}} eligible states across up to ${{n(a.lookback)?a.lookback.toFixed(0):'—'}} sessions. The 20-session median was ${{ret(h.median)}}; median best/worst moves were ${{ret(x.medianMfe)}} / ${{ret(x.medianMae)}}. <b>Stability: ${{esc(s.status)}}.</b> Removing one match at a time moves the median from ${{ret(s.medianMin)}} to ${{ret(s.medianMax)}}; effective sample size is ${{n(s.effectiveSample)?s.effectiveSample.toFixed(1):'—'}}. Derivatives are ${{derivatives}} (${{n(m.derivativeIndependent)?m.derivativeIndependent.toFixed(0):'0'}} of ${{n(m.derivativeRequired)?m.derivativeRequired.toFixed(0):'5'}} required non-overlapping states). These are observed outcomes, not probabilities.</div>`}}
function excursionTable(e){{const x=e.edge.analogs.excursion;return `<div class="me-table-wrap"><table><tbody><tr><th>Path excursion</th><td>median favorable ${{ret(x.medianMfe)}} · median adverse ${{ret(x.medianMae)}} · favorable/adverse magnitude ${{n(x.asymmetry)?x.asymmetry.toFixed(2)+'×':'—'}}</td></tr><tr><th>Threshold reached</th><td>+5% ${{n(x.hitUp5)?pct(x.hitUp5*100):'—'}} · −5% ${{n(x.hitDown5)?pct(x.hitDown5*100):'—'}} · +10% ${{n(x.hitUp10)?pct(x.hitUp10*100):'—'}} · −10% ${{n(x.hitDown10)?pct(x.hitDown10*100):'—'}}</td></tr><tr><th>First 5% move</th><td>up first ${{n(x.upFirst5)?pct(x.upFirst5*100):'—'}} · down first ${{n(x.downFirst5)?pct(x.downFirst5*100):'—'}} · neither ${{n(x.neither5)?pct(x.neither5*100):'—'}}</td></tr><tr><th>Path timing</th><td>median peak session ${{n(x.peakSession)?x.peakSession.toFixed(0):'—'}} · median trough session ${{n(x.troughSession)?x.troughSession.toFixed(0):'—'}}</td></tr></tbody></table></div>`}}
function comparableTable(e){{const rows=e.edge.analogs.rows;return rows.length?`<div class="me-table-wrap"><table><thead><tr><th>Matched date</th><th>Distance</th><th>20D return</th><th>Best / worst path</th><th>First ±5%</th></tr></thead><tbody>${{rows.map(row=>`<tr><td>${{esc(row.date)}}</td><td>${{n(row.distance)?row.distance.toFixed(2):'—'}}</td><td class="${{tone(n(row.outcomes['20'])?row.outcomes['20']*100:null)}}">${{ret(row.outcomes['20'])}}</td><td>${{ret(row.mfe)}} / ${{ret(row.mae)}}<br><span class="me-muted">peak D${{n(row.peakSession)?row.peakSession.toFixed(0):'—'}} · trough D${{n(row.troughSession)?row.troughSession.toFixed(0):'—'}}</span></td><td>${{esc(row.firstMove)}}</td></tr>`).join('')}}</tbody></table></div>`:'<div class="me-empty">No independent matched states.</div>'}}
function derivedTable(e){{const x=e.edge;return `<div class="me-table-wrap"><table><tbody><tr><th>Volatility surface</th><td>front ${{n(x.surface.frontIv)?pct(x.surface.frontIv*100):'—'}} (${{n(x.surface.frontDte)?x.surface.frontDte.toFixed(0):'—'}}D) · back ${{n(x.surface.backIv)?pct(x.surface.backIv*100):'—'}} (${{n(x.surface.backDte)?x.surface.backDte.toFixed(0):'—'}}D) · 25Δ put-call skew ${{n(x.surface.skew25)?pct(x.surface.skew25*100):'—'}}</td></tr><tr><th>Confirmed OI change</th><td>call ${{n(x.oi.callChange)?x.oi.callChange.toLocaleString():'—'}} · put ${{n(x.oi.putChange)?x.oi.putChange.toLocaleString():'—'}} · near spot call/put ${{n(x.oi.nearCall)?x.oi.nearCall.toLocaleString():'—'}} / ${{n(x.oi.nearPut)?x.oi.nearPut.toLocaleString():'—'}}</td></tr><tr><th>Flow context</th><td>${{esc(humanStatus(x.flow.status))}} · directional premium ${{signedCompact(x.flow.directionalPremium)}} · opening ${{n(x.flow.openingShare)?pct(x.flow.openingShare*100):'—'}} · single-leg ${{n(x.flow.singleLegShare)?pct(x.flow.singleLegShare*100):'—'}} · OI confirmed: ${{x.flow.oiConfirmed?'yes':'no'}}</td></tr><tr><th>GEX topology</th><td>${{esc(humanStatus(x.gex.regime))}} · distance to flip ${{n(x.gex.flipDistance)?pct(x.gex.flipDistance*100):'—'}} · call/put wall change ${{signedPrice(x.gex.callWallChange)}} / ${{signedPrice(x.gex.putWallChange)}}</td></tr><tr><th>Dark-pool shelf</th><td>${{money(x.dark.level)}} · ${{n(x.dark.levelShare)?pct(x.dark.levelShare*100):'—'}} of captured premium · spot is ${{esc(humanStatus(x.dark.priceState))}} · distance ${{n(x.dark.levelDistance)?pct(x.dark.levelDistance*100):'—'}} · trade direction unknown</td></tr><tr><th>Earnings history</th><td>n=${{n(x.earnings.eventCount)?x.earnings.eventCount.toFixed(0):'0'}} · median absolute move ${{n(x.earnings.medianMove)?pct(x.earnings.medianMove*100):'—'}} · median 1D straddle return ${{n(x.earnings.straddle1d)?pct(x.earnings.straddle1d*100):'—'}}</td></tr></tbody></table></div>`}}
function optionHeatmap(e){{const row=e.options.find(item=>item.mechanics.matrix.length),m=row?.mechanics;if(!m)return '<div class="me-empty">No stored option mechanics matrix is available.</div>';const headings=m.matrix[0]?.points||[];return `<p class="me-table-note"><b>${{esc(row.contract)}}</b> · return on stored ask under constant IV. This is a model sensitivity table, not a forecast. Risk-neutral B/E ${{n(m.riskNeutralBreakeven)?pct(m.riskNeutralBreakeven*100):'—'}} is not a real-world win probability.</p><div class="me-table-wrap"><table class="me-heat"><thead><tr><th>Underlying</th>${{headings.map(p=>`<th>${{n(p.elapsed)?pct(p.elapsed*100):'—'}} elapsed<br><span class="me-muted">${{n(p.days)?p.days.toFixed(0):'—'}}D left</span></th>`).join('')}}</tr></thead><tbody>${{m.matrix.map(r=>`<tr><td>${{n(r.move)?pct(r.move*100):'—'}} · ${{money(r.price)}}</td>${{r.points.map(p=>{{const cls=!n(p.return)?'':p.return>.02?'me-heat-pos':p.return<-.02?'me-heat-neg':'me-heat-flat';return `<td class="${{cls}}">${{n(p.return)?pct(p.return*100):'—'}}<br><span class="me-muted">${{money(p.value)}}</span></td>`}}).join('')}}</tr>`).join('')}}</tbody></table></div>`}}
function challengerStrip(e){{const c=e.edge.challengers||{{}},rows=c.models||[];if(!rows.length)return '<div class="me-empty">Shadow challengers need more price history.</div>';const label=v=>v.includes('regularized-logistic')?'Regularized direction':v.includes('historical-quantile')?'Historical base rate':'EWMA volatility';return `<div class="me-challenger"><div class="me-challenger-head"><b>Independent shadow checks</b><span>${{n(c.agreement)?pct(c.agreement*100)+' directional agreement':'directional agreement unavailable'}}</span></div><div class="me-challenger-grid">${{rows.map(row=>`<div><span>${{esc(label(row.version))}} · ${{n(row.horizon)?row.horizon.toFixed(0):'—'}}D</span><b class="${{directionTone(row.direction)}}">${{esc(row.direction)}}</b><small>${{n(row.sample)?'n='+row.sample.toFixed(0):n(row.annualizedVol)?'vol '+pct(row.annualizedVol*100):'shadow'}}${{n(row.rawScore)?' · raw score '+row.rawScore.toFixed(2):''}}</small></div>`).join('')}}</div><p>These models are frozen for evaluation. Raw scores are not probabilities and cannot change the active thesis.</p></div>`}}
function thesisFlipMap(e){{const t=e.thesis;return `<div class="me-flip-map"><div><span>Strengthen</span><b>${{esc(t.trigger)}}</b></div><div><span>Weaken or fail</span><b>${{esc(t.invalidation)}}</b></div><div><span>Decision boundary</span><b>Only tested, cutoff-safe signals can change calibrated confidence.</b></div></div>`}}
function forecastMethod(e){{const v4=e.edge.forecastV4,a=e.edge.analogs,w=e.whale.volatility,x=e.edge,m=a.match,s=v4.scaling,derivativeGate=m.derivativeStatus==='ENABLED'?`passed · ${{n(m.derivativeIndependent)?m.derivativeIndependent.toFixed(0):'—'}} independent states`:`held out · ${{n(m.derivativeIndependent)?m.derivativeIndependent.toFixed(0):'0'}}/${{n(m.derivativeRequired)?m.derivativeRequired.toFixed(0):'5'}} states`;return `${{challengerStrip(e)}}${{thesisFlipMap(e)}}<div class="me-forecast-note"><div><b>V4 center</b>75% volatility-scaled matched median + 25% capped trend. 1W and 20D use one frozen path.</div><div><b>V4 range</b>p10/p25/p75/p90 from ${{n(v4.sample)?v4.sample.toFixed(0):'—'}} scaled paths. Target volatility ${{n(s.targetVol)?pct(s.targetVol*100):'—'}}; median scale ${{n(s.medianScale)?s.medianScale.toFixed(2)+'×':'—'}}.</div><div><b>V3 comparison</b>Unscaled baseline remains visible and tracked. V4 cannot replace it before walk-forward error and coverage tests pass.</div><div><b>Derivative gate</b>${{derivativeGate}}. IV implies ±${{n(w.move30)?pct(w.move30*100):'—'}} over 30D but adds no direction.</div></div><p class="me-table-note"><b>Evidence depth:</b> price ${{e.bars.length}} sessions · analog search ${{n(a.lookback)?a.lookback.toFixed(0):'—'}} · flow ${{n(x.flow.historySessions)?x.flow.historySessions.toFixed(0):'0'}} · IV surface ${{n(x.surface.historySessions)?x.surface.historySessions.toFixed(0):'0'}} · comparable GEX ${{n(x.gex.comparableHistorySessions)?x.gex.comparableHistorySessions.toFixed(0):'0'}}. News, flow, GEX, and market tide stay contextual until timestamped out-of-sample lift is shown.</p>`}}
function evaluationHorizonTable(){{const h=DATA.evaluation.horizons;return `<div class="me-table-wrap"><table><thead><tr><th>Horizon</th><th>Independent origins</th><th>Row diagnostic</th><th>Center error / range</th><th>Paper option</th></tr></thead><tbody>${{['1','5','10','20'].map(key=>{{const x=h[key];return `<tr><td>${{key}} session${{key==='1'?'':'s'}}<br><span class="me-muted">${{n(x.evaluated)?x.evaluated.toFixed(0):'0'}} resolved · ${{n(x.pending)?x.pending.toFixed(0):'0'}} pending</span></td><td>${{n(x.independentAccuracy)?pct(x.independentAccuracy*100):'—'}} vs ${{n(x.independentBaseline)?pct(x.independentBaseline*100):'—'}}<br><span class="me-muted">lift ${{n(x.independentLift)?signedPct(x.independentLift*100):'—'}} · ${{n(x.independentOrigins)?x.independentOrigins.toFixed(0):'0'}} origins</span></td><td>${{n(x.accuracy)?pct(x.accuracy*100):'—'}} vs ${{n(x.baseline)?pct(x.baseline*100):'—'}}<br><span class="me-muted">lift ${{n(x.lift)?signedPct(x.lift*100):'—'}} · n=${{n(x.directionCount)?x.directionCount.toFixed(0):'0'}}</span></td><td>${{n(x.medianError)?pct(x.medianError):'—'}} / ${{n(x.rangeCoverage)?pct(x.rangeCoverage*100):'—'}}</td><td>${{n(x.medianOptionReturn)?pct(x.medianOptionReturn):'—'}} · n=${{n(x.optionCount)?x.optionCount.toFixed(0):'0'}}</td></tr>`}}).join('')}}</tbody></table></div>`}}
function performanceBreakdownTable(groups,label){{const rows=Object.entries(groups||{{}});if(!rows.length)return '<div class="me-empty">Tracking begins with this publication. No grouped result is available yet.</div>';return `<div class="me-table-wrap"><table><thead><tr><th>${{esc(label)}}</th><th>Published / resolved / pending</th><th>Direction accuracy</th><th>Lift vs realized majority</th><th>Median underlying</th><th>Paper option</th></tr></thead><tbody>${{rows.map(([name,x])=>{{const prospective=n(x.prospectiveDirectionCount)&&x.prospectiveDirectionCount>0,accuracy=prospective?x.prospectiveAccuracy:x.accuracy,baseline=prospective?x.prospectiveBaseline:x.baseline,lift=prospective?x.prospectiveLift:x.lift,optionReturn=prospective?x.prospectiveMedianOptionReturn:x.medianOptionReturn,optionCount=prospective?x.prospectiveOptionCount:x.optionCount,count=prospective?x.prospectiveDirectionCount:x.directionCount;return `<tr><td><b>${{esc(name)}}</b><br><span class="me-muted">${{prospective?'prospective':'seed or pending'}}</span></td><td>${{n(x.registered)?x.registered.toFixed(0):'0'}} / ${{n(x.evaluated)?x.evaluated.toFixed(0):'0'}} / ${{n(x.pending)?x.pending.toFixed(0):'0'}}</td><td class="${{n(lift)?tone(lift):''}}">${{n(accuracy)?pct(accuracy*100):'—'}}<br><span class="me-muted">baseline ${{n(baseline)?pct(baseline*100):'—'}} · n=${{n(count)?count.toFixed(0):'0'}}</span></td><td class="${{n(lift)?tone(lift):''}}">${{n(lift)?signedPct(lift*100):'—'}}</td><td class="${{tone(x.medianReturn)}}">${{n(x.medianReturn)?pct(x.medianReturn):'—'}}</td><td class="${{tone(optionReturn)}}">${{n(optionReturn)?pct(optionReturn):'—'}} · n=${{n(optionCount)?optionCount.toFixed(0):'0'}}</td></tr>`}}).join('')}}</tbody></table></div>`}}
function tickerPerformanceTable(e){{const row=DATA.evaluation.tickers?.[e.ticker];return row?performanceBreakdownTable({{[e.ticker]:row}},'Ticker'):'<div class="me-empty">No frozen tracker row for this ticker yet.</div>'}}
function outcomeAutopsy(row){{if(row.correct===null)return 'Direction not scored.';const direction=row.correct?'Direction held.':'Direction failed.',range=row.covered===true?'Outcome stayed inside the published range.':row.covered===false?'Outcome breached the published range.':'Range unavailable.',option=n(row.optionReturn)?` Paper option ${{row.optionReturn>=0?'gained':'lost'}} ${{pct(Math.abs(row.optionReturn))}}.`:' Option mark unavailable.';return direction+' '+range+option}}
function tickerEvaluationTable(e){{const rows=DATA.evaluation.recentRows.filter(row=>row.ticker===e.ticker);return rows.length?`<div class="me-table-wrap"><table><thead><tr><th>Origin → target</th><th>Call and autopsy</th><th>Realized</th><th>Center error</th><th>Range</th><th>Paper option</th></tr></thead><tbody>${{rows.map(row=>`<tr><td>${{esc(row.origin)}} → ${{esc(row.target)}} · ${{n(row.horizon)?row.horizon.toFixed(0):'—'}}D</td><td class="${{row.correct===true?'me-pos':row.correct===false?'me-neg':''}}">${{esc(row.direction)}} · ${{row.correct===true?'correct':row.correct===false?'wrong':'not scored'}}<br><span class="me-muted">${{esc(outcomeAutopsy(row))}}</span></td><td class="${{tone(row.return)}}">${{n(row.return)?pct(row.return):'—'}}</td><td>${{n(row.error)?pct(row.error):'—'}}</td><td>${{row.covered===true?'inside':row.covered===false?'outside':'—'}}</td><td class="${{tone(row.optionReturn)}}">${{n(row.optionReturn)?pct(row.optionReturn):row.optionAvailable?'—':'quote unavailable'}}</td></tr>`).join('')}}</tbody></table></div>`:'<div class="me-empty">No resolved walk-forward outcome for this ticker yet.</div>'}}
function renderStockRecord(e){{byId('me-stock-record').innerHTML=`<details><summary>${{esc(e.ticker)}} forecast record <span>ticker-specific prospective and paper outcomes</span></summary><div class="me-detail-body"><h3>${{esc(e.ticker)}} aggregate</h3>${{tickerPerformanceTable(e)}}<h3>Resolved forecasts</h3>${{tickerEvaluationTable(e)}}<p class="me-table-note">Pending rows are not misses. Option returns use the stored ask and later stored bid; unavailable marks remain unavailable.</p></div></details>`}}
function renderPlatformDetails(){{const ev=DATA.evaluation;byId('me-platform-details').innerHTML=`<details><summary>Evaluation detail <span>${{n(ev.evaluated)?ev.evaluated.toFixed(0):'0'}} resolved · calibration ${{ev.calibrated?'passed':'blocked'}}</span></summary><div class="me-detail-body"><h3>All horizons</h3>${{evaluationHorizonTable()}}<h3>V3 vs V4</h3>${{performanceBreakdownTable(ev.models,'Model')}}<h3>Trend regimes</h3>${{performanceBreakdownTable(ev.regimes?.trend,'Trend regime')}}<h3>Volatility regimes</h3>${{performanceBreakdownTable(ev.regimes?.volatility,'Volatility regime')}}<h3>Published direction</h3>${{performanceBreakdownTable(ev.directions,'Direction')}}<h3>Evidence-score bands</h3>${{performanceBreakdownTable(ev.convictionBands,'Evidence band')}}<p class="me-table-note">Independent-origin metrics are primary. Row metrics can overstate evidence because many tickers share one market day. V4 remains shadow-only until chronological stability, leakage, range, and friction tests pass.</p></div></details><details><summary>Metric glossary <span>decision terms only</span></summary><div class="me-detail-body"><table><tbody><tr><th>Evidence score</th><td>Strength and agreement of current inputs. It is not win probability.</td></tr><tr><th>Independent-origin lift</th><td>Equal-weight accuracy gain over the realized majority direction, with each publication date counted once.</td></tr><tr><th>GEX</th><td>Modeled gamma exposure by strike. It does not reveal dealer inventory.</td></tr><tr><th>Flow persistence</th><td>Share of stored sessions with the same aggregate Greek-flow sign. It does not identify owner intent.</td></tr><tr><th>p10–p90</th><td>Observed outer range from matched historical paths. It is descriptive, not a calibrated probability interval.</td></tr><tr><th>Paper option return</th><td>Stored ask to later stored bid. It is not a fill or executable result.</td></tr></tbody></table></div></details>`}}
function removeGlobalDetailFromStock(){{const detail=[...byId('me-details').querySelectorAll('details')].find(node=>node.querySelector('summary')?.textContent.trim().startsWith('Model accountability'));if(detail)detail.remove()}}
function renderDetails(e){{const headlinePreview=e.headlines.length?e.headlines[0].headline:'No provider headlines',analog=e.edge.analogs,ev=DATA.evaluation;byId('me-details').innerHTML=`<details open><summary>Pattern evidence and comparable states <span>${{esc(analog.disposition)}} · n=${{n(analog.sample)?analog.sample.toFixed(0):'0'}} · descriptive</span></summary><div class="me-detail-body">${{patternRead(e)}}${{forecastMethod(e)}}<div class="me-detail-grid"><section><h3>What changed and why it matters</h3><div class="me-pattern-read"><b>Daily derivatives synthesis.</b> ${{esc(dailyDerivativesRead(e))}}</div>${{whyTable(e)}}</section><section><h3>Forward outcomes after matched states</h3>${{analogTable(e)}}</section></div><div class="me-detail-grid"><section><h3>Path and first-passage diagnostics</h3>${{excursionTable(e)}}</section><section><h3>Closest independent states</h3>${{comparableTable(e)}}</section></div><h3>Evidence decomposition</h3>${{derivedTable(e)}}</div></details><details><summary>Model accountability <span>${{n(ev.evaluated)?ev.evaluated.toFixed(0):'0'}} resolved · calibration ${{ev.calibrated?'passed':'blocked'}}</span></summary><div class="me-detail-body"><p class="me-table-note">Every row was frozen at publication. Underlying outcomes use the first later stored regular-session close. Option marks use the published ask and later stored bid; missing contracts stay unavailable. These are paper measurements, not fills.</p><h3>All horizons</h3>${{evaluationHorizonTable()}}<h3>Direction choice performance</h3>${{performanceBreakdownTable(ev.directions,'Published direction')}}<h3>Conviction selectivity</h3>${{performanceBreakdownTable(ev.convictionBands,'Conviction band')}}<h3>${{esc(e.ticker)}} tracker</h3>${{tickerPerformanceTable(e)}}<h3>${{esc(e.ticker)}} resolved forecasts</h3>${{tickerEvaluationTable(e)}}<p class="me-table-note">${{esc(ev.optionMethod)}}. Pending rows are not counted as misses. No output is calibrated or promoted until sample-size, chronological stability, leakage, and friction checks pass.</p></div></details><details><summary>News impact and catalysts <span>${{esc(headlinePreview)}}</span></summary><div class="me-detail-body"><div class="me-detail-grid"><section><h3>Provider headlines</h3>${{e.headlines.length?`<ul>${{e.headlines.map(row=>`<li>${{esc(row.headline)}}<br><span class="me-muted">${{esc(row.source)}} · ${{esc(timeLabel(row.published))}}</span></li>`).join('')}}</ul>`:'<div class="me-empty">No headlines in the bounded response.</div>'}}</section><section><h3>Impact boundary</h3><p>${{esc(newsImpact(e).mechanism)}} Latest regular-session reaction: ${{n(e.change)?pct(e.change):'—'}}. Provider sentiment history is ${{n(e.edge.news.historySessions)?e.edge.news.historySessions.toFixed(0):'0'}} session, so this is not a calibrated news-return signal.</p></section></div></div></details><details><summary>Agent analysis <span>${{esc(e.analysis.summary)}}</span></summary><div class="me-detail-body"><p>${{esc(e.analysis.summary)}}</p><div class="me-detail-grid"><section><h3>Supporting evidence</h3>${{list(e.claims)}}</section><section><h3>Counterevidence and unknowns</h3>${{list(e.counter.concat(e.unknowns))}}</section></div></div></details><details><summary>Conditional scenarios <span>Bull · base · bear, no probabilities</span></summary><div class="me-detail-body">${{scenarioTable(e.analysis.scenarios)}}</div></details><details><summary>Options, positioning, and P/L sensitivity <span>${{e.whale.available?'native whale evidence attached':'enhanced snapshot unavailable'}} · ${{esc(e.analysis.optionContext)}}</span></summary><div class="me-detail-body"><h3>Added provider signals</h3>${{providerSignalTable(e)}}<h3>Native Unusual Whales evidence</h3>${{whaleTable(e)}}<h3>Stored chain and aggregate pages</h3>${{signalTable(e.signals)}}<p class="me-muted">${{esc(e.analysis.optionContext)}}</p><h3>Shadow scenario fit</h3>${{optionResearchTable(e)}}<h3>Reference contracts</h3>${{optionTable(e)}}${{optionHeatmap(e)}}</div></details><details><summary>Tracked positions <span>${{e.positions.length?e.positions.length+' open item(s)':'none supplied'}}</span></summary><div class="me-detail-body">${{e.positions.length?`<ul>${{e.positions.map(row=>`<li>${{esc(row.contract||row.symbol||'—')}} · ${{esc(row.state||'UNASSESSED')}}<br><span class="me-muted">${{esc(row.note||row.reason||'No handling note')}}</span></li>`).join('')}}</ul>`:'<div class="me-empty">No tracked position supplied.</div>'}}</div></details>`}}
function renderSelected(){{const e=DATA.entries[selected],t=e.thesis,a=e.edge.analogs,v4=e.edge.forecastV4,i=e.intraday||{{}},live=DATA.refresh?.mode==='INTRADAY'&&n(i.observedPrice);byId('me-stock-scope').textContent=`SELECTED STOCK · ${{e.ticker}}`;byId('me-selected-title').innerHTML=`#${{n(e.rank)?e.rank.toFixed(0):'—'}} · ${{esc(e.ticker)}} · <span class="${{directionTone(t.direction)}}">${{esc(t.direction)}}</span> · evidence ${{n(t.conviction)?t.conviction.toFixed(0):'—'}}/100`;byId('me-selected-price').innerHTML=`${{money(live?i.observedPrice:e.price)}} <span class="${{tone(live?(i.changeFromAnchor||0)*100:e.change)}}">${{live?signedPct((i.changeFromAnchor||0)*100):pct(e.change)}}</span>${{live?` <span class="me-muted">live · daily anchor ${{money(i.dailyAnchorPrice)}}</span>`:''}}`;byId('me-outlook').textContent=`${{t.direction}} thesis. ${{live?'Intraday '+i.status.toLowerCase()+'. ':''}}V4 frozen: 1W ${{n(v4.center5)?ret(v4.center5):'—'}}, 20D ${{n(v4.center20)?ret(v4.center20):'—'}}. Matched states: ${{a.disposition}}. Confirm: ${{t.trigger}} Fail: ${{t.invalidation}}`;byId('me-current-ticker').textContent=`${{e.ticker}} · ${{t.direction}}`;renderFreshness(e);lockedIndex=null;renderThesis(e);renderIntel(e);renderScoreChange(e);drawDecisionLadder(e);renderStockTimeline(e);renderDerivatives(e);renderEdge(e);const engineTitle=byId('me-edge').querySelector('.me-engine-title b');if(engineTitle)engineTitle.textContent=`${{e.ticker}} evidence quality`;renderStockRecord(e);renderDetails(e);removeGlobalDetailFromStock();drawActiveChart();drawOpportunityMap()}}
function selectTicker(index){{selected=Math.max(0,Math.min(DATA.entries.length-1,index));try{{const url=new URL(location.href);url.searchParams.set('ticker',DATA.entries[selected].ticker);history.replaceState(null,'',url)}}catch{{}}updateRankingState();updateTrackingSelection();renderSelected();if(root.clientWidth<=760)setTimeout(()=>root.querySelector('.me-stock').scrollIntoView({{block:'start'}}),50)}}
byId('me-ask').addEventListener('click',async()=>{{const e=DATA.entries[selected],a=e.edge.analogs,t=e.thesis,w=e.whale,c=DATA.contexts,v4=e.edge.forecastV4,prompt=`Stress-test the ${{e.ticker}} ${{t.direction}} thesis from the current stored run. Use only displayed evidence and provenance. Lead with: (1) disposition, (2) two strongest drivers, (3) strongest conflict, (4) 1W and 20D triggers and invalidation, and (5) option-reference fit. Reconcile price, IV/RV and term slope, confirmed OI changes, named and native GEX, Greek delta/vega flow, OTM share and sign persistence, dark-pool levels, short interest/borrow/short volume, earnings, market and sector tide, and news. Data depth: enhanced=${{w.available}}, flow=${{e.edge.flow.historySessions}}, surface=${{e.edge.surface.historySessions}}, GEX=${{e.edge.gex.historySessions}}. Tide: market=${{c.market.callMinusPut}}, Technology=${{c.technology.callMinusPut}}, Communication Services=${{c.communication.callMinusPut}}. Matched states: n=${{a.sample}}, disposition=${{a.disposition}}, with 1/5/10/20-session paths, base-rate lift, excursions, first ±5% passage, timing, distance, and regime mismatch. V4 shadow: 1W center=${{v4.center5}}, 1W p10/p90=${{v4.p10_5}}/${{v4.p90_5}}, 20D center=${{v4.center20}}, 20D p10/p90=${{v4.p10}}/${{v4.p90}}, target volatility=${{v4.scaling.targetVol}}. Compare V4 with active V3; do not promote V4. Challenge selection bias, overlap, small samples, instability, timestamp mismatch, and signal disagreement. Treat untested current-state fields as context. Do not call frequencies probabilities, infer dealer inventory from modeled GEX, infer owner intent from flow or dark-pool totals, or equate short volume with changing short interest. Keep the answer concise and decision-focused.`;if(window.openai?.sendFollowUpMessage)await window.openai.sendFollowUpMessage({{prompt,title:`Stress-test ${{e.ticker}} thesis`}})}});
let resizeFrame=0;window.addEventListener('resize',()=>{{const width=Math.round(byId('me-chart-wrap').getBoundingClientRect().width);if(Math.abs(width-lastWidth)<16)return;cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(()=>{{drawActiveChart();drawFlowMini(DATA.entries[selected]);drawOpportunityMap();drawDecisionLadder(DATA.entries[selected])}})}},{{passive:true}});
const renderDetailsExpanded=renderDetails;
renderDetails=e=>{{renderDetailsExpanded(e);if(window.matchMedia('(max-width:760px)').matches){{const first=byId('me-details').querySelector('details');if(first)first.open=false}}}};
byId('me-prev').addEventListener('click',()=>selectTicker((selected-1+DATA.entries.length)%DATA.entries.length));
byId('me-next').addEventListener('click',()=>selectTicker((selected+1)%DATA.entries.length));
byId('me-current-ticker').addEventListener('click',()=>root.querySelector('.me-watchboard')?.scrollIntoView({{block:'start'}}));
document.addEventListener('keydown',event=>{{if(event.target?.matches?.('input,textarea,select,[contenteditable=true]'))return;if(event.key==='j'||event.key==='ArrowDown'){{event.preventDefault();selectTicker((selected+1)%DATA.entries.length)}}else if(event.key==='k'||event.key==='ArrowUp'){{event.preventDefault();selectTicker((selected-1+DATA.entries.length)%DATA.entries.length)}}else if(event.key.toLowerCase()==='f')setProductMode('FOCUS');else if(event.key.toLowerCase()==='r')setProductMode('RESEARCH')}});
setProductMode(productMode);setRunHeader();renderConsensusPulse();renderSystemStatus();renderMacro();renderRanking();renderWatchAlerts();renderWatchControls();renderControls();renderEvaluation();renderPlatformTracking();renderPlatformDetails();renderSelected();renderReplay();
}})();
</script>
</section>'''
    fragment = template.replace("{{", "{").replace("}}", "}").replace("__DATA__", data)
    return _inline_script_fragment(fragment)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render an enriched Codex Screener dashboard fragment from a JSON run artifact")
    parser.add_argument("--input", required=True, type=Path, help="Prepared JSON run artifact; never a SQLite database")
    parser.add_argument("--enhanced-input", type=Path, help="Optional enhanced Unusual Whales summary JSON sidecar")
    parser.add_argument("--evaluation-input", type=Path, help="Optional immutable walk-forward evaluation summary JSON")
    parser.add_argument("--previous-input", type=Path, help="Optional prior prepared run for day-over-day score changes")
    parser.add_argument("--output", required=True, type=Path, help="HTML fragment destination")
    args = parser.parse_args(argv)
    run = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(run, Mapping):
        raise SystemExit("input JSON must be an object")
    if args.enhanced_input:
        enhanced = json.loads(args.enhanced_input.read_text(encoding="utf-8"))
        if not isinstance(enhanced, Mapping):
            raise SystemExit("enhanced input JSON must be an object")
        run = dict(run)
        run["enhanced_summary"] = enhanced
    if args.evaluation_input:
        evaluation = json.loads(args.evaluation_input.read_text(encoding="utf-8"))
        if not isinstance(evaluation, Mapping):
            raise SystemExit("evaluation input JSON must be an object")
        run = dict(run)
        run["model_evaluation"] = evaluation
    if args.previous_input:
        previous_run = json.loads(args.previous_input.read_text(encoding="utf-8"))
        if not isinstance(previous_run, Mapping):
            raise SystemExit("previous input JSON must be an object")
        run = dict(run)
        run["previous_run"] = previous_run
    fragment = build_fragment(run)
    if len(fragment.encode("utf-8")) >= 2_000_000:
        raise SystemExit("rendered fragment exceeds the 2 MB safety limit")
    write_private_text(args.output, fragment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
