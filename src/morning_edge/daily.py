"""Cutoff-bound, non-actionable daily Codex Screener run artifacts.

The orchestrator in this module joins an immutable current-capture report with
the local snapshot archive.  It is intentionally a *read model*: it never
contacts a provider, changes source data, invokes a model, or grants execution
authority.  A caller may render the returned JSON in a dashboard, but all
trade-facing gates default to false until a separate validation/calibration
process explicitly changes the governing policy.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, date, datetime
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .agent_analysis import (
    ANALYSIS_SCHEMA_VERSION,
    AnalysisCritic,
    DeterministicAnalystBackend,
    EvidenceBundle as AnalystEvidenceBundle,
    EvidenceFeature,
    EvidenceSource,
    FeatureStatus,
)
from .current_collection import CurrentCaptureReport, CurrentCaptureStatus
from .edge import EDGE_FEATURE_VERSION, EdgeAnalyzer, option_mechanics
from .freshness import dataset_freshness
from .models import payload_digest, timestamp_from_text, timestamp_text, utc_timestamp
from .normalization import EvidenceBundle, EvidenceReader, SourceRef
from .option_research import shadow_option_research


MORNING_RUN_SCHEMA_VERSION = "morning_run/v1"
FEATURE_VERSION = "daily-evidence-v2"
QUOTE_FRESHNESS_SECONDS = 5 * 60
MAX_REFERENCE_SPREAD_PCT = 0.25
MAX_REFERENCE_CONTRACTS_PER_SIDE = 6
MARKET_TIMEZONE = ZoneInfo("America/New_York")
_OPTION_SYMBOL_EXPIRY = re.compile(r"^[A-Z.\-]+(\d{6})[CP]\d+$")


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _rows(payload: Any) -> list[Mapping[str, Any]]:
    data = payload.get("data", payload) if isinstance(payload, Mapping) else payload
    return [row for row in data if isinstance(row, Mapping)] if isinstance(data, list) else []


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Providers commonly send millisecond Unix timestamps.
        seconds = float(value) / 1000 if value > 100_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _expiry(row: Mapping[str, Any]) -> date | None:
    for key in ("expiry", "expiration", "expiration_date", "exp_date"):
        value = row.get(key)
        if isinstance(value, str):
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                continue
    symbol = row.get("option_symbol")
    if isinstance(symbol, str):
        match = _OPTION_SYMBOL_EXPIRY.match(symbol.strip().upper())
        if match:
            try:
                return datetime.strptime(match.group(1), "%y%m%d").date()
            except ValueError:
                pass
    return None


def _source_lookup(database: str | Path, source_refs: Iterable[SourceRef]) -> dict[int, EvidenceSource]:
    ids = sorted({item.snapshot_id for item in source_refs})
    if not ids:
        return {}
    connection = sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"SELECT id, dataset, as_of, retrieved_at, raw_payload_hash FROM snapshots WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    finally:
        connection.close()
    return {
        int(row["id"]): EvidenceSource(
            source_id=f"snapshot-{row['id']}", snapshot_id=int(row["id"]), dataset=row["dataset"],
            as_of=timestamp_from_text(row["as_of"]), retrieved_at=timestamp_from_text(row["retrieved_at"]),
            payload_hash=row["raw_payload_hash"],
        )
        for row in rows
    }


def _analyst_bundle(database: str | Path, evidence: EvidenceBundle, *, analysis_id: str) -> AnalystEvidenceBundle | None:
    sources_by_id = _source_lookup(database, evidence.source_refs)
    sources = tuple(sources_by_id[item.snapshot_id] for item in evidence.source_refs if item.snapshot_id in sources_by_id)
    if not sources:
        return None
    latest_by_dataset: dict[str, EvidenceSource] = {}
    for source in sources:
        latest_by_dataset[source.dataset] = source

    fields: list[tuple[str, Any, str, str]] = []
    if evidence.price is not None:
        for key, value in asdict(evidence.price).items():
            if value is not None:
                fields.append((f"price.{key}", value, "ratio" if "return" in key or "pct" in key or "vol" in key else "value", "ohlc"))
    for key, value in asdict(evidence.chain).items():
        if value is not None and key != "latest_market_date":
            fields.append((f"chain.{key}", value, "contracts" if "count" in key else "value", "option_chain"))
    for key, value in asdict(evidence.gex).items():
        if value is not None and key != "latest_market_date":
            fields.append((f"gex.{key}", value, "price", "dealer_exposure"))
    features: list[EvidenceFeature] = []
    for feature_id, value, unit, dataset in fields:
        source = latest_by_dataset.get(dataset)
        if source:
            features.append(EvidenceFeature(
                feature_id, value, unit, "immutable snapshot normalization", FEATURE_VERSION,
                # A feature cannot be available before the source was retrieved.
                (source.source_id,), source.retrieved_at, 0.75, FeatureStatus.VALID,
            ))
    if not features:
        return None
    return AnalystEvidenceBundle(
        analysis_id=analysis_id, ticker=evidence.ticker, cutoff_at=evidence.cutoff_at,
        feature_version=FEATURE_VERSION, sources=sources, features=tuple(features),
        execution_ready=False, calibration_ready=False, buy_authorized=False,
    )


def _latest_chain_snapshot(database: str | Path, ticker: str, cutoff_at: datetime) -> tuple[int, datetime, Any] | None:
    connection = sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT s.id, s.retrieved_at, p.content_json
            FROM snapshots s JOIN raw_payloads p ON p.content_hash=s.raw_payload_hash
            WHERE s.symbol=? AND s.dataset='option_chain' AND s.as_of<=? AND s.retrieved_at<=?
            ORDER BY s.retrieved_at DESC, s.id DESC LIMIT 1
            """,
            (ticker, timestamp_text(cutoff_at), timestamp_text(cutoff_at)),
        ).fetchone()
    finally:
        connection.close()
    return (int(row["id"]), timestamp_from_text(row["retrieved_at"]), json.loads(row["content_json"])) if row else None


