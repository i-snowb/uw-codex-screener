"""Walk-forward evaluation for published Codex Screener shadow forecasts.

The harness freezes each published thesis in the existing append-only forecast
ledger.  It later scores a forecast only from a subsequent stored run that
contains the required regular-session close.  Reference-option returns use the
published ask as entry and a later stored bid as exit.  Missing quotes remain
missing; they are never converted to zero returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
import math
from pathlib import Path
import statistics
from typing import Any, Iterable, Mapping, Sequence

from .ledger import ForecastLedger, ForecastRecord, OutcomeRecord
from .models import timestamp_from_text, timestamp_text


EVALUATION_VERSION = "walk-forward-shadow-v2"
HORIZONS = (1, 5, 10, 20)
MINIMUM_EVALUATIONS_PER_HORIZON = 60
MINIMUM_ORIGIN_SESSIONS = 60
INTERVAL_ALPHA = 0.20


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def _number(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _text(value: object, default: str = "") -> str:
    text = str(value).strip() if value is not None else ""
    return text or default


def _median(values: Iterable[float]) -> float | None:
    finite = [float(value) for value in values if math.isfinite(value)]
    return statistics.median(finite) if finite else None


def _quantile(values: Iterable[float], probability: float) -> float | None:
    ordered = sorted(float(value) for value in values if math.isfinite(value))
    if not ordered:
        return None
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _wilson_interval(successes: int, total: int, *, z: float = 1.959963984540054) -> dict[str, float] | None:
    if total < 1 or successes < 0 or successes > total:
        return None
    proportion = successes / total
    denominator = 1.0 + z * z / total
    center = (proportion + z * z / (2.0 * total)) / denominator
    radius = z * math.sqrt(
        proportion * (1.0 - proportion) / total + z * z / (4.0 * total * total)
    ) / denominator
    return {"low": max(0.0, center - radius), "high": min(1.0, center + radius)}


def _classification_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    eligible = [
        row for row in rows
        if row.get("direction") in {"BULLISH", "BEARISH"}
        and (value := _number(row.get("underlying_return_pct"))) is not None
        and value != 0
    ]
    tp = sum(row["direction"] == "BULLISH" and float(row["underlying_return_pct"]) > 0 for row in eligible)
    tn = sum(row["direction"] == "BEARISH" and float(row["underlying_return_pct"]) < 0 for row in eligible)
    fp = sum(row["direction"] == "BULLISH" and float(row["underlying_return_pct"]) < 0 for row in eligible)
    fn = sum(row["direction"] == "BEARISH" and float(row["underlying_return_pct"]) > 0 for row in eligible)
    sensitivity = tp / (tp + fn) if tp + fn else None
    specificity = tn / (tn + fp) if tn + fp else None
    balanced = (
        (sensitivity + specificity) / 2.0
        if sensitivity is not None and specificity is not None else None
    )
    denominator = math.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    mcc = (tp * tn - fp * fn) / denominator if denominator else None
    return {
        "eligible": len(eligible),
        "true_bullish": tp,
        "true_bearish": tn,
        "false_bullish": fp,
        "false_bearish": fn,
        "bullish_recall": sensitivity,
        "bearish_recall": specificity,
        "balanced_accuracy": balanced,
        "matthews_correlation": mcc,
    }


def _interval_score(row: Mapping[str, Any], *, alpha: float = INTERVAL_ALPHA) -> float | None:
    realized = _number(row.get("underlying_return_pct"))
    low = _number(row.get("target_low_return_pct"))
    high = _number(row.get("target_high_return_pct"))
    if realized is None or low is None or high is None or high < low or not 0 < alpha < 1:
        return None
    score = high - low
    if realized < low:
        score += 2.0 / alpha * (low - realized)
    elif realized > high:
        score += 2.0 / alpha * (realized - high)
    return score


def _origin_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        origin = _text(row.get("origin_session"))
        if origin and isinstance(row.get("direction_correct"), bool):
            groups.setdefault(origin, []).append(row)
    accuracy = {
        origin: sum(bool(row.get("direction_correct")) for row in items) / len(items)
        for origin, items in groups.items()
    }
    values = list(accuracy.values())
    return {
        "distinct_origin_sessions": len(groups),
        "origin_accuracy_median": _median(values),
        "origin_accuracy_p10": _quantile(values, 0.10),
        "origin_accuracy_p90": _quantile(values, 0.90),
        "minimum_rows_per_origin": min((len(items) for items in groups.values()), default=0),
        "maximum_rows_per_origin": max((len(items) for items in groups.values()), default=0),
    }


def _independent_origin_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Equal-weight origin sessions so repeated same-day rows do not dominate."""

    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        origin = _text(row.get("origin_session"))
        if origin and isinstance(row.get("direction_correct"), bool):
            groups.setdefault(origin, []).append(row)
    origin_accuracy: list[float] = []
    origin_baseline: list[float] = []
    for items in groups.values():
        origin_accuracy.append(sum(bool(row["direction_correct"]) for row in items) / len(items))
        realized = [
            value for row in items
            if (value := _number(row.get("underlying_return_pct"))) is not None and value != 0
        ]
        if realized:
            up_rate = sum(value > 0 for value in realized) / len(realized)
            origin_baseline.append(max(up_rate, 1.0 - up_rate))
    accuracy = _median(origin_accuracy) if len(origin_accuracy) == 1 else (
        sum(origin_accuracy) / len(origin_accuracy) if origin_accuracy else None
    )
    baseline = sum(origin_baseline) / len(origin_baseline) if origin_baseline else None
    return {
        "origin_sessions": len(groups),
        "equal_weight_accuracy": accuracy,
        "equal_weight_majority_baseline": baseline,
        "equal_weight_lift": accuracy - baseline if accuracy is not None and baseline is not None else None,
        "origin_accuracy_p10": _quantile(origin_accuracy, 0.10),
        "origin_accuracy_median": _median(origin_accuracy),
        "origin_accuracy_p90": _quantile(origin_accuracy, 0.90),
        "minimum_tickers_per_origin": min((len(items) for items in groups.values()), default=0),
        "maximum_tickers_per_origin": max((len(items) for items in groups.values()), default=0),
        "status": "PRIMARY_MATURITY_METRIC" if groups else "UNAVAILABLE",
    }


