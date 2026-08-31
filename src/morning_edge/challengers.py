"""Deterministic shadow challengers for direction and forecast width.

The models in this module are deliberately small. They provide independent
mechanisms for comparison with the analog models and never change the active
thesis before chronological promotion gates pass.
"""

from __future__ import annotations

from datetime import date
from math import exp, log, sqrt
from statistics import fmean, median, pstdev
from typing import Any, Mapping, Sequence

from .clock import next_nyse_session


CHALLENGER_VERSION = "shadow-challenger-suite-v1"


def _number(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _clean_bars(bars: Sequence[Mapping[str, Any]]) -> tuple[list[date], list[float]]:
    clean: list[tuple[date, float]] = []
    for row in bars:
        value = _number(row.get("close"))
        try:
            session = date.fromisoformat(str(row.get("date"))[:10])
        except ValueError:
            continue
        if value is not None and value > 0:
            clean.append((session, value))
    return [item[0] for item in clean], [item[1] for item in clean]


def _ema(values: Sequence[float], window: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < window:
        return result
    current = fmean(values[:window])
    result[window - 1] = current
    weight = 2.0 / (window + 1.0)
    for index in range(window, len(values)):
        current = values[index] * weight + current * (1.0 - weight)
        result[index] = current
    return result


def _feature_rows(closes: Sequence[float]) -> list[list[float] | None]:
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    rows: list[list[float] | None] = [None] * len(closes)
    for index in range(63, len(closes)):
        returns = [log(closes[position] / closes[position - 1]) for position in range(index - 19, index + 1)]
        high = max(closes[index - 62:index + 1])
        if ema20[index] is None or ema50[index] is None or high <= 0:
            continue
        rows[index] = [
            closes[index] / closes[index - 5] - 1.0,
            closes[index] / closes[index - 20] - 1.0,
            closes[index] / closes[index - 63] - 1.0,
            closes[index] / float(ema20[index]) - 1.0,
            closes[index] / float(ema50[index]) - 1.0,
            pstdev(returns) * sqrt(252),
            closes[index] / high - 1.0,
        ]
    return rows


def _logistic_shadow(closes: Sequence[float], horizon: int) -> dict[str, Any]:
    feature_rows = _feature_rows(closes)
    train: list[tuple[list[float], float]] = []
    for index in range(63, len(closes) - horizon):
        features = feature_rows[index]
        if features is not None:
            train.append((features, 1.0 if closes[index + horizon] > closes[index] else 0.0))
    current = feature_rows[-1] if feature_rows else None
    if current is None or len(train) < 120:
        return {"status": "INSUFFICIENT_HISTORY", "sample_size": len(train)}
    means = [fmean(row[0][column] for row in train) for column in range(len(current))]
    scales = [pstdev(row[0][column] for row in train) for column in range(len(current))]
    scales = [value if value > 1e-12 else 1.0 for value in scales]
    standardized = [
        ([(features[column] - means[column]) / scales[column] for column in range(len(current))], target)
        for features, target in train
    ]
    weights = [0.0] * (len(current) + 1)
    learning_rate = 0.08
    penalty = 0.10
    for _ in range(500):
        gradient = [0.0] * len(weights)
        for features, target in standardized:
            score = weights[0] + sum(weights[column + 1] * value for column, value in enumerate(features))
            prediction = 1.0 / (1.0 + exp(-max(-30.0, min(30.0, score))))
            error = prediction - target
            gradient[0] += error
            for column, value in enumerate(features):
                gradient[column + 1] += error * value
        gradient[0] /= len(standardized)
        for column in range(1, len(weights)):
            gradient[column] = gradient[column] / len(standardized) + penalty * weights[column]
        weights = [weight - learning_rate * gradient[index] for index, weight in enumerate(weights)]
    current_scaled = [(current[column] - means[column]) / scales[column] for column in range(len(current))]
    logit = weights[0] + sum(weights[column + 1] * value for column, value in enumerate(current_scaled))
    score = 1.0 / (1.0 + exp(-max(-30.0, min(30.0, logit))))
    direction = "BULLISH" if score >= 0.55 else "BEARISH" if score <= 0.45 else "NEUTRAL"
    return {
        "status": "SHADOW_UNCALIBRATED", "sample_size": len(train),
        "direction": direction, "raw_direction_score": score,
        "raw_score_is_probability": False,
        "feature_names": [
            "return_5d", "return_20d", "return_63d", "ema20_distance",
            "ema50_distance", "realized_vol_20", "drawdown_63d",
        ],
        "method": "Ticker-specific L2 logistic challenger with deterministic standardization and fixed training parameters.",
    }


def _ewma_volatility(closes: Sequence[float], horizon: int) -> dict[str, Any]:
    if len(closes) < 64:
        return {"status": "INSUFFICIENT_HISTORY"}
    returns = [log(closes[index] / closes[index - 1]) for index in range(1, len(closes))]
    variance = returns[0] * returns[0]
    decay = 0.94
    for value in returns[1:]:
        variance = decay * variance + (1.0 - decay) * value * value
    annualized = sqrt(max(0.0, variance) * 252)
    horizon_sigma = annualized * sqrt(horizon / 252)
    return {
        "status": "SHADOW_UNCALIBRATED", "annualized_volatility": annualized,
        "low_return": -1.2815515655446004 * horizon_sigma,
        "high_return": 1.2815515655446004 * horizon_sigma,
        "method": "EWMA daily log-return variance with lambda 0.94; symmetric 10th/90th normal reference.",
    }


def _historical_quantile(closes: Sequence[float], horizon: int) -> dict[str, Any]:
    start = max(0, len(closes) - 504 - horizon)
    values = [closes[index + horizon] / closes[index] - 1.0 for index in range(max(0, start), len(closes) - horizon)]
    if len(values) < 120:
        return {"status": "INSUFFICIENT_HISTORY", "sample_size": len(values)}
    center = median(values)
    return {
        "status": "SHADOW_UNCALIBRATED", "sample_size": len(values),
        "center_return": center, "low_return": _quantile(values, 0.10),
        "high_return": _quantile(values, 0.90),
        "direction": "BULLISH" if center >= 0.01 else "BEARISH" if center <= -0.01 else "NEUTRAL",
        "method": "Unconditional ticker-specific forward-return quantiles over at most 504 sessions.",
    }


def shadow_challengers(*, bars: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    dates, closes = _clean_bars(bars)
    if len(closes) < 64:
        return {
            "status": "INSUFFICIENT_HISTORY", "version": CHALLENGER_VERSION,
            "promotion_eligible": False, "models": [],
            "directional_model_count": 0, "directional_agreement": None,
        }
    models: list[dict[str, Any]] = []
    for horizon in (5, 20):
        target_date = dates[-1]
        for _ in range(horizon):
            target_date = next_nyse_session(target_date, include_current=False)
        logistic = _logistic_shadow(closes, horizon)
        if logistic.get("status") == "SHADOW_UNCALIBRATED":
            models.append({
                **logistic, "model_version": f"regularized-logistic-direction-v1-{horizon}d",
                "horizon_sessions": horizon,
                "path": [{"session": horizon, "date": target_date.isoformat()}],
            })
        quantile = _historical_quantile(closes, horizon)
        if quantile.get("status") == "SHADOW_UNCALIBRATED":
            models.append({
                **quantile, "model_version": f"historical-quantile-baseline-v1-{horizon}d",
                "horizon_sessions": horizon,
                "path": [{
                    "session": horizon, "date": target_date.isoformat(),
                    "center_return": quantile["center_return"],
                    "low_return": quantile["low_return"], "high_return": quantile["high_return"],
                }],
            })
        ewma = _ewma_volatility(closes, horizon)
        if ewma.get("status") == "SHADOW_UNCALIBRATED":
            models.append({
                **ewma, "model_version": f"ewma-volatility-reference-v1-{horizon}d",
                "horizon_sessions": horizon, "direction": "NEUTRAL",
                "path": [{
                    "session": horizon, "date": target_date.isoformat(), "center_return": 0.0,
                    "low_return": ewma["low_return"], "high_return": ewma["high_return"],
                }],
            })
    directional = [row["direction"] for row in models if row.get("direction") in {"BULLISH", "BEARISH"}]
    agreement = max((directional.count(value) for value in set(directional)), default=0) / len(directional) if directional else None
    return {
        "status": "SHADOW_ONLY", "version": CHALLENGER_VERSION, "promotion_eligible": False,
        "models": models, "directional_model_count": len(directional),
        "directional_agreement": agreement,
        "limitations": [
            "Raw logistic scores are not calibrated probabilities.",
            "Models are ticker-specific and share the same limited price history.",
            "No challenger can change rank, thesis, option reference, or recommendation state.",
        ],
    }