def _reference_contracts(database: str | Path, ticker: str, cutoff_at: datetime, latest_close: float | None) -> list[dict[str, Any]]:
    selected = _latest_chain_snapshot(database, ticker, cutoff_at)
    if selected is None:
        return []
    snapshot_id, retrieved_at, payload = selected
    candidates: list[tuple[float, dict[str, Any]]] = []
    for row in _rows(payload):
        option_type = str(row.get("option_type", row.get("type", ""))).lower()
        strike = _finite_number(row.get("strike"))
        bid = _finite_number(row.get("nbbo_bid", row.get("bid")))
        ask = _finite_number(row.get("nbbo_ask", row.get("ask")))
        delta = _finite_number(row.get("delta"))
        gamma = _finite_number(row.get("gamma"))
        theta = _finite_number(row.get("theta"))
        vega = _finite_number(row.get("vega"))
        oi = _finite_number(row.get("open_interest"))
        expiry = _expiry(row)
        if option_type not in {"call", "put"} or strike is None or strike <= 0 or bid is None or ask is None or bid <= 0 or ask < bid:
            continue
        if any(value is None for value in (delta, gamma, theta, vega, oi)) or oi < 1 or expiry is None:
            continue
        dte = (expiry - cutoff_at.astimezone(MARKET_TIMEZONE).date()).days
        if not 30 <= dte <= 240:
            continue
        quote_at = _timestamp(row.get("last_tape_time"))
        quote_age = (cutoff_at - quote_at).total_seconds() if quote_at is not None else None
        fresh = quote_age is not None and 0 <= quote_age <= QUOTE_FRESHNESS_SECONDS
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid if mid > 0 else None
        # This is a reference screen, not a liquidity prediction. Exclude
        # obviously wide markets instead of allowing them into a selection.
        if spread_pct is None or spread_pct > MAX_REFERENCE_SPREAD_PCT:
            continue
        # A neutral, liquid reference selection: no direction is inferred.
        liquidity_penalty = spread_pct
        delta_distance = abs(abs(delta) - 0.5)
        price_distance = abs(strike - latest_close) / latest_close if latest_close and latest_close > 0 else 0.5
        score = delta_distance + price_distance + liquidity_penalty - min(oi, 10_000) / 1_000_000
        candidates.append((score, {
            "contract": row.get("option_symbol"), "option_type": option_type.upper(), "expiry": expiry.isoformat(),
            "dte": dte, "strike": strike, "bid": bid, "ask": ask, "mid": mid,
            "spread_pct_of_mid": spread_pct, "delta": delta, "gamma": gamma,
            "theta": theta, "vega": vega, "implied_volatility": _finite_number(row.get("implied_volatility", row.get("iv"))),
            "open_interest": int(oi), "volume": int(_finite_number(row.get("volume")) or 0),
            "quote_at": timestamp_text(quote_at) if quote_at else None, "quote_age_seconds": round(quote_age, 3) if quote_age is not None else None,
            "quote_fresh": fresh, "source_snapshot_id": snapshot_id, "source_retrieved_at": timestamp_text(retrieved_at),
            "status": "NOT_ELIGIBLE", "reason": "Reference-only contract; no calibrated directional thesis or execution validation.",
        }))
    # Preserve a bounded liquid universe on both sides. Directional selection
    # happens later and remains shadow-only/non-executable.
    result: list[dict[str, Any]] = []
    side_counts = {"CALL": 0, "PUT": 0}
    seen_contracts: set[str] = set()
    for score, contract in sorted(candidates, key=lambda item: (item[0], str(item[1].get("contract")))):
        side = str(contract["option_type"])
        identity = str(contract.get("contract") or "")
        if side_counts.get(side, 0) >= MAX_REFERENCE_CONTRACTS_PER_SIDE or identity in seen_contracts:
            continue
        contract["screen_rank_within_side"] = side_counts[side] + 1
        contract["screen_score"] = round(score, 6)
        result.append(contract)
        side_counts[side] += 1
        seen_contracts.add(identity)
    return sorted(result, key=lambda row: (str(row["option_type"]), int(row["screen_rank_within_side"])))


