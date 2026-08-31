#!/usr/bin/env python3
"""Validate and merge evidence-bound Codex research into a morning_run/v1 artifact."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from private_artifacts import write_private_bytes


ENRICHMENT_SCHEMA = "codex_agent_enrichment/v1"
ALLOWED_POSTURES = {"BULLISH_TREND", "BEARISH_TREND", "MIXED", "NEUTRAL"}
RECORD_KEYS = {
    "ticker", "action", "posture", "research_priority", "evidence_confidence",
    "day_outlook", "summary", "evidence_points", "counterevidence", "scenarios",
    "option_context", "unknowns",
}
EVIDENCE_KEYS = {"statement", "field_refs", "source_snapshot_ids"}
SCENARIO_KEYS = {"name", "conditions", "outcome", "invalidation"}
FORBIDDEN_ACTION_LANGUAGE = re.compile(
    r"\b(?:should|recommend(?:s|ed|ing)?|instruction\s+to)\s+"
    r"(?:buy|sell|enter|exit|short|purchase)\b"
    r"|\b(?:buy|sell|short)\s+(?:setup|signal|now|today|shares?|stock|option|contract)\b"
    r"|\b(?:enter|exit|purchase)\s+(?:now|today|shares?|stock|option|contract)\b",
    re.IGNORECASE,
)


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _array(value: Any, label: str, *, minimum: int, maximum: int) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{label} must contain {minimum} to {maximum} items")
    return value


def _text(value: Any, label: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise ValueError(f"{label} exceeds {maximum} characters")
    if FORBIDDEN_ACTION_LANGUAGE.search(cleaned):
        raise ValueError(f"{label} contains trade-action language")
    return cleaned


def _strings(value: Any, label: str, *, minimum: int, maximum: int) -> list[str]:
    return [
        _text(item, f"{label}[{index}]")
        for index, item in enumerate(_array(value, label, minimum=minimum, maximum=maximum))
    ]


def _string_list(value: Any, label: str, *, minimum: int, maximum: int) -> list[str]:
    if isinstance(value, str):
        return [_text(value, f"{label}[0]")]
    return _strings(value, label, minimum=minimum, maximum=maximum)


def _score(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ValueError(f"{label} must be an integer from 0 to 100")
    return value


def _resolve_field(record: Mapping[str, Any], path: str) -> Any:
    current: Any = record
    for component in path.split("."):
        match = re.fullmatch(r"([^\[\]]+)(?:\[(\d+)\])?", component)
        if match is None or not isinstance(current, Mapping) or match.group(1) not in current:
            raise ValueError(f"unknown evidence field path: {path}")
        current = current[match.group(1)]
        if match.group(2) is not None:
            index = int(match.group(2))
            if not isinstance(current, list) or index >= len(current):
                raise ValueError(f"unknown evidence field path: {path}")
            current = current[index]
    return current


def _validate_record(candidate: Any, source: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(_object(candidate, "enrichment record"))
    if set(item) != RECORD_KEYS:
        raise ValueError(f"{source.get('ticker')} enrichment keys do not match the v1 contract")
    ticker = str(source.get("ticker", ""))
    if item["ticker"] != ticker:
        raise ValueError(f"enrichment ticker mismatch for {ticker}")
    if item["action"] != "NO_RECOMMENDATION":
        raise ValueError(f"{ticker} action must remain NO_RECOMMENDATION")
    if item["posture"] not in ALLOWED_POSTURES:
        raise ValueError(f"{ticker} has unsupported posture")
    item["research_priority"] = _score(item["research_priority"], f"{ticker}.research_priority")
    item["evidence_confidence"] = _score(item["evidence_confidence"], f"{ticker}.evidence_confidence")
    item["day_outlook"] = _text(item["day_outlook"], f"{ticker}.day_outlook", maximum=350)
    if "prior-session" not in item["day_outlook"].lower():
        raise ValueError(f"{ticker}.day_outlook must explicitly say prior-session")
    item["summary"] = _text(item["summary"], f"{ticker}.summary", maximum=400)
    item["counterevidence"] = _strings(item["counterevidence"], f"{ticker}.counterevidence", minimum=2, maximum=4)
    item["unknowns"] = _strings(item["unknowns"], f"{ticker}.unknowns", minimum=1, maximum=4)
    item["option_context"] = _text(item["option_context"], f"{ticker}.option_context", maximum=500)
    option_context_lower = item["option_context"].lower()
    if "non-actionable" not in option_context_lower and "not actionable" not in option_context_lower:
        raise ValueError(f"{ticker}.option_context must explicitly say non-actionable or not actionable")

    allowed_ids = {
        value for value in _object(source.get("provenance"), f"{ticker}.provenance").get("snapshot_ids", [])
        if isinstance(value, int) and not isinstance(value, bool)
    }
    evidence_points: list[dict[str, Any]] = []
    for index, raw_point in enumerate(_array(item["evidence_points"], f"{ticker}.evidence_points", minimum=2, maximum=5)):
        point = dict(_object(raw_point, f"{ticker}.evidence_points[{index}]"))
        if set(point) != EVIDENCE_KEYS:
            raise ValueError(f"{ticker}.evidence_points[{index}] keys do not match the v1 contract")
        point["statement"] = _text(point["statement"], f"{ticker}.evidence_points[{index}].statement")
        refs = _strings(point["field_refs"], f"{ticker}.evidence_points[{index}].field_refs", minimum=1, maximum=8)
        for ref in refs:
            _resolve_field(source, ref)
        ids = _array(point["source_snapshot_ids"], f"{ticker}.evidence_points[{index}].source_snapshot_ids", minimum=1, maximum=8)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in ids):
            raise ValueError(f"{ticker} evidence source IDs must be integers")
        if not set(ids).issubset(allowed_ids):
            raise ValueError(f"{ticker} evidence cites a snapshot outside the current capture")
        point["field_refs"] = refs
        point["source_snapshot_ids"] = ids
        evidence_points.append(point)
    item["evidence_points"] = evidence_points

    raw_scenarios: Any = item["scenarios"]
    if isinstance(raw_scenarios, Mapping):
        if set(raw_scenarios) != {"BULL", "BASE", "BEAR"}:
            raise ValueError(f"{ticker} requires exactly BULL, BASE, and BEAR scenarios")
        raw_scenarios = [raw_scenarios[name] for name in ("BULL", "BASE", "BEAR")]
    scenarios: list[dict[str, Any]] = []
    for index, raw_scenario in enumerate(_array(raw_scenarios, f"{ticker}.scenarios", minimum=3, maximum=3)):
        scenario = dict(_object(raw_scenario, f"{ticker}.scenarios[{index}]"))
        if set(scenario) != SCENARIO_KEYS:
            raise ValueError(f"{ticker}.scenarios[{index}] keys do not match the v1 contract")
        if scenario["name"] not in {"BULL", "BASE", "BEAR"}:
            raise ValueError(f"{ticker} has unsupported scenario name")
        scenario["conditions"] = _string_list(scenario["conditions"], f"{ticker}.{scenario['name']}.conditions", minimum=1, maximum=4)
        scenario["outcome"] = _text(scenario["outcome"], f"{ticker}.{scenario['name']}.outcome")
        scenario["invalidation"] = _string_list(scenario["invalidation"], f"{ticker}.{scenario['name']}.invalidation", minimum=1, maximum=4)
        scenarios.append(scenario)
    if {scenario["name"] for scenario in scenarios} != {"BULL", "BASE", "BEAR"}:
        raise ValueError(f"{ticker} requires exactly BULL, BASE, and BEAR scenarios")
    item["scenarios"] = scenarios
    return item


def enrich(run: Mapping[str, Any], batches: Sequence[Mapping[str, Any]], *, input_digest: str) -> dict[str, Any]:
    if run.get("run_schema_version") != "morning_run/v1":
        raise ValueError("input must use morning_run/v1")
    watchlist = run.get("watchlist")
    if not isinstance(watchlist, list) or not watchlist:
        raise ValueError("input watchlist is missing")
    source_by_ticker = {str(item.get("ticker")): item for item in watchlist if isinstance(item, Mapping)}
    candidates: dict[str, Any] = {}
    for batch_index, batch in enumerate(batches):
        if batch.get("schema") != ENRICHMENT_SCHEMA:
            raise ValueError(f"analysis batch {batch_index} has an unsupported schema")
        for item in _array(batch.get("records"), f"analysis batch {batch_index}.records", minimum=1, maximum=20):
            ticker = str(_object(item, "analysis record").get("ticker", ""))
            if ticker not in source_by_ticker or ticker in candidates:
                raise ValueError(f"unexpected or duplicate analysis ticker: {ticker}")
            candidates[ticker] = item
    missing = set(source_by_ticker) - set(candidates)
    if missing:
        raise ValueError(f"analysis is missing tickers: {sorted(missing)}")

    validated = {
        ticker: _validate_record(candidates[ticker], source_by_ticker[ticker])
        for ticker in sorted(source_by_ticker)
    }
    ranking = sorted(
        validated,
        key=lambda ticker: (
            -validated[ticker]["research_priority"],
            -validated[ticker]["evidence_confidence"],
            ticker,
        ),
    )
    rank_by_ticker = {ticker: index + 1 for index, ticker in enumerate(ranking)}
    enriched_watchlist: list[dict[str, Any]] = []
    for source in watchlist:
        record = dict(source)
        ticker = str(record["ticker"])
        record["research_rank"] = rank_by_ticker[ticker]
        record["agent_enrichment"] = validated[ticker]
        record["agent_enrichment_validated"] = True
        enriched_watchlist.append(record)
    result = dict(run)
    result["watchlist"] = enriched_watchlist
    result["agentic_analysis"] = {
        "schema": ENRICHMENT_SCHEMA,
        "backend": "interactive_codex_analysis",
        "critic": "deterministic-contract-validator-v1",
        "input_sha256": input_digest,
        "validated_at": datetime.now(UTC).isoformat(),
        "probabilities_calibrated": False,
        "recommendations_enabled": False,
        "ranking_semantics": "relative rank of the uncalibrated directional thesis; not probability, expected return, or an execution instruction",
    }
    return result


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False).encode() + b"\n"
    write_private_bytes(path, encoded)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--analysis", required=True, action="append", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    raw = args.input.read_bytes()
    run = json.loads(raw)
    batches = [json.loads(path.read_text(encoding="utf-8")) for path in args.analysis]
    if not isinstance(run, Mapping) or any(not isinstance(batch, Mapping) for batch in batches):
        raise SystemExit("input and analysis JSON roots must be objects")
    enriched = enrich(run, batches, input_digest=hashlib.sha256(raw).hexdigest())
    _write_atomic(args.output, enriched)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
