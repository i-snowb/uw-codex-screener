"""Deterministic, date-blocked development experiments. No production writes."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
import hashlib
import json
from math import isfinite, log, sqrt
from statistics import fmean, pstdev
from typing import Any, Mapping, Sequence

from .clock import is_nyse_session

EXPERIMENT_VERSION = "market-relative-development-v1"
PRICE_FEATURES = ("return_5", "return_20", "realized_vol_20")
MARKET_FEATURES = PRICE_FEATURES + ("excess_5", "excess_20")
ABLATION_FEATURES = {"ridge_trend": ("return_5", "return_20"),
                     "ridge_volatility": ("realized_vol_20",)}
ABLATION_PAIRS = (("volatility", "ridge_price", "ridge_trend"),
                  ("trend", "ridge_price", "ridge_volatility"),
                  ("market_relative", "ridge_market", "ridge_price"))


def encoded(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def digest(value: object) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if isfinite(result) else None


def sessions(start: date, end: date) -> list[str]:
    result = []
    while start <= end:
        if is_nyse_session(start):
            result.append(start.isoformat())
        start += timedelta(days=1)
    return result


@dataclass(frozen=True)
class ExperimentPlan:
    horizons: tuple[int, ...] = (1, 5, 20)
    min_train_origins: int = 126
    test_origins: int = 21
    holdout_origins: int = 63
    ridge_penalty: float = 1.0
    min_train_rows: int = 126

    def __post_init__(self):
        if not self.horizons or len(set(self.horizons)) != len(self.horizons) or any(
            type(h) is not int or not 1 <= h <= 63 for h in self.horizons
        ):
            raise ValueError("horizons must be unique integers from 1 to 63")
        if any(type(n) is not int or n < 1 for n in (
            self.min_train_origins, self.test_origins, self.holdout_origins, self.min_train_rows
        )):
            raise ValueError("fold and sample sizes must be positive integers")
        if number(self.ridge_penalty) is None or self.ridge_penalty <= 0:
            raise ValueError("ridge penalty must be finite and positive")


def make_examples(series: Mapping[str, Sequence[Mapping[str, Any]]], benchmark: str,
                  horizons: Sequence[int]) -> tuple[list[dict], dict]:
    """Use exact calendar windows; missing dates are never compressed."""
    if benchmark not in series or not series[benchmark]:
        raise ValueError("benchmark history is required")
    bars = {}
    for ticker, rows in sorted(series.items()):
        values = {}
        for row in rows:
            day = str(row["date"])
            parsed = date.fromisoformat(day)
            close = number(row.get("close"))
            if not is_nyse_session(parsed) or close is None or close <= 0 or day in values:
                raise ValueError("invalid or duplicate price session")
            values[day] = close
        bars[ticker] = values
    benchmark_days = sorted(bars[benchmark])
    calendar = sessions(date.fromisoformat(benchmark_days[0]), date.fromisoformat(benchmark_days[-1]))
    rows, exclusions = [], defaultdict(int)
    for ticker, prices in sorted(bars.items()):
        if ticker == benchmark:
            continue
        for index in range(20, len(calendar)):
            origin = calendar[index]
            if origin not in prices:
                continue
            past = calendar[index-20:index+1]
            if any(day not in prices or day not in bars[benchmark] for day in past):
                exclusions["missing_feature_window"] += 1
                continue
            closes = [prices[day] for day in past]
            market = [bars[benchmark][day] for day in past]
            r5, r20 = closes[-1]/closes[-6]-1, closes[-1]/closes[0]-1
            b5, b20 = market[-1]/market[-6]-1, market[-1]/market[0]-1
            features = dict(zip(MARKET_FEATURES, (
                r5, r20, pstdev([log(b/a) for a, b in zip(closes, closes[1:])])*sqrt(252),
                r5-b5, r20-b20,
            )))
            for horizon in sorted(horizons):
                future = calendar[index:index+horizon+1]
                if len(future) != horizon+1:
                    exclusions["unresolved_target"] += 1
                    continue
                if any(day not in prices or day not in bars[benchmark] for day in future):
                    exclusions["missing_target_window"] += 1
                    continue
                stock_return = prices[future[-1]]/closes[-1]-1
                benchmark_return = bars[benchmark][future[-1]]/market[-1]-1
                rows.append({"ticker": ticker, "origin": origin, "target": future[-1],
                             "horizon": horizon, "features": features,
                             "absolute_return": stock_return, "excess_return": stock_return-benchmark_return})
    return rows, dict(sorted(exclusions.items()))


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    augmented = [list(row)+[value] for row, value in zip(matrix, vector)]
    size = len(vector)
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(augmented[row][col]))
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        value = augmented[col][col]
        if abs(value) < 1e-12:
            raise ValueError("singular training matrix")
        augmented[col] = [item/value for item in augmented[col]]
        for row in range(size):
            if row != col:
                factor = augmented[row][col]
                augmented[row] = [a-factor*b for a, b in zip(augmented[row], augmented[col])]
    return [row[-1] for row in augmented]


def fit_ridge(rows: Sequence[Mapping], names: Sequence[str], target: str, penalty: float) -> dict:
    """Weighted least squares: each training origin has equal total weight."""
    if not rows or number(penalty) is None or penalty <= 0:
        raise ValueError("nonempty training data and positive penalty required")
    counts = defaultdict(int)
    for row in rows:
        counts[row["origin"]] += 1
    weights = [1/(len(counts)*counts[row["origin"]]) for row in rows]
    means = [sum(w*row["features"][name] for row, w in zip(rows, weights)) for name in names]
    scales = [sqrt(sum(w*(row["features"][name]-mean)**2 for row, w in zip(rows, weights))) or 1.0
              for name, mean in zip(names, means)]
    size = len(names)+1
    gram, rhs = [[0.0]*size for _ in range(size)], [0.0]*size
    for row, weight in zip(rows, weights):
        x = [1.0]+[(row["features"][name]-mean)/scale for name, mean, scale in zip(names, means, scales)]
        for i in range(size):
            rhs[i] += weight*x[i]*row[target]
            for j in range(size):
                gram[i][j] += weight*x[i]*x[j]
    for i in range(1, size):
        gram[i][i] += penalty
    return {"features": list(names), "means": means, "scales": scales,
            "coefficients": _solve(gram, rhs), "penalty": penalty,
            "training_rows": len(rows), "training_origins": len(counts),
            "training_digest": digest(rows)}


def ridge_predict(model: Mapping, features: Mapping[str, float]) -> float:
    scaled = [(features[name]-mean)/scale for name, mean, scale in zip(
        model["features"], model["means"], model["scales"])]
    return model["coefficients"][0]+sum(a*b for a, b in zip(model["coefficients"][1:], scaled))


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def paired_metrics(rows: Sequence[Mapping], candidate: str, baseline: str) -> dict:
    """Pair on the same rows, then weight origins equally. Neutral is abstention."""
    groups = defaultdict(list)
    direction_groups = defaultdict(list)
    matched_keys, direction_keys = [], []
    seen = set()
    candidate_rows = baseline_rows = 0
    for row in rows:
        predictions = row["predictions"]
        a, b, actual = (number(value) for value in (predictions.get(candidate), predictions.get(baseline), row["actual"]))
        key = [row["ticker"], row["origin"], row["target"], row["horizon"],
               row.get("cutoff"), row.get("feature_version"), row.get("prediction_target")]
        identity = encoded(key)
        if identity in seen:
            raise ValueError("duplicate paired decision identity")
        seen.add(identity)
        candidate_rows += a is not None and actual is not None
        baseline_rows += b is not None and actual is not None
        if a is None or b is None or actual is None:
            continue
        matched_keys.append(key)
        groups[row["origin"]].append((abs(a-actual), abs(b-actual)))
        if _sign(a) and _sign(b) and _sign(actual):
            direction_keys.append(key)
            direction_groups[row["origin"]].append((_sign(a)==_sign(actual), _sign(b)==_sign(actual)))
    errors = [(fmean(a for a, _ in group), fmean(b for _, b in group)) for group in groups.values()]
    accuracy = [(fmean(a for a, _ in group), fmean(b for _, b in group)) for group in direction_groups.values()]
    return {
        "candidate": candidate, "baseline": baseline, "eligible_rows": len(rows),
        "candidate_prediction_rows": candidate_rows, "baseline_prediction_rows": baseline_rows,
        "matched_coverage_of_candidate": len(matched_keys)/candidate_rows if candidate_rows else None,
        "direction_coverage_of_matched": len(direction_keys)/len(matched_keys) if matched_keys else None,
        "coverage_denominator": "eligible_rows is the supplied decision universe, including missing model predictions; availability and nonneutral coverage are reported separately.",
        "matched_rows": len(matched_keys), "origin_dates": len(groups),
        "cohort_sha256": digest(sorted(matched_keys, key=encoded)),
        "direction_rows": len(direction_keys), "direction_origin_dates": len(direction_groups),
        "direction_cohort_sha256": digest(sorted(direction_keys, key=encoded)),
        "paired_direction_coverage": len(direction_keys)/len(rows) if rows else None,
        "candidate_mae": fmean(a for a, _ in errors) if errors else None,
        "baseline_mae": fmean(b for _, b in errors) if errors else None,
        "mae_improvement": fmean(b-a for a, b in errors) if errors else None,
        "candidate_accuracy": fmean(a for a, _ in accuracy) if accuracy else None,
        "baseline_accuracy": fmean(b for _, b in accuracy) if accuracy else None,
        "accuracy_lift": fmean(a-b for a, b in accuracy) if accuracy else None,
        "method": "Equal-origin paired metrics. Origins and overlapping horizons are not statistically independent; no significance claim.",
    }


def run_experiment(examples: Sequence[Mapping], plan: ExperimentPlan, *, source_digest: str,
                   include_ablation: bool = False) -> dict:
    rows = sorted(examples, key=lambda row: (row["origin"], row["ticker"], row["horizon"]))
    identities = [(row["ticker"], row["origin"], row["horizon"]) for row in rows]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate example identity")
    for row in rows:
        origin, target = date.fromisoformat(row["origin"]), date.fromisoformat(row["target"])
        if (type(row["horizon"]) is not int or row["horizon"] not in plan.horizons
                or not is_nyse_session(origin) or not is_nyse_session(target)
                or len(sessions(origin, target)) != row["horizon"]+1):
            raise ValueError("target must be the exact future NYSE session")
        if any(number(row["features"].get(name)) is None for name in MARKET_FEATURES) or any(
            number(row.get(name)) is None for name in ("absolute_return", "excess_return")
        ):
            raise ValueError("features and labels must be finite")
    origins = sorted({row["origin"] for row in rows})
    holdout = origins[-plan.holdout_origins:]
    holdout_start = holdout[0] if holdout else None
    development = [row for row in rows if holdout_start and row["origin"] < holdout_start and row["target"] < holdout_start]
    dev_origins = sorted({row["origin"] for row in development})
    predictions, folds = [], []
    for horizon in sorted(plan.horizons):
        candidates = [row for row in development if row["horizon"] == horizon]
        for start in range(plan.min_train_origins, len(dev_origins), plan.test_origins):
            test_dates = dev_origins[start:start+plan.test_origins]
            if len(test_dates) < plan.test_origins:
                continue
            train = [row for row in candidates if row["target"] < test_dates[0]]
            test = [row for row in candidates if row["origin"] in test_dates]
            if not test or len(train) < plan.min_train_rows or len({row["origin"] for row in train}) < plan.min_train_origins:
                continue
            fold = {"id": f"h{horizon}-{test_dates[0]}", "horizon": horizon,
                    "test_start": test_dates[0], "test_end": test_dates[-1],
                    "latest_training_target": max(row["target"] for row in train),
                    "train_rows": len(train), "test_rows": len(test), "models": {}}
            for target in ("absolute_return", "excess_return"):
                price_model = fit_ridge(train, PRICE_FEATURES, target, plan.ridge_penalty)
                market_model = fit_ridge(train, MARKET_FEATURES, target, plan.ridge_penalty)
                fold["models"][target] = {"ridge_price": price_model, "ridge_market": market_model}
                extra_models = {name: fit_ridge(train, names, target, plan.ridge_penalty)
                                for name, names in ABLATION_FEATURES.items()} if include_ablation else {}
                fold["models"][target].update(extra_models)
                origin_means = defaultdict(list)
                for row in train:
                    origin_means[row["origin"]].append(row[target])
                training_mean = fmean(fmean(values) for values in origin_means.values())
                for row in test:
                    f = row["features"]
                    r5, r20 = (f["return_5"], f["return_20"]) if target == "absolute_return" else (f["excess_5"], f["excess_20"])
                    trend = max(-.015, min(.015, .4*r5/5+.6*r20/20))*horizon
                    predictions.append({
                        **{key: row[key] for key in ("ticker", "origin", "target", "horizon")},
                        "fold": fold["id"], "prediction_target": target, "actual": row[target],
                        "predictions": {"ridge_price": ridge_predict(price_model, f),
                                        "ridge_market": ridge_predict(market_model, f),
                                        "trend_only": trend, "training_mean": training_mean,
                                        "zero_return": 0.0,
                                        **{name: ridge_predict(model, f) for name, model in extra_models.items()}},
                    })
            folds.append(fold)
    scores = []
    for horizon in sorted(plan.horizons):
        for target in ("absolute_return", "excess_return"):
            subset = [row for row in predictions if row["horizon"] == horizon and row["prediction_target"] == target]
            for candidate, baseline in (("ridge_market", "ridge_price"), ("ridge_market", "trend_only"),
                                        ("ridge_price", "trend_only"), ("ridge_market", "training_mean"),
                                        ("ridge_market", "zero_return")):
                scores.append({"horizon": horizon, "target": target, **paired_metrics(subset, candidate, baseline)})
            if include_ablation:
                for group, candidate, baseline in ABLATION_PAIRS:
                    scores.append({"horizon": horizon, "target": target, "ablation_group": group,
                                   **paired_metrics(subset, candidate, baseline)})
    return {"schema_version": EXPERIMENT_VERSION, "status": "DEVELOPMENT_ONLY" if predictions else "INSUFFICIENT_HISTORY",
            "plan": asdict(plan), "source_sha256": source_digest,
            "experiment_id": digest({"version": EXPERIMENT_VERSION, "plan": asdict(plan), "source": source_digest,
                                     "include_ablation": include_ablation}),
            "include_ablation": include_ablation,
            "holdout": {"status": "SEALED_NOT_SCORED", "origin_dates": len(holdout),
                        "start": holdout_start, "end": holdout[-1] if holdout else None,
                        "boundary": "No holdout labels enter fitting, model selection, or metrics."},
            "folds": folds, "scores": scores, "predictions": predictions,
            "promotion_eligible": False, "active_model_changed": False,
            "limitations": ["Historical reconstruction from a later capture, not prospective evidence.",
                           "Current watchlist selection; no survivorship-free universe claim.",
                           "Corporate-action and security-lineage reconciliation remains required.",
                           "Fixed parameters; no hyperparameter search or holdout scoring.",
                           "Close-to-close returns, not executable fills or option returns.",
                           "No calibrated probabilities, prediction intervals, or significance tests."]}


def signal_contribution_report(experiment: Mapping, *, capture_cutoff: str) -> dict:
    comparisons = []
    for score in experiment["scores"]:
        if "ablation_group" not in score:
            continue
        comparisons.append({key: score[key] for key in (
            "ablation_group", "horizon", "target", "candidate", "baseline", "candidate_mae",
            "baseline_mae", "mae_improvement", "accuracy_lift", "matched_rows", "origin_dates",
            "direction_rows", "direction_origin_dates", "cohort_sha256", "direction_cohort_sha256")})
    return {"schema_version": "signal-contribution-v1", "status": experiment["status"],
            "capture_cutoff": capture_cutoff, "experiment_id": experiment["experiment_id"],
            "benchmark": experiment.get("benchmark"), "holdout": experiment["holdout"],
            "comparisons": comparisons, "promotion_eligible": False,
            "untested": [{"signal": "Flow", "reason": "No validated comparable historical feature panel supplied."},
                         {"signal": "Events/news", "reason": "No validated point-in-time event feature panel supplied."}],
            "interpretation": "Lower paired error is a development observation, not proven signal value. Features can interact; contributions do not sum to a forecast or establish causality.",
            "limitations": experiment["limitations"]}
