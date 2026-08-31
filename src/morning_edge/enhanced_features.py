"""Deterministic summaries for enhanced raw provider snapshots.

The calculations expose interpretable components. They do not manufacture a
probability, trade instruction, or dealer-inventory claim from provider data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from statistics import fmean
from typing import Any, Mapping, Sequence


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rows(payload: object, *, field: str = "data") -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    data = payload.get(field)
    if isinstance(data, list):
        return [dict(row) for row in data if isinstance(row, Mapping)]
    return [dict(data)] if isinstance(data, Mapping) and data else []


def _sum(row: Mapping[str, Any], *fields: str) -> float:
    return sum(_number(row.get(field)) or 0.0 for field in fields)


def summarize_greek_exposure(rows: Sequence[Mapping[str, Any]], *, spot: float | None) -> dict[str, Any]:
    points: list[tuple[float, float, float, float]] = []
    for row in rows:
        strike = _number(row.get("strike"))
        if strike is None:
            continue
        points.append((
            strike,
            _sum(row, "call_gex", "put_gex"),
            _sum(row, "call_vanna", "put_vanna"),
            _sum(row, "call_charm", "put_charm"),
        ))
    if not points:
        return {"quality": "empty"}
    total_abs = sum(abs(item[1]) for item in points)
    strongest_positive = max(points, key=lambda item: item[1])
    strongest_negative = min(points, key=lambda item: item[1])
    near = [item for item in points if spot and abs(item[0] / spot - 1.0) <= 0.05]
    near_gex = sum(item[1] for item in near) if near else None
    return {
        "quality": "observed",
        "strike_count": len(points),
        "provider_date": next((row.get("date") for row in rows if row.get("date")), None),
        "net_gex_total": sum(item[1] for item in points),
        "net_vanna_total": sum(item[2] for item in points),
        "net_charm_total": sum(item[3] for item in points),
        "gex_concentration": abs(max(points, key=lambda item: abs(item[1]))[1]) / total_abs if total_abs else None,
        "strongest_positive_gex_strike": strongest_positive[0],
        "strongest_positive_gex": strongest_positive[1],
        "strongest_negative_gex_strike": strongest_negative[0],
        "strongest_negative_gex": strongest_negative[1],
        "near_spot_net_gex_5pct": near_gex,
        "near_spot_regime": None if near_gex is None else ("positive" if near_gex > 0 else "negative" if near_gex < 0 else "flat"),
        "caveat": "Modeled listed-option exposure; not verified dealer inventory or hedge direction.",
    }


def summarize_greek_flow(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted((row for row in rows if row.get("timestamp")), key=lambda row: str(row["timestamp"]))
    if not ordered:
        return {"quality": "empty"}
    final = ordered[-1]
    final_delta = _number(final.get("dir_delta_flow"))
    final_vega = _number(final.get("dir_vega_flow"))
    otm_delta = _number(final.get("otm_dir_delta_flow"))
    delta_series = [_number(row.get("dir_delta_flow")) for row in ordered]
    valid_delta = [value for value in delta_series if value is not None]
    final_sign = 1 if (final_delta or 0) > 0 else -1 if (final_delta or 0) < 0 else 0
    same_sign = sum(1 for value in valid_delta if (1 if value > 0 else -1 if value < 0 else 0) == final_sign)
    quarter = ordered[max(0, int(len(ordered) * 0.75) - 1)]
    quarter_delta = _number(quarter.get("dir_delta_flow"))
    return {
        "quality": "observed",
        "row_count": len(ordered),
        "final_timestamp": final.get("timestamp"),
        "directional_delta_flow": final_delta,
        "directional_vega_flow": final_vega,
        "otm_directional_delta_flow": otm_delta,
        "otm_delta_share": abs(otm_delta / final_delta) if otm_delta is not None and final_delta not in {None, 0.0} else None,
        "delta_sign_persistence": same_sign / len(valid_delta) if valid_delta else None,
        "last_quarter_delta_change": final_delta - quarter_delta if final_delta is not None and quarter_delta is not None else None,
        "transactions": _number(final.get("transactions")),
        "volume": _number(final.get("volume")),
        "caveat": "Provider directional classification can include closing trades, spreads, and dealer activity.",
    }


def summarize_volatility(
    term_rows: Sequence[Mapping[str, Any]],
    stats_rows: Sequence[Mapping[str, Any]],
    interpolated_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    term = sorted(
        (row for row in term_rows if _number(row.get("dte")) is not None),
        key=lambda row: _number(row.get("dte")) or 0,
    )
    stats = stats_rows[-1] if stats_rows else {}
    interp = [row for row in interpolated_rows if _number(row.get("days")) is not None]

    def nearest(source: Sequence[Mapping[str, Any]], field: str, target: float) -> Mapping[str, Any] | None:
        return min(source, key=lambda row: abs((_number(row.get(field)) or 0) - target)) if source else None

    front = next((row for row in term if (_number(row.get("dte")) or 0) > 0), None)
    at_30 = nearest(term, "dte", 30)
    at_60 = nearest(term, "dte", 60)
    fixed_30 = nearest(interp, "days", 30)
    iv = _number(stats.get("iv"))
    rv = _number(stats.get("rv"))
    iv30 = _number(at_30.get("volatility")) if at_30 else None
    iv60 = _number(at_60.get("volatility")) if at_60 else None
    return {
        "quality": "observed" if term or stats or interp else "empty",
        "provider_date": stats.get("date") or (front or {}).get("date"),
        "iv": iv,
        "realized_volatility": rv,
        "iv_minus_rv": iv - rv if iv is not None and rv is not None else None,
        "iv_rank": _number(stats.get("iv_rank")),
        "front_dte": _number((front or {}).get("dte")),
        "front_iv": _number((front or {}).get("volatility")),
        "front_implied_move_pct": _number((front or {}).get("implied_move_perc")),
        "iv_30d": iv30,
        "iv_60d": iv60,
        "term_slope_30d_to_60d": iv60 - iv30 if iv30 is not None and iv60 is not None else None,
        "fixed_30d_implied_move_pct": _number((fixed_30 or {}).get("implied_move_perc")),
        "fixed_30d_iv_percentile": _number((fixed_30 or {}).get("percentile")),
        "caveat": "IV measures option-implied dispersion, not the probability or direction of a stock move.",
    }


def summarize_dark_pool(rows: Sequence[Mapping[str, Any]], *, spot: float | None) -> dict[str, Any]:
    levels: list[tuple[float, float, float]] = []
    for row in rows:
        price = _number(row.get("price")); dark = _number(row.get("dark_pool_volume")); regular = _number(row.get("regular_volume"))
        if price is not None and dark is not None:
            levels.append((price, dark, regular or 0.0))
    if not levels:
        return {"quality": "empty"}
    top = max(levels, key=lambda item: item[1])
    dark_total = sum(item[1] for item in levels); regular_total = sum(item[2] for item in levels)
    return {
        "quality": "observed",
        "level_count": len(levels),
        "dominant_price": top[0],
        "dominant_dark_volume": top[1],
        "dominant_distance_pct": top[0] / spot - 1.0 if spot else None,
        "dark_volume_total": dark_total,
        "regular_volume_total": regular_total,
        "dark_share_at_reported_levels": dark_total / (dark_total + regular_total) if dark_total + regular_total else None,
        "caveat": "Off-exchange prints do not identify beneficial owner, opening status, or trade thesis.",
    }


def summarize_short(
    interest_rows: Sequence[Mapping[str, Any]],
    borrow_rows: Sequence[Mapping[str, Any]],
    volume_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    interest = max(interest_rows, key=lambda row: str(row.get("market_date") or "")) if interest_rows else {}
    borrow = sorted((row for row in borrow_rows if row.get("timestamp")), key=lambda row: str(row["timestamp"]))
    latest_borrow = borrow[-1] if borrow else {}
    prior_borrow: Mapping[str, Any] = {}
    if borrow:
        try:
            cutoff = datetime.fromisoformat(str(latest_borrow["timestamp"]).replace("Z", "+00:00")) - timedelta(days=1)
            prior_borrow = min(
                borrow,
                key=lambda row: abs(datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")) - cutoff),
            )
        except (KeyError, ValueError, TypeError):
            prior_borrow = {}
    volume = sorted(volume_rows, key=lambda row: str(row.get("market_date") or ""), reverse=True)
    latest_volume = volume[0] if volume else {}
    recent_ratios = [value for row in volume[:20] if (value := _number(row.get("short_volume_ratio"))) is not None]
    latest_available = _number(latest_borrow.get("short_shares_available"))
    prior_available = _number(prior_borrow.get("short_shares_available"))
    return {
        "quality": "observed" if interest or borrow or volume else "empty",
        "short_interest_date": interest.get("market_date"),
        "short_interest": _number(interest.get("short_interest")),
        "short_interest_float": _number(interest.get("si_float")),
        "days_to_cover": _number(interest.get("days_to_cover")),
        "latest_borrow_timestamp": latest_borrow.get("timestamp"),
        "borrow_fee_rate": _number(latest_borrow.get("fee_rate")),
        "short_shares_available": latest_available,
        "shares_available_change_vs_approx_1d": latest_available - prior_available if latest_available is not None and prior_available is not None else None,
        "short_volume_date": latest_volume.get("market_date"),
        "short_volume_ratio": _number(latest_volume.get("short_volume_ratio")),
        "short_volume_ratio_20d_mean": fmean(recent_ratios) if recent_ratios else None,
        "caveat": "Short volume is not a change in short interest; borrow observations can be venue-specific estimates.",
    }


def summarize_tide(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted((row for row in rows if row.get("timestamp")), key=lambda row: str(row["timestamp"]))
    if not ordered:
        return {"quality": "empty"}
    complete = [
        row for row in ordered
        if _number(row.get("net_call_premium")) is not None
        and _number(row.get("net_put_premium")) is not None
    ]
    final = complete[-1] if complete else ordered[-1]
    calls = _number(final.get("net_call_premium")); puts = _number(final.get("net_put_premium"))
    return {
        "quality": "observed",
        "provider_date": final.get("date"),
        "final_timestamp": final.get("timestamp"),
        "net_call_premium": calls,
        "net_put_premium": puts,
        "call_minus_put_premium": calls - puts if calls is not None and puts is not None else None,
        "net_volume": _number(final.get("net_volume")),
        "caveat": "Tide is an options-demand regime indicator, not a standalone directional forecast.",
    }


def summarize_stock_state(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    row = rows[-1] if rows else {}
    close = _number(row.get("close")); previous = _number(row.get("prev_close"))
    return {
        "quality": "observed" if row else "empty",
        "market_time": row.get("market_time"),
        "tape_time": row.get("tape_time"),
        "price": close,
        "previous_close": previous,
        "change_pct": close / previous - 1 if close is not None and previous not in {None, 0.0} else None,
        "volume": _number(row.get("volume")),
        "total_volume": _number(row.get("total_volume")),
        "caveat": "Stock state can be premarket or intraday. It does not replace the latest completed regular-session close.",
    }


def summarize_option_price_levels(
    rows: Sequence[Mapping[str, Any]], *, spot: float | None
) -> dict[str, Any]:
    points = [
        (_number(row.get("price")), _number(row.get("call_volume")) or 0.0, _number(row.get("put_volume")) or 0.0)
        for row in rows
    ]
    points = [(price, calls, puts) for price, calls, puts in points if price is not None]
    if not points:
        return {"quality": "empty"}
    calls = sum(item[1] for item in points); puts = sum(item[2] for item in points)
    total = calls + puts
    dominant = max(points, key=lambda item: item[1] + item[2])
    call_peak = max(points, key=lambda item: item[1])
    put_peak = max(points, key=lambda item: item[2])
    near = [item for item in points if spot and abs(item[0] / spot - 1) <= 0.02]
    near_calls = sum(item[1] for item in near); near_puts = sum(item[2] for item in near)
    return {
        "quality": "observed", "level_count": len(points),
        "call_volume": calls, "put_volume": puts,
        "call_minus_put_share": (calls - puts) / total if total else None,
        "dominant_activity_price": dominant[0],
        "dominant_activity_share": (dominant[1] + dominant[2]) / total if total else None,
        "call_peak_price": call_peak[0], "put_peak_price": put_peak[0],
        "near_spot_call_volume_2pct": near_calls, "near_spot_put_volume_2pct": near_puts,
        "near_spot_call_minus_put_share": (
            (near_calls - near_puts) / (near_calls + near_puts) if near_calls + near_puts else None
        ),
        "caveat": "Volume concentration locates activity. It does not establish opening direction, buyer intent, or support/resistance.",
    }


def summarize_volatility_diagnostics(
    anomaly_rows: Sequence[Mapping[str, Any]],
    character_rows: Sequence[Mapping[str, Any]],
    premium_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    anomaly_container = anomaly_rows[-1] if anomaly_rows else {}
    character_container = character_rows[-1] if character_rows else {}
    anomaly = anomaly_container.get("latest") if isinstance(anomaly_container.get("latest"), Mapping) else {}
    character = character_container.get("latest") if isinstance(character_container.get("latest"), Mapping) else {}
    premium = max(premium_rows, key=lambda row: str(row.get("date") or "")) if premium_rows else {}
    premium_rank = _number(premium.get("rank"))
    return {
        "quality": "observed" if anomaly or character or premium else "empty",
        "anomaly_date": anomaly.get("date"), "anomaly_direction": anomaly.get("direction"),
        "anomaly_score": _number(anomaly.get("score")), "anomaly_sample_size": _number(anomaly.get("sample_size")),
        "character_date": character.get("date"), "character": character.get("character"),
        "half_life_days": _number(character.get("half_life_days")), "hurst_rv": _number(character.get("hurst_rv")),
        "variance_risk_premium_date": premium.get("date"),
        "variance_risk_premium": _number(premium.get("risk_premium")),
        "variance_risk_premium_rank": (
            premium_rank * 100 if premium_rank is not None and 0 <= premium_rank <= 1 else premium_rank
        ),
        "caveat": "Provider volatility diagnostics describe dispersion regimes. They can lag and do not predict price direction.",
    }


def summarize_economic_calendar(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted((row for row in rows if row.get("time")), key=lambda row: str(row.get("time")))
    return {
        "quality": "observed" if ordered else "empty",
        "event_count": len(ordered),
        "first_event_time": ordered[0].get("time") if ordered else None,
        "first_event": ordered[0].get("event") if ordered else None,
        "events": [
            {"time": row.get("time"), "event": row.get("event"), "type": row.get("type"),
             "forecast": row.get("forecast"), "previous": row.get("prev")}
            for row in ordered[:8]
        ],
        "caveat": "Calendar rows flag scheduled macro risk. Forecasts can be missing or revised.",
    }


def build_enhanced_summary(
    database: str | Path,
    *,
    snapshot_ids: Sequence[int] | None = None,
    cutoff_at: datetime | None = None,
) -> dict[str, Any]:
    """Read the latest immutable enhanced snapshots and derive source-linked summaries."""

    cutoff_text: str | None = None
    if cutoff_at is not None:
        if cutoff_at.tzinfo is None or cutoff_at.utcoffset() is None:
            raise ValueError("cutoff_at must be timezone-aware")
        cutoff_text = cutoff_at.astimezone(UTC).isoformat()

    path = Path(database).resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        records = connection.execute("""
            SELECT s.id, s.symbol, s.dataset, s.metadata_json, p.content_json
            FROM snapshots s JOIN raw_payloads p ON p.content_hash=s.raw_payload_hash
            WHERE json_extract(s.metadata_json, '$.capture_mode')='enhanced_current'
              AND (? IS NULL OR s.retrieved_at <= ?)
            ORDER BY s.id DESC
        """, (cutoff_text, cutoff_text)).fetchall()
        prices: dict[str, float] = {}
        ohlc = connection.execute("""
            SELECT s.symbol, p.content_json FROM snapshots s
            JOIN raw_payloads p ON p.content_hash=s.raw_payload_hash
            WHERE s.dataset='ohlc' AND (? IS NULL OR s.retrieved_at <= ?)
            ORDER BY s.id DESC
        """, (cutoff_text, cutoff_text)).fetchall()
        for record in ohlc:
            symbol = str(record["symbol"] or "")
            if symbol in prices:
                continue
            bars = _rows(json.loads(record["content_json"]))
            dated = [bar for bar in bars if bar.get("date") or bar.get("end_time")]
            if dated:
                close = _number(max(dated, key=lambda bar: str(bar.get("date") or bar.get("end_time"))).get("close"))
                if close is not None:
                    prices[symbol] = close
    finally:
        connection.close()

    allowed_ids = None if snapshot_ids is None else frozenset(snapshot_ids)
    latest: dict[tuple[str, str], tuple[int, list[dict[str, Any]]]] = {}
    for record in records:
        if allowed_ids is not None and int(record["id"]) not in allowed_ids:
            continue
        metadata = json.loads(record["metadata_json"])
        dataset = str(metadata.get("enhanced_dataset") or "")
        symbol = str(record["symbol"] or "")
        key = (symbol, dataset)
        if key in latest:
            continue
        payload = json.loads(record["content_json"])
        latest[key] = (int(record["id"]), _rows(payload, field="si" if dataset == "short_volume" else "data"))

    contexts: dict[str, Any] = {}
    for symbol, dataset in (
        ("MARKET", "market_tide"),
        ("SECTOR:TECHNOLOGY", "sector_tide_technology"),
        ("SECTOR:COMMUNICATION_SERVICES", "sector_tide_communication_services"),
        ("ETF:QQQ", "etf_tide_qqq"),
        ("ETF:SMH", "etf_tide_smh"),
        ("ETF:SOXX", "etf_tide_soxx"),
    ):
        source = latest.get((symbol, dataset))
        if source:
            contexts[dataset] = {"source_snapshot_id": source[0], **summarize_tide(source[1])}

    calendar_source = latest.get(("MARKET:CALENDAR", "economic_calendar"))
    if calendar_source:
        contexts["economic_calendar"] = {
            "source_snapshot_id": calendar_source[0],
            **summarize_economic_calendar(calendar_source[1]),
        }

    symbols = sorted({
        symbol for symbol, _dataset in latest
        if symbol and not symbol.startswith(("SECTOR:", "ETF:", "MARKET:")) and symbol != "MARKET"
    })
    output: dict[str, Any] = {}
    for symbol in symbols:
        def source(name: str) -> tuple[int | None, list[dict[str, Any]]]:
            return latest.get((symbol, name), (None, []))

        state_id, state = source("stock_state")
        levels_id, levels = source("option_price_levels")
        gex_id, gex = source("greek_exposure_strike")
        flow_id, flow = source("greek_flow")
        term_id, term = source("iv_term_structure")
        stats_id, stats = source("volatility_stats")
        interp_id, interp = source("interpolated_iv")
        anomaly_id, anomaly = source("volatility_anomaly")
        character_id, character = source("volatility_character")
        premium_id, premium = source("variance_risk_premium")
        dark_id, dark = source("dark_pool_levels")
        interest_id, interest = source("short_interest")
        borrow_id, borrow = source("short_borrow")
        volume_id, volume = source("short_volume")
        output[symbol] = {
            "reference_price": prices.get(symbol),
            "sources": {
                "stock_state": state_id, "option_price_levels": levels_id,
                "greek_exposure_strike": gex_id, "greek_flow": flow_id,
                "iv_term_structure": term_id, "volatility_stats": stats_id,
                "interpolated_iv": interp_id, "dark_pool_levels": dark_id,
                "volatility_anomaly": anomaly_id, "volatility_character": character_id,
                "variance_risk_premium": premium_id,
                "short_interest": interest_id, "short_borrow": borrow_id,
                "short_volume": volume_id,
            },
            "stock_state": summarize_stock_state(state),
            "option_price_levels": summarize_option_price_levels(levels, spot=prices.get(symbol)),
            "greek_exposure": summarize_greek_exposure(gex, spot=prices.get(symbol)),
            "greek_flow": summarize_greek_flow(flow),
            "volatility": summarize_volatility(term, stats, interp),
            "volatility_diagnostics": summarize_volatility_diagnostics(anomaly, character, premium),
            "dark_pool_levels": summarize_dark_pool(dark, spot=prices.get(symbol)),
            "short_crowding": summarize_short(interest, borrow, volume),
        }
    return {
        "schema": "morning_edge/enhanced_summary/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "recommendations_enabled": False,
        "symbols": output,
        "contexts": contexts,
    }