def _ema_series(values: Sequence[float], window: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < window:
        return result
    current = sum(values[:window]) / window
    result[window - 1] = current
    weight = 2 / (window + 1)
    for index in range(window, len(values)):
        current = values[index] * weight + current * (1 - weight)
        result[index] = current
    return result


def _rsi14(values: Sequence[float]) -> float | None:
    if len(values) < 15:
        return None
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    gains = [max(0.0, item) for item in changes[:14]]
    losses = [max(0.0, -item) for item in changes[:14]]
    average_gain = sum(gains) / 14
    average_loss = sum(losses) / 14
    for change in changes[14:]:
        average_gain = (average_gain * 13 + max(0.0, change)) / 14
        average_loss = (average_loss * 13 + max(0.0, -change)) / 14
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    return 100 - 100 / (1 + average_gain / average_loss)


def _technical_view(reader: EvidenceReader, ticker: str, cutoff_at: datetime, evidence: EvidenceBundle) -> dict[str, Any]:
    bars = reader.normalize_bars(ticker, cutoff_at=cutoff_at)
    closes = [bar.close for bar in bars]
    ema20, ema50 = _ema_series(closes, 20), _ema_series(closes, 50)
    trailing = closes[-126:]
    drawdown = trailing[-1] / max(trailing) - 1 if trailing and max(trailing) > 0 else None
    return_1d = closes[-1] / closes[-2] - 1 if len(closes) > 1 and closes[-2] > 0 else None
    return {
        "latest_regular_close": evidence.price.latest_close if evidence.price else None,
        "latest_regular_session": bars[-1].session_date.isoformat() if bars else None,
        "observations": len(bars),
        "bars": [
            {"date": bar.session_date.isoformat(), "close": bar.close, "ema20": ema20[index], "ema50": ema50[index]}
            for index, bar in enumerate(bars)
        ],
        "return_1d_pct": return_1d * 100 if return_1d is not None else None,
        "return_5d": evidence.price.return_5d if evidence.price else None,
        "return_20d": evidence.price.return_20d if evidence.price else None,
        "return_63d": evidence.price.return_63d if evidence.price else None,
        "ema20": evidence.price.ema_20 if evidence.price else None,
        "ema50": evidence.price.ema_50 if evidence.price else None,
        "ema_20": evidence.price.ema_20 if evidence.price else None,
        "ema_50": evidence.price.ema_50 if evidence.price else None,
        "ema_200": evidence.price.ema_200 if evidence.price else None,
        "realized_vol_20": evidence.price.realized_vol_20 if evidence.price else None,
        "rv20_ann_pct": evidence.price.realized_vol_20 * 100 if evidence.price and evidence.price.realized_vol_20 is not None else None,
        "rsi14": _rsi14(closes),
        "atr_14_pct": evidence.price.atr_14_pct if evidence.price else None,
        "volume_ratio_20": evidence.price.volume_ratio_20 if evidence.price else None,
        "drawdown_126d_pct": drawdown * 100 if drawdown is not None else None,
        "interpretation": "Historical regular-session reference only; not an intraday prediction.",
    }


def _collection_status(report: CurrentCaptureReport, ticker: str) -> dict[str, Any]:
    entries = [item for item in report.results if item.ticker == ticker]
    return {
        "preflight_passed": report.preflight_passed,
        "recommendations_enabled": False,
        "datasets": [
            {"dataset": item.dataset.value, "status": item.status.value, "snapshot_id": item.snapshot_id,
             "fetched_at": item.fetched_at, "row_count": item.row_count, "reason": item.reason}
            for item in entries
        ],
        "captured_count": sum(item.status is CurrentCaptureStatus.CAPTURED for item in entries),
        "incomplete_count": sum(item.status is not CurrentCaptureStatus.CAPTURED for item in entries),
    }


def _provider_names(database: str | Path, source_refs: Sequence[SourceRef]) -> list[str]:
    ids = sorted({item.snapshot_id for item in source_refs})
    if not ids:
        return []
    connection = sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            f"SELECT DISTINCT provider FROM snapshots WHERE id IN ({','.join('?' for _ in ids)}) ORDER BY provider",
            ids,
        ).fetchall()
    finally:
        connection.close()
    return [str(row[0]) for row in rows]


