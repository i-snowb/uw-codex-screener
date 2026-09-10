"""Read-only evidence audit and matched comparisons of frozen forecasts."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .clock import NEW_YORK, is_nyse_session, next_nyse_session
from .evaluation import EVALUATION_VERSION
from .experiments import digest, number, paired_metrics, sessions
from .freshness import latest_complete_session
from .models import timestamp_from_text
from .normalization import EvidenceReader


def read_only(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database.resolve().as_uri()+"?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def load_development_data(database: Path, run: Mapping, benchmark: str, *, include_reconciliation=False) -> dict:
    cutoff = timestamp_from_text(run["cutoff_at"])
    end = latest_complete_session(cutoff)
    tickers = sorted({str(row["ticker"]) for row in run["watchlist"]})
    if benchmark in tickers:
        raise ValueError("benchmark must not also be a research ticker")
    series, sources = {}, {}
    with EvidenceReader(database) as reader:
        reader._connection.execute("BEGIN")
        for ticker in sorted(tickers+[benchmark]):
            bars = reader.normalize_bars(ticker, cutoff_at=cutoff)
            rows = [{"date": bar.session_date.isoformat(), "close": bar.close,
                     "open": bar.open, "high": bar.high, "low": bar.low, "volume": bar.volume,
                     "available_at": bar.available_at.isoformat(), "source_snapshot_id": bar.source_snapshot_id}
                    for bar in bars if bar.session_date <= end]
            series[ticker] = rows
        ids = sorted({row["source_snapshot_id"] for rows in series.values() for row in rows})
        for snapshot_id in ids:
            source = reader._connection.execute(
                """SELECT s.id,s.symbol,s.dataset,s.as_of,s.retrieved_at,s.raw_payload_hash,p.content_json
                FROM snapshots s JOIN raw_payloads p ON p.content_hash=s.raw_payload_hash WHERE s.id=?""",
                (snapshot_id,)).fetchone()
            if source is None or source["dataset"] != "ohlc":
                raise ValueError("price source is missing or is not OHLC")
            if hashlib.sha256(source["content_json"].encode()).hexdigest() != source["raw_payload_hash"]:
                raise ValueError("raw price payload integrity failed")
            if max(timestamp_from_text(source["as_of"]), timestamp_from_text(source["retrieved_at"])) > cutoff:
                raise ValueError("price source is later than capture cutoff")
            sources[str(snapshot_id)] = {key: source[key] for key in (
                "id", "symbol", "dataset", "as_of", "retrieved_at", "raw_payload_hash")}
        dataset = {"schema_version": "development-price-input-v1", "capture_cutoff": run["cutoff_at"],
                   "latest_complete_session": end.isoformat(), "benchmark": benchmark,
                   "provenance_mode": "LATER_CAPTURE_HISTORICAL_RECONSTRUCTION",
                   "series": series, "sources": sources}
        if include_reconciliation:
            from .reconciliation import reconcile_inputs
            dataset["reconciliation"] = reconcile_inputs(reader._connection, dataset)
    for ticker, rows in series.items():
        if any(sources[str(row["source_snapshot_id"])]["symbol"] != ticker for row in rows):
            raise ValueError("price source symbol mismatch")
    return dataset


def audit_inputs(run: Mapping, dataset: Mapping) -> dict:
    cutoff = timestamp_from_text(run["cutoff_at"])
    end = latest_complete_session(cutoff).isoformat()
    prices = []
    for ticker, rows in sorted(dataset["series"].items()):
        dates = [row["date"] for row in rows]
        valid_dates = []
        issues = []
        if not rows:
            issues.append({"issue": "EMPTY_PRICE_HISTORY"})
        for row in rows:
            day = date.fromisoformat(row["date"])
            if not is_nyse_session(day):
                issues.append({"date": row["date"], "issue": "NON_SESSION"})
            else:
                valid_dates.append(row["date"])
            if row["date"] > end:
                issues.append({"date": row["date"], "issue": "FUTURE_OR_INCOMPLETE_SESSION"})
            close = number(row.get("close"))
            if close is None or close <= 0:
                issues.append({"date": row["date"], "issue": "INVALID_CLOSE"})
            if timestamp_from_text(row["available_at"]) > cutoff:
                issues.append({"date": row["date"], "issue": "AVAILABLE_AFTER_CAPTURE"})
        calendar = sessions(date.fromisoformat(min(valid_dates)), date.fromisoformat(max(valid_dates))) if valid_dates else []
        missing = sorted(set(calendar)-set(dates))
        jumps = [{"from": left["date"], "to": right["date"], "return": right["close"]/left["close"]-1}
                 for left, right in zip(rows, rows[1:])
                 if number(left.get("close")) is not None and left["close"] > 0
                 and number(right.get("close")) is not None
                 and abs(right["close"]/left["close"]-1) > .25]
        late = sum(timestamp_from_text(row["available_at"]).date() > date.fromisoformat(row["date"]) for row in rows)
        prices.append({"ticker": ticker, "rows": len(rows), "first": min(dates) if dates else None,
                       "last": max(dates) if dates else None, "duplicate_dates": len(dates)-len(set(dates)),
                       "missing_internal_sessions": missing, "issues": issues, "large_adjacent_moves": jumps,
                       "retrieved_on_later_utc_date": late,
                       "adjustment_basis": "UNRECONCILED", "security_lineage": "UNRECONCILED"})
    symbols = []
    for row in sorted(run["watchlist"], key=lambda item: item["ticker"]):
        flow = row.get("edge", {}).get("flow_conviction", {})
        news = row.get("evidence", {}).get("news", {}).get("latest_headlines", [])
        after_close, duplicates, seen = [], [], set()
        price_session = row.get("price", {}).get("as_of")
        for item in news:
            key = str(item.get("headline", "")).strip().lower()
            if key and key in seen:
                duplicates.append(key)
            seen.add(key)
            try:
                published = timestamp_from_text(item.get("published_at", ""))
                if price_session and published.astimezone(NEW_YORK).date().isoformat() > price_session:
                    after_close.append(item.get("published_at"))
            except (ValueError, TypeError, AttributeError):
                pass
        symbols.append({"ticker": row["ticker"], "flow_coverage": flow.get("coverage_status", "UNAVAILABLE"),
                        "flow_comparison": flow.get("comparison_status", "UNAVAILABLE"),
                        "flow_quality": flow.get("quality_multiplier"),
                        "headline_duplicate_count": len(duplicates),
                        "headlines_after_price_session_date": len(after_close),
                        "news_boundary": "Publication-date flag only; this is not a measured post-event reaction."})
    hard = [row["ticker"] for row in prices if row["issues"] or row["duplicate_dates"]]
    return {"schema_version": "research-integrity-audit-v1",
            "status": "INVALID_INPUT" if hard else "DEVELOPMENT_ONLY_WITH_LIMITATIONS",
            "invalid_tickers": hard, "prices": prices, "current_context": symbols,
            "promotion_blockers": ["Corporate-action adjustment and security lineage are unreconciled.",
                                   "Historical rows were acquired later; retrospective experiments are not prospective.",
                                   "Current watchlist is not a point-in-time historical universe."],
            "boundary": "Audit covers normalized research inputs and their raw payload hashes, not every rejected raw row. Large moves are flags, not split diagnoses. No prices are repaired or winsorized."}


def _expected_target(origin: str, horizon: int) -> str:
    target = date.fromisoformat(origin)
    if not is_nyse_session(target) or type(horizon) is not int or horizon < 1:
        raise ValueError("invalid forecast origin or horizon")
    for _ in range(horizon):
        target = next_nyse_session(target, include_current=False)
    return target.isoformat()


def frozen_cohorts(database: Path) -> dict:
    """Never initialize a ledger or select a later forecast because it won."""
    connection = read_only(database)
    try:
        rows = connection.execute(
            """SELECT f.*,o.id AS outcome_id,o.underlying_return_pct,o.metadata_json AS outcome_metadata
            FROM forecasts f LEFT JOIN outcomes o ON o.id=(
                SELECT MIN(o2.id) FROM outcomes o2 WHERE o2.forecast_id=f.id)
            ORDER BY f.cutoff_at,f.id""").fetchall()
    finally:
        connection.close()
    canonical, rejected = {}, defaultdict(int)
    for raw in rows:
        metadata = json.loads(raw["metadata_json"])
        if metadata.get("evaluation_version") != EVALUATION_VERSION or metadata.get("registration_mode") != "PROSPECTIVE":
            rejected["nonprospective_or_other_evaluation"] += 1
            continue
        origin = metadata.get("origin_session")
        try:
            target = _expected_target(origin, raw["horizon_sessions"])
        except (ValueError, TypeError):
            rejected["invalid_origin"] += 1
            continue
        key = (raw["ticker"], origin, raw["horizon_sessions"], raw["model_version"])
        if key in canonical:
            rejected["later_same_origin_forecast"] += 1
            continue
        # Freeze the earliest eligible publication before considering its outcome.
        canonical[key] = {"raw": dict(raw), "metadata": metadata, "target": target}
    grouped, records = {}, []
    label_sign = {"BULLISH": 1.0, "BEARISH": -1.0, "NEUTRAL": 0.0}
    for item in canonical.values():
        raw, metadata, target = item["raw"], item["metadata"], item["target"]
        outcome = json.loads(raw["outcome_metadata"]) if raw["outcome_metadata"] else {}
        if not raw["outcome_id"] or number(raw["underlying_return_pct"]) is None:
            rejected["pending_or_invalid_outcome"] += 1
            continue
        if outcome.get("target_session") != target or metadata.get("published_target_date") not in (None, "", target):
            rejected["target_mismatch"] += 1
            continue
        key = (raw["ticker"], metadata["origin_session"], target, raw["horizon_sessions"],
               raw["cutoff_at"], raw["scoring_version"])
        actual = raw["underlying_return_pct"]/100
        record = grouped.setdefault(key, {"ticker": key[0], "origin": key[1], "target": target,
            "horizon": key[3], "cutoff": key[4], "feature_version": key[5], "actual": actual,
            "predictions": {}, "directions": {}, "intervals": {}, "forecast_ids": {}})
        if abs(record["actual"]-actual) > 1e-10:
            raise ValueError("matched forecasts disagree on realized return")
        name = raw["model_version"]
        record["forecast_ids"][name] = raw["id"]
        record["predictions"][name] = number(metadata.get("target_center_return"))
        record["directions"][name] = label_sign.get(metadata.get("direction_label"))
        record["intervals"][name] = [number(metadata.get("target_low_return")), number(metadata.get("target_high_return"))]
        for baseline, direction in {"always_bullish": "BULLISH", "always_bearish": "BEARISH",
                                    **metadata.get("baseline_directions", {})}.items():
            value = label_sign.get(direction)
            if baseline in record["directions"] and record["directions"][baseline] != value:
                raise ValueError("matched forecasts disagree on stored baseline")
            record["directions"][baseline] = value
        records.append({"forecast_id": raw["id"], "outcome_id": raw["outcome_id"],
                        "forecast_digest": digest({k: raw[k] for k in ("idempotency_key", "metadata_json", "feature_hash")}),
                        "outcome_digest": digest({"return": actual, "metadata": outcome})})
    comparisons = []
    for horizon in sorted({key[3] for key in grouped}):
        subset = [row for row in grouped.values() if row["horizon"] == horizon]
        models = sorted({name for row in subset for name in row["forecast_ids"]})
        for name in models:
            for baseline in ("always_bullish", "always_bearish", "twenty_session_momentum"):
                directional_rows = [{**row, "predictions": row["directions"]} for row in subset]
                scored = paired_metrics(directional_rows, name, baseline)
                comparisons.append({"horizon": horizon, "candidate": name, "baseline": baseline,
                    **{key: value for key, value in scored.items() if key not in (
                        "candidate_mae", "baseline_mae", "mae_improvement")},
                    "scope": "Stored direction labels; paired nonneutral outcomes; no return-error metric for direction-only baselines."})
            if name != "analog-path-ensemble-v3":
                score = paired_metrics(subset, name, "analog-path-ensemble-v3")
                comparisons.append({"horizon": horizon, **score, "scope": "Numeric centers on identical decisions; historical direction-label semantics are not rewritten."})
    return {"schema_version": "frozen-matched-cohorts-v1", "status": "READ_ONLY_PROSPECTIVE_DIAGNOSTIC",
            "ledger_slice_sha256": digest(sorted(records, key=lambda row: row["forecast_id"])),
            "selected_forecasts": len(records), "rejected_counts": dict(sorted(rejected.items())),
            "comparison_contract": "Earliest prospective forecast per ticker/origin/horizon/model. Pair on ticker, origin, target, horizon, exact cutoff, and feature version. Fixed ex-ante or stored momentum baselines; no hindsight majority baseline.",
            "comparisons": comparisons, "cohort_rows": list(grouped.values()), "source_records": records,
            "promotion_eligible": False,
            "limitations": ["Small prospective cohorts cannot establish stable lift.",
                           "Origin weighting does not remove serial dependence or overlapping labels.",
                           "Legacy stored direction labels remain intact; numeric-center comparisons use their own contract.",
                           "Intervals, option fills, calibration and corporate actions are not validated by these comparisons."]}