def _baseline_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    eligible = [
        row for row in rows
        if (value := _number(row.get("underlying_return_pct"))) is not None and value != 0
    ]
    result: dict[str, Any] = {}
    definitions = {
        "always_bullish": lambda row: "BULLISH",
        "always_bearish": lambda row: "BEARISH",
        "five_session_momentum": lambda row: _mapping(row.get("baseline_directions")).get("five_session_momentum"),
        "twenty_session_momentum": lambda row: _mapping(row.get("baseline_directions")).get("twenty_session_momentum"),
    }
    for name, selector in definitions.items():
        scored: list[bool] = []
        for row in eligible:
            direction = selector(row)
            if direction not in {"BULLISH", "BEARISH"}:
                continue
            realized = float(row["underlying_return_pct"])
            scored.append(realized > 0 if direction == "BULLISH" else realized < 0)
        result[name] = {
            "evaluated": len(scored),
            "accuracy": sum(scored) / len(scored) if scored else None,
            "accuracy_wilson_95": _wilson_interval(sum(scored), len(scored)) if scored else None,
        }
    return result


def _performance_slice(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize one immutable subset without treating pending rows as misses."""

    resolved = [row for row in rows if row.get("status") == "EVALUATED"]
    directional = [row for row in resolved if isinstance(row.get("direction_correct"), bool)]
    prospective = [row for row in resolved if row.get("registration_mode") == "PROSPECTIVE"]
    prospective_directional = [
        row for row in prospective if isinstance(row.get("direction_correct"), bool)
    ]
    realized = [
        value for row in resolved
        if (value := _number(row.get("underlying_return_pct"))) is not None
    ]
    prospective_realized = [
        value for row in prospective
        if (value := _number(row.get("underlying_return_pct"))) is not None
    ]
    option_returns = [
        value for row in resolved
        if (value := _number(row.get("option_return_pct"))) is not None
    ]
    prospective_option_returns = [
        value for row in prospective
        if (value := _number(row.get("option_return_pct"))) is not None
    ]

    def direction_metrics(
        direction_rows: Sequence[Mapping[str, Any]],
        return_rows: Sequence[float],
    ) -> tuple[float | None, float | None, float | None]:
        accuracy = (
            sum(bool(row.get("direction_correct")) for row in direction_rows) / len(direction_rows)
            if direction_rows else None
        )
        up_rate = sum(value > 0 for value in return_rows) / len(return_rows) if return_rows else None
        baseline = max(up_rate, 1.0 - up_rate) if up_rate is not None else None
        lift = accuracy - baseline if accuracy is not None and baseline is not None else None
        return accuracy, baseline, lift

    accuracy, baseline, lift = direction_metrics(directional, realized)
    prospective_accuracy, prospective_baseline, prospective_lift = direction_metrics(
        prospective_directional, prospective_realized
    )
    covered = [row for row in resolved if isinstance(row.get("range_covered"), bool)]
    return {
        "registered": len(rows),
        "evaluated": len(resolved),
        "pending": len(rows) - len(resolved),
        "direction_evaluable": len(directional),
        "direction_accuracy": accuracy,
        "majority_direction_baseline": baseline,
        "accuracy_lift_vs_majority": lift,
        "median_underlying_return_pct": _median(realized),
        "median_absolute_center_error_pct_points": _median(
            value for row in resolved
            if (value := _number(row.get("absolute_center_error_pct_points"))) is not None
        ),
        "range_coverage": (
            sum(bool(row.get("range_covered")) for row in covered) / len(covered)
            if covered else None
        ),
        "option_evaluated": len(option_returns),
        "median_option_return_pct": _median(option_returns),
        "prospective_evaluated": len(prospective),
        "prospective_direction_evaluable": len(prospective_directional),
        "prospective_direction_accuracy": prospective_accuracy,
        "prospective_majority_direction_baseline": prospective_baseline,
        "prospective_accuracy_lift_vs_majority": prospective_lift,
        "prospective_option_evaluated": len(prospective_option_returns),
        "prospective_median_option_return_pct": _median(prospective_option_returns),
    }


def load_run(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"run artifact must be a JSON object: {path}")
    return payload


def discover_runs(root: str | Path) -> list[Path]:
    root_path = Path(root)
    direct = root_path / "morning-run-enriched.json"
    if direct.is_file():
        return [direct]
    return sorted(root_path.glob("*/morning-run-enriched.json"))


def _entry_source_ids(entry: Mapping[str, Any]) -> tuple[int, ...]:
    provenance = _mapping(entry.get("provenance"))
    edge = _mapping(entry.get("edge"))
    raw = provenance.get("snapshot_ids", edge.get("source_snapshot_ids", ()))
    values = sorted({int(value) for value in _sequence(raw) if isinstance(value, int) and value > 0})
    if not values:
        raise ValueError(f"{_text(entry.get('ticker'), 'ticker')} has no source snapshot IDs")
    return tuple(values)


def _path_targets(forecast: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    targets: dict[int, Mapping[str, Any]] = {}
    for row in _sequence(forecast.get("path")):
        item = _mapping(row)
        session = item.get("session")
        if isinstance(session, int) and session in HORIZONS:
            targets[session] = item
    return targets


def register_run(
    database: str | Path,
    run: Mapping[str, Any],
    *,
    registration_mode: str = "PROSPECTIVE",
) -> dict[str, int]:
    """Freeze all available 1/5/10/20-session forecasts from one run."""

    registration_mode = registration_mode.strip().upper()
    if registration_mode not in {"PROSPECTIVE", "RETROSPECTIVE_ARTIFACT_SEED"}:
        raise ValueError("registration_mode must be PROSPECTIVE or RETROSPECTIVE_ARTIFACT_SEED")
    cutoff_at = timestamp_from_text(_text(run.get("cutoff_at")))
    generated_at = timestamp_from_text(_text(run.get("generated_at", run.get("cutoff_at"))))
    run_id = _text(run.get("run_id"))
    if not run_id:
        raise ValueError("run_id is required for evaluation registration")
    inserted: set[int] = set()
    seen = 0
    with ForecastLedger(database) as ledger:
        for raw_entry in _sequence(run.get("watchlist")):
            entry = _mapping(raw_entry)
            ticker = _text(entry.get("ticker")).upper()
            thesis = _mapping(entry.get("trade_thesis"))
            edge = _mapping(entry.get("edge"))
            forecast = _mapping(edge.get("forecast"))
            forecast_v4 = _mapping(edge.get("forecast_v4"))
            challenger_suite = _mapping(edge.get("shadow_challengers"))
            price = _mapping(entry.get("price"))
            technical = _mapping(entry.get("technical"))
            origin_close = _number(price.get("value"))
            origin_session = _text(price.get("as_of"))[:10]
            direction = _text(thesis.get("direction"), "NEUTRAL").upper()
            if not ticker or origin_close is None or origin_close <= 0 or not origin_session:
                continue
            source_ids = _entry_source_ids(entry)
            conviction = _number(thesis.get("conviction_score")) or 0.0
            option = dict(_mapping(thesis.get("option_reference"))) or None
            return_5d = _number(technical.get("return_5d"))
            return_20d = _number(technical.get("return_20d"))
            baseline_directions = {
                "five_session_momentum": (
                    "BULLISH" if return_5d is not None and return_5d > 0
                    else "BEARISH" if return_5d is not None and return_5d < 0 else None
                ),
                "twenty_session_momentum": (
                    "BULLISH" if return_20d is not None and return_20d > 0
                    else "BEARISH" if return_20d is not None and return_20d < 0 else None
                ),
            }
            latest_close = origin_close
            ema20 = _number(technical.get("ema20", technical.get("ema_20")))
            ema50 = _number(technical.get("ema50", technical.get("ema_50")))
            trend_regime = (
                "ABOVE_EMA20_EMA50" if ema20 is not None and ema50 is not None and latest_close > ema20 and latest_close > ema50
                else "BELOW_EMA20_EMA50" if ema20 is not None and ema50 is not None and latest_close < ema20 and latest_close < ema50
                else "MIXED_EMA_STATE"
            )
            realized_vol = _number(technical.get("realized_vol_20"))
            volatility_regime = (
                "HIGH_RV" if realized_vol is not None and realized_vol >= 0.50
                else "LOW_RV" if realized_vol is not None and realized_vol <= 0.25
                else "MID_RV" if realized_vol is not None else "RV_UNAVAILABLE"
            )
            variants = [("ACTIVE_THESIS_V3", forecast, direction, option)]
            if _path_targets(forecast_v4):
                variants.append((
                    "SHADOW_V4", forecast_v4,
                    _text(forecast_v4.get("direction"), "NEUTRAL").upper(), None,
                ))
            for raw_challenger in _sequence(challenger_suite.get("models")):
                challenger = _mapping(raw_challenger)
                if _path_targets(challenger):
                    variants.append((
                        "SHADOW_CHALLENGER", challenger,
                        _text(challenger.get("direction"), "NEUTRAL").upper(), None,
                    ))
            for model_role, model, model_direction, model_option in variants:
                targets = _path_targets(model)
                if not targets and model_role == "ACTIVE_THESIS_V3":
                    # Preserve the published direction even when the numeric V3
                    # path is unavailable. Shadow models require numeric targets.
                    targets = {horizon: {} for horizon in HORIZONS}
                model_version = _text(model.get("model_version"), "unknown-model")
                observed_frequency = _number(model.get("directional_analog_frequency"))
                feature_payload = {
                    "direction": model_direction,
                    "origin_close": origin_close,
                    "origin_session": origin_session,
                    "forecast": {
                        "model_version": model_version,
                        "status": model.get("status"),
                        "path": [dict(_mapping(row)) for row in _sequence(model.get("path"))],
                    },
                    "dimensions": dict(_mapping(edge.get("dimensions"))),
                    "trade_thesis": {
                        "conviction_score": conviction,
                        "option_reference": model_option,
                    },
                }
                for horizon, target in sorted(targets.items()):
                    existing = ledger.connection.execute(
                        """
                        SELECT id FROM forecasts
                        WHERE ticker = ? AND horizon_sessions = ? AND model_version = ?
                          AND json_extract(metadata_json, '$.evaluation_version') = ?
                          AND json_extract(metadata_json, '$.origin_session') = ?
                        ORDER BY
                          CASE json_extract(metadata_json, '$.registration_mode')
                            WHEN 'PROSPECTIVE' THEN 0 ELSE 1
                          END,
                          cutoff_at, id
                        LIMIT 1
                        """,
                        (ticker, horizon, model_version, EVALUATION_VERSION, origin_session),
                    ).fetchone()
                    if existing is not None:
                        inserted.add(int(existing["id"]))
                        seen += 1
                        continue
                    record = ForecastRecord(
                        ticker=ticker,
                        cutoff_at=cutoff_at,
                        generated_at=generated_at,
                        horizon_sessions=horizon,
                        scoring_version=_text(edge.get("feature_version"), "edge-research-v1"),
                        model_version=model_version,
                        action="NO_ACTION",
                        setup_score=round(max(0.0, min(100.0, conviction))),
                        # This stores an observed analog frequency, not a
                        # calibrated probability. Metadata keeps that boundary.
                        directional_probability=max(0.0, min(1.0, observed_frequency if observed_frequency is not None else 0.5)),
                        confidence=max(0.0, min(1.0, conviction / 100.0)),
                        source_snapshot_ids=source_ids,
                        trigger=_text(thesis.get("trigger_reference"), "No trigger supplied"),
                        invalidation=_text(thesis.get("invalidation_reference"), "No invalidation supplied"),
                        feature_payload=feature_payload,
                        option_metadata=model_option,
                        friction_metadata={
                            "entry_mark": "stored_ask",
                            "exit_mark": "later_stored_bid",
                            "commissions_included": False,
                            "slippage_beyond_spread_included": False,
                            "paper_only": True,
                        },
                        metadata={
                            "evaluation_version": EVALUATION_VERSION,
                            "model_role": model_role,
                            "registration_mode": registration_mode,
                            "run_id": run_id,
                            "origin_session": origin_session,
                            "origin_close": origin_close,
                            "direction_label": model_direction,
                            "target_center_return": _number(target.get("center_return")),
                            "target_low_return": _number(target.get("low_return")),
                            "target_high_return": _number(target.get("high_return")),
                            "published_target_date": _text(target.get("date"))[:10],
                            "daily_action": _text(entry.get("action"), "NO_RECOMMENDATION"),
                            "forecast_status": _text(model.get("status"), "UNAVAILABLE"),
                            "observed_analog_frequency_is_probability": False,
                            "baseline_directions": baseline_directions,
                            "regime_labels": {"trend": trend_regime, "volatility": volatility_regime},
                            "paper_only": True,
                        },
                    )
                    stored = ledger.insert_forecast(record)
                    inserted.add(stored.id)
                    seen += 1
    return {"registered": seen, "unique_forecasts": len(inserted)}


@dataclass(frozen=True, slots=True)
class _Observation:
    ticker: str
    session: str
    close: float
    known_at: datetime
    run_id: str
    options: Mapping[str, Mapping[str, Any]]


def _observations(runs: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, _Observation]]:
    observations: dict[str, dict[str, _Observation]] = {}
    for run in runs:
        known_at = timestamp_from_text(_text(run.get("cutoff_at")))
        run_id = _text(run.get("run_id"))
        for raw_entry in _sequence(run.get("watchlist")):
            entry = _mapping(raw_entry)
            ticker = _text(entry.get("ticker")).upper()
            price = _mapping(entry.get("price"))
            current_session = _text(price.get("as_of"))[:10]
            current_close = _number(price.get("value"))
            option_rows = _sequence(_mapping(entry.get("options")).get("candidates"))
            options = {
                _text(_mapping(row).get("contract")): dict(_mapping(row))
                for row in option_rows if _text(_mapping(row).get("contract"))
            }
            if ticker and current_session and current_close is not None:
                prior = observations.setdefault(ticker, {}).get(current_session)
                candidate = _Observation(ticker, current_session, current_close, known_at, run_id, options)
                if prior is None or candidate.known_at < prior.known_at:
                    observations[ticker][current_session] = candidate
            technical = _mapping(entry.get("technical"))
            for raw_bar in _sequence(technical.get("bars")):
                bar = _mapping(raw_bar)
                session = _text(bar.get("date"))[:10]
                close = _number(bar.get("close"))
                if not ticker or not session or close is None:
                    continue
                prior = observations.setdefault(ticker, {}).get(session)
                candidate = _Observation(ticker, session, close, known_at, run_id, options if session == current_session else {})
                if prior is None or candidate.known_at < prior.known_at:
                    observations[ticker][session] = candidate
    return observations


def _realized_volatility(closes: Sequence[float]) -> float | None:
    if len(closes) < 3:
        return None
    returns = [math.log(closes[index] / closes[index - 1]) for index in range(1, len(closes))]
    return statistics.stdev(returns) * math.sqrt(252) if len(returns) >= 2 else None


def _option_outcome(
    option: Mapping[str, Any] | None,
    observation: _Observation,
) -> tuple[float | None, dict[str, Any]]:
    if not option:
        return None, {"available": False, "reason": "no published reference contract"}
    contract = _text(option.get("contract"))
    entry_ask = _number(option.get("ask"))
    option_type = _text(option.get("type")).upper()
    strike = _number(option.get("strike"))
    expiry_text = _text(option.get("expiry"))[:10]
    if not contract or entry_ask is None or entry_ask <= 0:
        return None, {"available": False, "reason": "published contract or positive entry ask missing"}
    if expiry_text and observation.session >= expiry_text and strike is not None:
        intrinsic = max(0.0, observation.close - strike) if option_type == "CALL" else max(0.0, strike - observation.close)
        return (intrinsic / entry_ask - 1.0) * 100.0, {
            "available": True, "entry": entry_ask, "exit": intrinsic,
            "entry_mark": "stored_ask", "exit_mark": "expiration_intrinsic",
        }
    later = _mapping(observation.options.get(contract))
    exit_bid = _number(later.get("bid"))
    if exit_bid is None or exit_bid < 0:
        return None, {"available": False, "reason": "matching later stored bid unavailable", "contract": contract}
    return (exit_bid / entry_ask - 1.0) * 100.0, {
        "available": True, "entry": entry_ask, "exit": exit_bid,
        "entry_mark": "stored_ask", "exit_mark": "later_stored_bid",
        "contract": contract,
    }


def evaluate_registered(database: str | Path, runs: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Append the first eligible realized outcome for each registered forecast."""

    observations = _observations(runs)
    evaluated = 0
    pending = 0
    with ForecastLedger(database) as ledger:
        rows = ledger.connection.execute(
            "SELECT id FROM forecasts WHERE json_extract(metadata_json, '$.evaluation_version') = ? ORDER BY id",
            (EVALUATION_VERSION,),
        ).fetchall()
        for row in rows:
            forecast = ledger.get_forecast(int(row["id"]))
            if forecast is None:
                continue
            existing = ledger.connection.execute(
                "SELECT 1 FROM outcomes WHERE forecast_id = ? LIMIT 1", (forecast.id,)
            ).fetchone()
            if existing:
                continue
            metadata = forecast.record.metadata
            origin_session = _text(metadata.get("origin_session"))[:10]
            origin_close = _number(metadata.get("origin_close"))
            ticker_observations = observations.get(forecast.record.ticker, {})
            future_sessions = sorted(session for session in ticker_observations if session > origin_session)
            horizon = forecast.record.horizon_sessions
            if origin_close is None or len(future_sessions) < horizon:
                pending += 1
                continue
            target_session = future_sessions[horizon - 1]
            observation = ticker_observations[target_session]
            if observation.known_at <= forecast.record.generated_at:
                pending += 1
                continue
            path_sessions = future_sessions[:horizon]
            closes = [origin_close] + [ticker_observations[session].close for session in path_sessions]
            actual_return = observation.close / origin_close - 1.0
            path_returns = [close / origin_close - 1.0 for close in closes[1:]]
            direction = _text(metadata.get("direction_label"), "NEUTRAL").upper()
            direction_correct = (
                actual_return > 0 if direction == "BULLISH" else
                actual_return < 0 if direction == "BEARISH" else None
            )
            center = _number(metadata.get("target_center_return"))
            low = _number(metadata.get("target_low_return"))
            high = _number(metadata.get("target_high_return"))
            option_return, option_meta = _option_outcome(forecast.record.option_metadata, observation)
            outcome = OutcomeRecord(
                forecast_id=forecast.id,
                observed_at=observation.known_at,
                underlying_return_pct=actual_return * 100.0,
                option_return_pct=option_return,
                max_adverse_excursion_pct=min(path_returns) * 100.0,
                realized_volatility=_realized_volatility(closes),
                metadata={
                    "evaluation_version": EVALUATION_VERSION,
                    "target_session": target_session,
                    "origin_session": origin_session,
                    "observed_run_id": observation.run_id,
                    "direction_label": direction,
                    "direction_correct": direction_correct,
                    "predicted_center_return_pct": center * 100.0 if center is not None else None,
                    "center_error_pct_points": (actual_return - center) * 100.0 if center is not None else None,
                    "absolute_center_error_pct_points": abs(actual_return - center) * 100.0 if center is not None else None,
                    "range_covered": low <= actual_return <= high if low is not None and high is not None else None,
                    "maximum_favorable_excursion_pct": max(path_returns) * 100.0,
                    "option_valuation": option_meta,
                    "paper_only": True,
                },
            )
            ledger.insert_outcome(outcome)
            evaluated += 1
    return {"evaluated": evaluated, "pending": pending}


def build_report(database: str | Path) -> dict[str, Any]:
    """Build a bounded accountability report from frozen ledger records."""

    rows: list[dict[str, Any]] = []
    with ForecastLedger(database) as ledger:
        forecasts = ledger.connection.execute(
            "SELECT id FROM forecasts WHERE json_extract(metadata_json, '$.evaluation_version') = ? ORDER BY cutoff_at, ticker, horizon_sessions",
            (EVALUATION_VERSION,),
        ).fetchall()
        for item in forecasts:
            stored = ledger.get_forecast(int(item["id"]))
            if stored is None:
                continue
            outcome_row = ledger.connection.execute(
                "SELECT id FROM outcomes WHERE forecast_id = ? ORDER BY id LIMIT 1", (stored.id,)
            ).fetchone()
            outcome = ledger.get_outcome(int(outcome_row["id"])) if outcome_row else None
            metadata = stored.record.metadata
            outcome_meta = outcome.record.metadata if outcome else {}
            rows.append({
                "forecast_id": stored.id,
                "ticker": stored.record.ticker,
                "run_id": metadata.get("run_id"),
                "published_at": timestamp_text(stored.record.cutoff_at),
                "origin_session": metadata.get("origin_session"),
                "horizon_sessions": stored.record.horizon_sessions,
                "direction": metadata.get("direction_label"),
                "model_version": stored.record.model_version,
                "model_role": metadata.get("model_role", "ACTIVE_THESIS_V3"),
                "conviction_score": stored.record.setup_score,
                "registration_mode": metadata.get("registration_mode", "UNKNOWN"),
                "status": "EVALUATED" if outcome else "PENDING",
                "target_session": outcome_meta.get("target_session") if outcome else None,
                "underlying_return_pct": outcome.record.underlying_return_pct if outcome else None,
                "direction_correct": outcome_meta.get("direction_correct") if outcome else None,
                "center_error_pct_points": outcome_meta.get("center_error_pct_points") if outcome else None,
                "absolute_center_error_pct_points": outcome_meta.get("absolute_center_error_pct_points") if outcome else None,
                "range_covered": outcome_meta.get("range_covered") if outcome else None,
                "target_center_return_pct": (
                    _number(metadata.get("target_center_return")) * 100.0
                    if _number(metadata.get("target_center_return")) is not None else None
                ),
                "target_low_return_pct": (
                    _number(metadata.get("target_low_return")) * 100.0
                    if _number(metadata.get("target_low_return")) is not None else None
                ),
                "target_high_return_pct": (
                    _number(metadata.get("target_high_return")) * 100.0
                    if _number(metadata.get("target_high_return")) is not None else None
                ),
                "baseline_directions": dict(_mapping(metadata.get("baseline_directions"))),
                "regime_labels": dict(_mapping(metadata.get("regime_labels"))),
                "option_return_pct": outcome.record.option_return_pct if outcome else None,
                "option_available": _mapping(outcome_meta.get("option_valuation")).get("available") if outcome else False,
            })

    # The ledger is append-only, so older same-session refreshes remain
    # auditable. Performance uses one earliest frozen publication per ticker,
    # origin session, horizon, and model version to avoid duplicate weight while
    # preserving independent shadow-model records. Registration mode describes
    # how a publication first entered the ledger; it is not a second forecast.
    # Prefer a genuine prospective publication when legacy replays left both
    # prospective and retrospective copies for the same origin.
    canonical: dict[tuple[str, str, int, str], int] = {}
    rows_by_id = {int(row["forecast_id"]): row for row in rows}
    for row in rows:
        key = (
            str(row["ticker"]), str(row["origin_session"]),
            int(row["horizon_sessions"]), str(row["model_version"]),
        )
        selected_id = canonical.get(key)
        if selected_id is None:
            canonical[key] = int(row["forecast_id"])
            continue
        selected = rows_by_id[selected_id]
        if (
            row["registration_mode"] == "PROSPECTIVE"
            and selected["registration_mode"] != "PROSPECTIVE"
        ):
            canonical[key] = int(row["forecast_id"])
    for row in rows:
        key = (
            str(row["ticker"]), str(row["origin_session"]),
            int(row["horizon_sessions"]), str(row["model_version"]),
        )
        row["daily_tracking_eligible"] = int(row["forecast_id"]) == canonical[key]
    ledger_rows = rows
    model_rows = [row for row in ledger_rows if row["daily_tracking_eligible"]]
    # Keep platform and thesis metrics on the active model. All shadow models
    # appear only in model comparison until a promotion gate passes.
    rows = [row for row in model_rows if row["model_role"] == "ACTIVE_THESIS_V3"]

    horizons: dict[str, Any] = {}
    for horizon in HORIZONS:
        registered = [row for row in rows if row["horizon_sessions"] == horizon]
        resolved = [row for row in registered if row["status"] == "EVALUATED"]
        directional = [row for row in resolved if isinstance(row["direction_correct"], bool)]
        prospective = [row for row in resolved if row["registration_mode"] == "PROSPECTIVE"]
        prospective_directional = [row for row in prospective if isinstance(row["direction_correct"], bool)]
        realized = [float(row["underlying_return_pct"]) for row in resolved if _number(row["underlying_return_pct"]) is not None]
        prospective_realized = [float(row["underlying_return_pct"]) for row in prospective if _number(row["underlying_return_pct"]) is not None]
        option_returns = [float(row["option_return_pct"]) for row in resolved if _number(row["option_return_pct"]) is not None]
        prospective_option_returns = [float(row["option_return_pct"]) for row in prospective if _number(row["option_return_pct"]) is not None]
        accuracy = sum(bool(row["direction_correct"]) for row in directional) / len(directional) if directional else None
        up_rate = sum(value > 0 for value in realized) / len(realized) if realized else None
        baseline = max(up_rate, 1.0 - up_rate) if up_rate is not None else None
        prospective_accuracy = sum(bool(row["direction_correct"]) for row in prospective_directional) / len(prospective_directional) if prospective_directional else None
        prospective_up_rate = sum(value > 0 for value in prospective_realized) / len(prospective_realized) if prospective_realized else None
        prospective_baseline = max(prospective_up_rate, 1.0 - prospective_up_rate) if prospective_up_rate is not None else None
        origin_sessions = len({row["origin_session"] for row in prospective})
        enough = len(prospective_directional) >= MINIMUM_EVALUATIONS_PER_HORIZON and origin_sessions >= MINIMUM_ORIGIN_SESSIONS
        classification = _classification_metrics(resolved)
        prospective_classification = _classification_metrics(prospective)
        direction_successes = sum(bool(row["direction_correct"]) for row in directional)
        prospective_successes = sum(bool(row["direction_correct"]) for row in prospective_directional)
        interval_scores = [
            value for row in resolved if (value := _interval_score(row)) is not None
        ]
        prospective_interval_scores = [
            value for row in prospective if (value := _interval_score(row)) is not None
        ]
        center_errors = [
            value for row in resolved
            if (value := _number(row.get("center_error_pct_points"))) is not None
        ]
        prospective_center_errors = [
            value for row in prospective
            if (value := _number(row.get("center_error_pct_points"))) is not None
        ]
        independent = _independent_origin_metrics(prospective)
        horizons[str(horizon)] = {
            "registered": len(registered),
            "evaluated": len(resolved),
            "pending": len(registered) - len(resolved),
            "direction_evaluable": len(directional),
            "direction_accuracy": accuracy,
            "direction_accuracy_wilson_95": _wilson_interval(direction_successes, len(directional)),
            "classification": classification,
            "majority_direction_baseline": baseline,
            "accuracy_lift_vs_majority": accuracy - baseline if accuracy is not None and baseline is not None else None,
            "median_underlying_return_pct": _median(realized),
            "median_absolute_center_error_pct_points": _median(
                float(row["absolute_center_error_pct_points"])
                for row in resolved if _number(row["absolute_center_error_pct_points"]) is not None
            ),
            "range_coverage": (
                sum(bool(row["range_covered"]) for row in resolved if isinstance(row["range_covered"], bool)) /
                len([row for row in resolved if isinstance(row["range_covered"], bool)])
                if any(isinstance(row["range_covered"], bool) for row in resolved) else None
            ),
            "median_interval_score": _median(interval_scores),
            "median_signed_center_error_pct_points": _median(center_errors),
            "option_evaluated": len(option_returns),
            "median_option_return_pct": _median(option_returns),
            "prospective_evaluated": len(prospective),
            "artifact_seed_evaluated": len(resolved) - len(prospective),
            "prospective_direction_evaluable": len(prospective_directional),
            "prospective_direction_accuracy": prospective_accuracy,
            "prospective_direction_accuracy_wilson_95": _wilson_interval(
                prospective_successes, len(prospective_directional)
            ),
            "prospective_classification": prospective_classification,
            "prospective_majority_direction_baseline": prospective_baseline,
            "prospective_accuracy_lift_vs_majority": prospective_accuracy - prospective_baseline if prospective_accuracy is not None and prospective_baseline is not None else None,
            "prospective_option_evaluated": len(prospective_option_returns),
            "prospective_median_option_return_pct": _median(prospective_option_returns),
            "prospective_median_interval_score": _median(prospective_interval_scores),
            "prospective_median_signed_center_error_pct_points": _median(prospective_center_errors),
            "distinct_origin_sessions": origin_sessions,
            "origin_dependence": _origin_summary(prospective),
            "independent_origin_metrics": independent,
            "baselines": _baseline_metrics(prospective),
            "sample_gate_passed": enough,
        }

    direction_breakdown = {
        direction: _performance_slice([row for row in rows if row["direction"] == direction])
        for direction in ("BULLISH", "BEARISH", "NEUTRAL")
    }
    ticker_breakdown = {
        ticker: _performance_slice([row for row in rows if row["ticker"] == ticker])
        for ticker in sorted({str(row["ticker"]) for row in rows})
    }
    conviction_bands = {
        "0–49": (0.0, 50.0),
        "50–64": (50.0, 65.0),
        "65–79": (65.0, 80.0),
        "80–100": (80.0, 101.0),
    }
    conviction_breakdown = {
        label: _performance_slice([
            row for row in rows
            if (score := _number(row.get("conviction_score"))) is not None and low <= score < high
        ])
        for label, (low, high) in conviction_bands.items()
    }
    model_breakdown = {
        model: _performance_slice([row for row in model_rows if row["model_version"] == model])
        for model in sorted({str(row["model_version"]) for row in model_rows})
    }
    regime_breakdown: dict[str, Any] = {}
    for dimension in ("trend", "volatility"):
        labels = sorted({
            _text(_mapping(row.get("regime_labels")).get(dimension))
            for row in rows if _text(_mapping(row.get("regime_labels")).get(dimension))
        })
        regime_breakdown[dimension] = {
            label: _performance_slice([
                row for row in rows
                if _text(_mapping(row.get("regime_labels")).get(dimension)) == label
            ]) for label in labels
        }

    evaluated_count = sum(row["status"] == "EVALUATED" for row in rows)
    prospective_evaluated = sum(
        row["status"] == "EVALUATED" and row["registration_mode"] == "PROSPECTIVE" for row in rows
    )
    return {
        "evaluation_version": EVALUATION_VERSION,
        "generated_at": timestamp_text(datetime.now().astimezone()),
        "status": "INSUFFICIENT_OUT_OF_SAMPLE_HISTORY",
        "calibrated": False,
        "promotion_eligible": False,
        "registered_forecasts": len(rows),
        "tracked_model_forecasts": len(model_rows),
        "ledger_forecasts": len(ledger_rows),
        "duplicate_publications_excluded": len(ledger_rows) - len(model_rows),
        "evaluated_forecasts": evaluated_count,
        "prospective_evaluated_forecasts": prospective_evaluated,
        "artifact_seed_evaluated_forecasts": evaluated_count - prospective_evaluated,
        "pending_forecasts": len(rows) - evaluated_count,
        "horizons": horizons,
        "direction_breakdown": direction_breakdown,
        "ticker_breakdown": ticker_breakdown,
        "conviction_breakdown": conviction_breakdown,
        "model_breakdown": model_breakdown,
        "regime_breakdown": regime_breakdown,
        "minimum_gates": {
            "evaluations_per_horizon": MINIMUM_EVALUATIONS_PER_HORIZON,
            "distinct_origin_sessions": MINIMUM_ORIGIN_SESSIONS,
            "both_required": True,
            "additional_reviews_required": [
                "chronological stability", "leakage audit", "baseline lift",
                "interval calibration", "option friction",
            ],
        },
        "probability_scoring": {
            "status": "BLOCKED_UNCALIBRATED_INPUT",
            "reason": "Observed analog frequencies are not calibrated probabilities; Brier and log scores are intentionally omitted.",
        },
        "dependence_note": "Ticker outcomes from one origin session share market conditions. Equal-weight origin metrics are the primary maturity read; row-weighted accuracy is diagnostic.",
        "paper_option_method": "published stored ask to first eligible later stored bid; expiration uses intrinsic value; commissions and additional slippage excluded",
        "limitations": [
            "Direction accuracy is compared with the realized majority-direction baseline.",
            "Retrospectively registered dated artifacts are shown as seed diagnostics and never count toward prospective calibration gates.",
            "Option returns are paper marks, not executable fills or recommendations.",
            "Missing matching option quotes remain unavailable and are excluded from return aggregates.",
            "No model is promoted or calibrated from this report without chronological stability and leakage review.",
            "Shadow-model records do not alter active-thesis accuracy, ranking, or option-return aggregates.",
        ],
        "rows": ledger_rows,
    }


def update_evaluations(database: str | Path, run_paths: Sequence[str | Path]) -> dict[str, Any]:
    runs = [load_run(path) for path in sorted((Path(path) for path in run_paths))]
    registration = {"registered": 0, "unique_forecasts": 0}
    for index, run in enumerate(runs):
        mode = "PROSPECTIVE" if index == len(runs) - 1 else "RETROSPECTIVE_ARTIFACT_SEED"
        result = register_run(database, run, registration_mode=mode)
        registration["registered"] += result["registered"]
        registration["unique_forecasts"] += result["unique_forecasts"]
    scoring = evaluate_registered(database, runs)
    report = build_report(database)
    report["update"] = {"registration": registration, "scoring": scoring, "run_count": len(runs)}
    return report