def _capture_status(collection: Mapping[str, Any], dataset: str) -> str:
    for item in collection["datasets"]:
        if item["dataset"] == dataset:
            return str(item["status"])
    return "not_collected"


def _analyst_view(analysis: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "summary": analysis.get("evidence_summary"),
        "status": analysis.get("status"),
        "claims": [item.get("statement") for item in analysis.get("claims", ()) if isinstance(item, Mapping) and isinstance(item.get("statement"), str)],
        "counterevidence": [item.get("counterevidence_summary") for item in analysis.get("claims", ()) if isinstance(item, Mapping) and isinstance(item.get("counterevidence_summary"), str)],
        "unknowns": [item.get("reason") for item in analysis.get("unknowns", ()) if isinstance(item, Mapping) and isinstance(item.get("reason"), str)],
        "note": "Narrative is evidence-bound and non-directional in shadow mode.",
    }


def _why_today(technical: Mapping[str, Any], edge: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Summarize observed changes without converting them into trade advice."""

    items: list[dict[str, Any]] = []
    return_1d = _finite_number(technical.get("return_1d_pct"))
    if return_1d is not None:
        items.append({"factor": "price", "change": return_1d, "unit": "percent", "note": "Latest regular-session return"})
    surface = edge.get("option_surface", {}) if isinstance(edge.get("option_surface"), Mapping) else {}
    history = surface.get("history", ()) if isinstance(surface.get("history"), list) else ()
    if len(history) >= 2:
        current_iv = _finite_number(history[-1].get("front_iv")); previous_iv = _finite_number(history[-2].get("front_iv"))
        if current_iv is not None and previous_iv is not None:
            items.append({"factor": "front_iv", "change": current_iv - previous_iv, "unit": "volatility", "note": "ATM front-expiry IV change"})
    gex = edge.get("gex_topology", {}) if isinstance(edge.get("gex_topology"), Mapping) else {}
    for field, label in (("flip_change", "gamma_flip"), ("call_wall_change", "call_wall"), ("put_wall_change", "put_wall")):
        value = _finite_number(gex.get(field))
        if value is not None:
            items.append({"factor": label, "change": value, "unit": "price", "note": "Provider positioning-level migration"})
    dark = edge.get("dark_pool", {}) if isinstance(edge.get("dark_pool"), Mapping) else {}
    if (value := _finite_number(dark.get("dominant_level_change"))) is not None:
        items.append({"factor": "dark_pool_level", "change": value, "unit": "price", "note": "Dominant premium-weighted print-level migration"})
    oi = edge.get("open_interest", {}) if isinstance(edge.get("open_interest"), Mapping) else {}
    if (value := _finite_number(oi.get("net_call_minus_put_change"))) is not None:
        items.append({"factor": "call_minus_put_oi", "change": value, "unit": "contracts", "note": "Consecutive full-chain OI difference"})
    return items


def _trade_thesis(
    *, technical: Mapping[str, Any], edge: Mapping[str, Any],
    contracts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Create a directional, still-fail-closed thesis and option reference."""

    dimensions = edge.get("dimensions", {}) if isinstance(edge.get("dimensions"), Mapping) else {}
    forecast = edge.get("forecast", {}) if isinstance(edge.get("forecast"), Mapping) else {}
    directional = float(_finite_number(dimensions.get("directional_edge")) or 50.0)
    forecast_direction = str(forecast.get("direction", "NEUTRAL"))
    direction = forecast_direction if forecast_direction in {"BULLISH", "BEARISH"} else (
        "BULLISH" if directional >= 57 else "BEARISH" if directional <= 43 else "NEUTRAL"
    )
    direction_strength = abs(directional - 50) * 2
    evidence_quality = float(_finite_number(dimensions.get("evidence_quality")) or 0.0)
    positioning = float(_finite_number(dimensions.get("positioning_context")) or 50.0)
    positioning_alignment = positioning if direction == "BULLISH" else 100 - positioning if direction == "BEARISH" else 50.0
    tradeability = float(_finite_number(dimensions.get("tradeability")) or 0.0)
    analog_frequency = _finite_number(forecast.get("directional_analog_frequency"))
    # directional_analog_frequency is already expressed in the selected
    # direction. Values below 50% conflict with that direction and must reduce,
    # not increase, the thesis score.
    analog_alignment = (float(analog_frequency) - .5) * 200 if analog_frequency is not None else 0.0
    catalyst_risk = float(_finite_number(dimensions.get("catalyst_risk")) or 0.0)
    raw_score = (
        .36 * direction_strength + .22 * evidence_quality + .16 * positioning_alignment
        + .16 * tradeability + .10 * analog_alignment
    )
    technical_direction = "BULLISH" if directional >= 57 else "BEARISH" if directional <= 43 else "NEUTRAL"
    if (
        forecast_direction in {"BULLISH", "BEARISH"}
        and technical_direction in {"BULLISH", "BEARISH"}
        and forecast_direction != technical_direction
    ):
        raw_score *= .62
    if forecast.get("status") != "EXPERIMENTAL_UNCALIBRATED":
        raw_score *= .72
    analogs = edge.get("historical_analogs", {}) if isinstance(edge.get("historical_analogs"), Mapping) else {}
    stability = analogs.get("stability", {}) if isinstance(analogs.get("stability"), Mapping) else {}
    robustness_adjustments: list[str] = []
    if stability.get("status") == "SENSITIVE":
        raw_score *= .85
        robustness_adjustments.append("leave-one-out analog sensitivity")
    analog_sample = int(_finite_number(analogs.get("sample_size")) or 0)
    if 0 < analog_sample < 8:
        raw_score *= .90
        robustness_adjustments.append("fewer than eight independent analogs")
    if analogs.get("historical_disposition") == "MIXED" and direction in {"BULLISH", "BEARISH"}:
        raw_score *= .90
        robustness_adjustments.append("mixed historical disposition")
    if direction == "NEUTRAL":
        raw_score *= .55
    if catalyst_risk >= 85:
        raw_score *= .88
    conviction = round(max(0.0, min(100.0, raw_score)), 1)

    wanted_type = "call" if direction == "BULLISH" else "put" if direction == "BEARISH" else None
    eligible = [item for item in contracts if str(item.get("option_type", "")).lower() == wanted_type]

    def option_fit(item: Mapping[str, Any]) -> float:
        spread = float(_finite_number(item.get("spread_pct_of_mid")) or 1.0)
        delta = abs(float(_finite_number(item.get("delta")) or 0.0))
        dte = float(_finite_number(item.get("dte")) or 0.0)
        oi = max(0.0, float(_finite_number(item.get("open_interest")) or 0.0))
        spread_score = max(0.0, 1 - spread / MAX_REFERENCE_SPREAD_PCT)
        delta_score = max(0.0, 1 - abs(delta - .45) / .35)
        dte_score = max(0.0, 1 - abs(dte - 75) / 150)
        oi_score = min(1.0, oi / 1000)
        return .40 * spread_score + .25 * delta_score + .20 * dte_score + .15 * oi_score

    option = dict(max(eligible, key=option_fit)) if eligible else None
    option_reference = None
    if option is not None:
        mechanics = option.get("mechanics", {}) if isinstance(option.get("mechanics"), Mapping) else {}
        option_reference = {
            "contract": option.get("contract"), "type": wanted_type.upper(),
            "expiry": option.get("expiry"), "dte": option.get("dte"), "strike": option.get("strike"),
            "bid": option.get("bid"), "ask": option.get("ask"), "spread_pct": option.get("spread_pct_of_mid"),
            "delta": option.get("delta"), "open_interest": option.get("open_interest"), "volume": option.get("volume"),
            "fit_score": round(option_fit(option) * 100, 1),
            "breakeven": mechanics.get("breakeven"), "breakeven_move_pct": mechanics.get("breakeven_move_pct"),
            "status": "MODEL_SELECTED_REFERENCE_NOT_EXECUTABLE",
            "reason": "Matches thesis direction and best balances stored spread, delta, DTE, and OI among displayed contracts.",
        }

    oi = edge.get("open_interest", {}) if isinstance(edge.get("open_interest"), Mapping) else {}
    flow = edge.get("flow_conviction", {}) if isinstance(edge.get("flow_conviction"), Mapping) else {}
    surface = edge.get("option_surface", {}) if isinstance(edge.get("option_surface"), Mapping) else {}
    surface_history = surface.get("history", ()) if isinstance(surface.get("history"), list) else ()
    iv_change = None
    if len(surface_history) >= 2:
        current_iv = _finite_number(surface_history[-1].get("front_iv")); prior_iv = _finite_number(surface_history[-2].get("front_iv"))
        if current_iv is not None and prior_iv is not None:
            iv_change = current_iv - prior_iv
    oi_strike = oi.get("largest_call_build_strike") if direction == "BULLISH" else oi.get("largest_put_build_strike")
    return {
        "status": "CONDITIONAL_RESEARCH_ONLY", "direction": direction,
        "conviction_score": conviction, "conviction_is_probability": False,
        "score_label": "uncalibrated evidence score",
        "robustness_adjustments": robustness_adjustments,
        "expected_return_20d": forecast.get("center_return_20d"),
        "range_low_20d": forecast.get("low_return_20d"), "range_high_20d": forecast.get("high_return_20d"),
        "forecast_status": forecast.get("status", "UNAVAILABLE"),
        "option_reference": option_reference,
        "unusual_oi": {
            "status": oi.get("status", "UNAVAILABLE"), "directional_build_strike": oi_strike,
            "call_change": oi.get("call_oi_change"), "put_change": oi.get("put_oi_change"),
            "note": "Consecutive-chain OI change confirms contract count changes, not buyer intent.",
        },
        "premium_expansion": {
            "status": "AVAILABLE" if int(flow.get("history_sessions", 0)) >= 20 else "INSUFFICIENT_FLOW_HISTORY",
            "directional_premium": flow.get("directional_premium"),
            "directional_percentile": flow.get("directional_percentile"),
            "iv_change": iv_change,
            "note": "Premium anomaly needs at least 20 comparable flow sessions; IV change is shown separately.",
        },
        "trigger_reference": (
            "Require fresh price acceptance above the stored EMA20/GEX flip region."
            if direction == "BULLISH" else
            "Require fresh price acceptance below the stored EMA20/GEX flip region."
            if direction == "BEARISH" else "Wait for directional state separation."
        ),
        "invalidation_reference": (
            "Invalidate if fresh price loses the stored EMA20/GEX flip region."
            if direction == "BULLISH" else
            "Invalidate if fresh price reclaims the stored EMA20/GEX flip region."
            if direction == "BEARISH" else "No directional thesis is active."
        ),
        "limitations": [
            "Conviction is an uncalibrated thesis score, not chance of profit.",
            "Option quote is prior-session/reference data and is not executable.",
            "Fresh price, spread, and calibration gates remain false.",
        ],
    }


def _ticker_record(
    reader: EvidenceReader, edge_analyzer: EdgeAnalyzer, database: str | Path,
    report: CurrentCaptureReport, ticker: str, cutoff_at: datetime,
    positions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    evidence = reader.bundle(ticker, cutoff_at=cutoff_at)
    analysis_id = f"{ticker.lower()}-{cutoff_at.strftime('%Y%m%dT%H%M%SZ')}"
    analyst_evidence = _analyst_bundle(database, evidence, analysis_id=analysis_id)
    if analyst_evidence is None:
        analysis: dict[str, Any] = {
            "analysis_schema_version": ANALYSIS_SCHEMA_VERSION, "analysis_id": analysis_id, "status": "ABSTAIN",
            "evidence_summary": "No cutoff-safe source could be promoted to the analyst contract.", "claims": [],
            "scenarios": [], "confidence": {"overall": 0}, "suggested_action": "NO_RECOMMENDATION",
            "unknowns": [{"field_or_question": "evidence", "reason": "No usable source rows.", "blocked_capability": "Grounded analysis", "resolution": "Collect valid source snapshots."}],
            "contradictions": [], "agent_model_id": "deterministic-fallback-v1", "prompt_version": "agent-contract-v1",
        }
    else:
        result = DeterministicAnalystBackend().analyze(analyst_evidence)
        AnalysisCritic.validate(analyst_evidence, result)
        analysis = result.to_dict()
    technical = _technical_view(reader, ticker, cutoff_at, evidence)
    edge = edge_analyzer.analyze(ticker, cutoff_at, technical=technical)
    collection = _collection_status(report, ticker)
    contracts = _reference_contracts(database, ticker, cutoff_at, technical["latest_regular_close"])
    for contract in contracts:
        contract["mechanics"] = option_mechanics(
            contract, spot=technical["latest_regular_close"], cutoff_at=cutoff_at,
        )
    trade_thesis = _trade_thesis(technical=technical, edge=edge, contracts=contracts)
    option_research = shadow_option_research(
        direction=str(trade_thesis.get("direction", "NEUTRAL")),
        spot=technical["latest_regular_close"], cutoff_at=cutoff_at,
        contracts=contracts,
        forecast_v4=edge.get("forecast_v4", {}) if isinstance(edge.get("forecast_v4"), Mapping) else {},
    )
    chain_status = _capture_status(collection, "option_chain")
    gex_status = _capture_status(collection, "dealer_exposure")
    freshness = "fresh_reference_quote" if contracts and all(item["quote_fresh"] for item in contracts) else "not_execution_fresh"
    freshness_contract = dataset_freshness(
        cutoff_at=cutoff_at,
        price_session=technical.get("latest_regular_session"),
        dataset_dates={
            "option_chain": edge.get("option_surface", {}).get("market_date"),
            "option_flow": edge.get("flow_conviction", {}).get("market_date"),
            "open_interest": edge.get("open_interest", {}).get("market_date"),
            "gex": edge.get("gex_topology", {}).get("date"),
            "dark_pool": edge.get("dark_pool", {}).get("date"),
        },
    )
    complete = collection["incomplete_count"] == 0 and collection["captured_count"] > 0
    current_snapshot_ids = [
        item["snapshot_id"] for item in collection["datasets"] if item["snapshot_id"] is not None
    ]
    positions_for_ticker = [dict(item) for item in positions if str(item.get("ticker", "")).upper() == ticker]
    return {
        "ticker": ticker,
        "action": "NO_RECOMMENDATION",
        "gates": {"data_ready": False, "calibrated": False, "execution_ready": False},
        "decision": {
            "action": "NO_RECOMMENDATION", "data_ready": False, "calibrated": False, "execution_ready": False,
            "reason": "Daily artifact is a shadow/read-only analysis. Fresh quotes, validated calibration, and explicit execution controls are not established.",
        },
        "collection": collection,
        "coverage_status": "COMPLETE_CAPTURE_NOT_EXECUTION_VALIDATED" if complete else "PARTIAL_OR_UNAVAILABLE_CAPTURE",
        "data_quality": {
            "complete": complete, "freshness": freshness, "chain_status": chain_status, "gex_status": gex_status,
            "exclusions": list(evidence.exclusions), "source_count": len(evidence.source_refs),
            "note": "Completeness reflects bounded raw capture only. It does not establish execution readiness, trade direction, or calibration.",
        },
        "freshness_contract": freshness_contract,
        "price": {
            "value": technical["latest_regular_close"], "as_of": technical["latest_regular_session"],
            "market_time": "regular_session", "status": "PRIOR_SESSION_REFERENCE",
        },
        "return_1d_pct": technical["return_1d_pct"],
        "technical": technical,
        "edge": edge,
        "trade_thesis": trade_thesis,
        "option_research": option_research,
        "why_today": _why_today(technical, edge),
        "evidence": evidence.to_agent_input(),
        "analyst": _analyst_view(analysis),
        "agent_analysis": analysis,
        "options": {"candidates": contracts, "note": f"Up to {MAX_REFERENCE_CONTRACTS_PER_SIDE} contracts per side; spreads above {MAX_REFERENCE_SPREAD_PCT:.0%} of mid are excluded. Every row remains a non-executable reference."},
        "provenance": {
            "as_of": timestamp_text(cutoff_at), "snapshot_ids": current_snapshot_ids,
            "evidence_source_count": len(evidence.source_refs),
            "provider": _provider_names(database, evidence.source_refs),
        },
        "positions": positions_for_ticker,
    }


def _watchlist_regime(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Attach cross-sectional context without pretending the watchlist is the market."""

    returns = [
        value for record in records
        if (value := _finite_number(record.get("technical", {}).get("return_20d"))) is not None
    ]
    one_day = [
        value for record in records
        if (value := _finite_number(record.get("return_1d_pct"))) is not None
    ]
    center = sorted(returns)[len(returns) // 2] if returns else None
    ordered = sorted(returns)
    for record in records:
        value = _finite_number(record.get("technical", {}).get("return_20d"))
        relative = value - center if value is not None and center is not None else None
        rank = None
        if value is not None and ordered:
            rank = (sum(item < value for item in ordered) + 0.5 * sum(item == value for item in ordered)) / len(ordered)
        record["relative_regime"] = {
            "universe": "configured_watchlist_only", "return_20d_minus_watchlist_median": relative,
            "cross_sectional_percentile": rank,
            "note": "This is not beta-adjusted sector alpha until benchmark series are captured.",
        }
        dimensions = record.get("edge", {}).get("dimensions")
        if isinstance(dimensions, dict) and rank is not None:
            dimensions["directional_edge_pre_relative"] = dimensions.get("directional_edge")
            dimensions["directional_edge"] = round(max(0.0, min(100.0, float(dimensions["directional_edge"]) + (rank - 0.5) * 16)), 1)
        if isinstance(dimensions, dict):
            directional = abs(float(dimensions.get("directional_edge", 50)) - 50)
            volatility = abs(float(dimensions.get("long_volatility_attractiveness", 50)) - 50)
            positioning = abs(float(dimensions.get("positioning_context", 50)) - 50)
            catalyst = float(dimensions.get("catalyst_risk", 0))
            quality = float(dimensions.get("evidence_quality", 0))
            strength = min(80.0, directional + volatility * .4 + positioning * .3 + catalyst * .15)
            record["edge"]["attention_score"] = round(min(100.0, quality * (.45 + strength / 120)), 1)
    ranked = sorted(records, key=lambda item: (
        -float(item.get("trade_thesis", {}).get("conviction_score", 0)), item["ticker"],
    ))
    for index, record in enumerate(ranked, start=1):
        record["trade_rank"] = index
    return {
        "scope": "configured_watchlist_only", "ticker_count": len(records),
        "positive_1d_count": sum(item > 0 for item in one_day),
        "median_1d_pct": sorted(one_day)[len(one_day) // 2] if one_day else None,
        "median_20d_return": center,
        "note": "Cross-sectional context is descriptive; QQQ/SMH/SOXX regime capture is not yet present.",
    }


def build_morning_run(
    *, database: str | Path, capture_report: CurrentCaptureReport, tickers: Sequence[str] | None = None,
    cutoff_at: datetime | None = None, positions: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build a JSON-ready shadow artifact from a prior bounded current capture.

    ``capture_report`` is required so a dashboard cannot silently imply that
    uncollected data was available.  ``positions`` are copied as supplied and
    are never interpreted as account authority.
    """

    report_time = utc_timestamp(datetime.fromisoformat(capture_report.generated_at.replace("Z", "+00:00")), field_name="capture_report.generated_at")
    if cutoff_at is None:
        fetched_times = [_timestamp(item.fetched_at) for item in capture_report.results if item.fetched_at]
        cutoff = max([report_time, *(item for item in fetched_times if item is not None)])
    else:
        cutoff = utc_timestamp(cutoff_at, field_name="cutoff_at")
    symbols = tuple(dict.fromkeys((tickers or capture_report.tickers)))
    if not symbols:
        raise ValueError("at least one ticker is required")
    symbols = tuple(str(item).strip().upper() for item in symbols)
    if any(not item for item in symbols):
        raise ValueError("ticker must not be empty")
    with EvidenceReader(database) as reader, EdgeAnalyzer(database) as edge_analyzer:
        records = [
            _ticker_record(reader, edge_analyzer, database, capture_report, ticker, cutoff, positions)
            for ticker in symbols
        ]
    regime = _watchlist_regime(records)
    run_key = {"schema": MORNING_RUN_SCHEMA_VERSION, "cutoff_at": timestamp_text(cutoff), "tickers": list(symbols), "capture_snapshot_ids": [item.snapshot_id for item in capture_report.results if item.snapshot_id is not None]}
    return {
        "run_schema_version": MORNING_RUN_SCHEMA_VERSION,
        "run_id": f"morning-{cutoff.strftime('%Y%m%dT%H%M%SZ')}-{payload_digest(run_key)[:12]}",
        "generated_at": timestamp_text(cutoff), "cutoff_at": timestamp_text(cutoff), "mode": "SHADOW_READ_ONLY",
        "recommendations_enabled": False,
        "global_gates": {"data_ready": False, "calibrated": False, "execution_ready": False},
        "global_blockers": ["No validated calibration artifact is registered.", "No fresh independently verified executable quotes are established.", "This run does not authorize a trade."],
        "edge_feature_version": EDGE_FEATURE_VERSION,
        "watchlist_regime": regime,
        "capture_report": capture_report.to_dict(), "watchlist": records,
    }


def write_morning_run(path: str | Path, artifact: Mapping[str, Any]) -> Path:
    """Write an artifact atomically with owner-only permissions where supported."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(artifact, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        with open(temporary, "xb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination
