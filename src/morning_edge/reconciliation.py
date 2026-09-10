"""Explain price eligibility without repairing data or granting verification."""
from collections import Counter
from datetime import date
import hashlib
import json
from math import isclose

from .clock import is_nyse_session
from .experiments import digest, number, sessions
from .features import DailyBar
from .freshness import latest_complete_session
from .models import timestamp_from_text, timestamp_text
from .normalization import _payload_rows

FIELDS = ("open", "high", "low", "close", "volume")


def inspect_price_source(source, selected, cutoff):
    """Validate one raw capture; equality with this provider is not corroboration."""
    metadata = json.loads(source["metadata_json"])
    result = {"snapshot_id": source["id"], "provider": source["provider"],
              "raw_payload_hash": source["raw_payload_hash"],
              "metadata_sha256": hashlib.sha256(source["metadata_json"].encode()).hexdigest(),
              "as_of": source["as_of"], "retrieved_at": source["retrieved_at"],
              "requested_market_date": metadata.get("requested_market_date"),
              "historical_scope_verified": metadata.get("historical_scope_verified") is True,
              "scope_excluded": metadata.get("backfill_plan_id") is not None
                                and metadata.get("historical_scope_verified") is not True,
              "source_issues": [], "row_issues": {}, "overlap_rows": 0,
              "close_conflict_dates": [], "field_conflict_counts": {}}
    if hashlib.sha256(source["content_json"].encode()).hexdigest() != source["raw_payload_hash"]:
        result["source_issues"].append("PAYLOAD_HASH_MISMATCH")
        return result, {}
    if max(timestamp_from_text(source[key]) for key in ("as_of", "retrieved_at")) > cutoff:
        result["source_issues"].append("SOURCE_AFTER_CUTOFF")
        return result, {}
    valid, counts, seen, duplicate_dates = {}, Counter(), set(), set()
    for row in _payload_rows(json.loads(source["content_json"])):
        if row.get("market_time") != "r":
            counts["NON_REGULAR_ROW"] += 1
            continue
        try:
            day = date.fromisoformat(row["date"])
            if not is_nyse_session(day):
                counts["NON_SESSION"] += 1
                continue
            values = [number(row.get(key)) for key in FIELDS]
            if any(value is None for value in values):
                raise ValueError("invalid price")
            DailyBar(day, *values, timestamp_from_text(source["retrieved_at"]))
        except (KeyError, TypeError, ValueError):
            counts["INVALID_REGULAR_ROW"] += 1
            continue
        day = day.isoformat()
        if day in seen:
            duplicate_dates.add(day)
        seen.add(day)
        valid[day] = dict(zip(FIELDS, values))
    for day in duplicate_dates:
        valid.pop(day, None)
    if duplicate_dates:
        counts["DUPLICATE_REGULAR_SESSION"] = len(duplicate_dates)
    result["row_issues"] = dict(sorted(counts.items()))
    result["duplicate_dates"] = sorted(duplicate_dates)
    result["valid_regular_rows"] = len(valid)
    result["first"] = min(valid) if valid else None
    result["last"] = max(valid) if valid else None
    complete_at_capture = latest_complete_session(timestamp_from_text(source["retrieved_at"])).isoformat()
    result["not_final_at_capture_dates"] = sorted(day for day in valid if day > complete_at_capture)
    requested = metadata.get("requested_market_date")
    result["rows_after_requested_date"] = None
    if requested is not None:
        try:
            requested = date.fromisoformat(requested).isoformat()
            result["rows_after_requested_date"] = sum(day > requested for day in valid)
        except (TypeError, ValueError):
            result["source_issues"].append("INVALID_REQUESTED_DATE")
    conflicts = Counter()
    overlap = sorted(set(valid) & set(selected))
    for day in overlap:
        for field in FIELDS:
            a, b = valid[day][field], number(selected[day].get(field))
            if b is None or (a != b if field == "volume" else not isclose(a, b, rel_tol=1e-8, abs_tol=1e-8)):
                conflicts[field] += 1
                if field == "close":
                    result["close_conflict_dates"].append(day)
    result.update(overlap_rows=len(overlap), overlap_first=overlap[0] if overlap else None,
                  overlap_last=overlap[-1] if overlap else None,
                  field_conflict_counts=dict(sorted(conflicts.items())))
    return result, valid


def reconcile_inputs(connection, dataset):
    """Use the caller's read-only snapshot transaction and normalized dataset."""
    cutoff = timestamp_from_text(dataset["capture_cutoff"])
    cutoff_text = timestamp_text(cutoff)
    tickers = []
    for ticker, normalized in sorted(dataset["series"].items()):
        selected = {row["date"]: row for row in normalized}
        if len(selected) != len(normalized):
            raise ValueError("duplicate normalized reconciliation session")
        calendar = sessions(date.fromisoformat(min(selected)), date.fromisoformat(max(selected))) if selected else []
        gaps = sorted(set(calendar)-set(selected))
        sources, candidates = [], {day: [] for day in gaps}
        query = """SELECT s.id,s.provider,s.as_of,s.retrieved_at,s.metadata_json,
                   s.raw_payload_hash,p.content_json FROM snapshots s
                   JOIN raw_payloads p ON p.content_hash=s.raw_payload_hash
                   WHERE s.symbol=? AND s.dataset='ohlc' AND s.as_of<=? AND s.retrieved_at<=?
                   ORDER BY s.id"""
        for source in connection.execute(query, (ticker, cutoff_text, cutoff_text)):
            inspected, valid = inspect_price_source(source, selected, cutoff)
            sources.append(inspected)
            for day in gaps:
                if day not in valid:
                    continue
                blockers = list(inspected["source_issues"])
                if inspected["scope_excluded"]:
                    blockers.append("HISTORICAL_SCOPE_UNVERIFIED")
                if inspected["close_conflict_dates"]:
                    blockers.append("OVERLAP_CLOSE_CONFLICT")
                if inspected["rows_after_requested_date"]:
                    blockers.append("ROWS_AFTER_REQUESTED_DATE")
                if day in inspected["not_final_at_capture_dates"]:
                    blockers.append("NOT_FINAL_AT_CAPTURE")
                candidates[day].append({"snapshot_id": source["id"], "raw_payload_hash": source["raw_payload_hash"],
                    "row_sha256": digest({"date": day, **valid[day]}), "close": valid[day]["close"],
                    "overlap_rows": inspected["overlap_rows"],
                    "overlap_close_conflicts": len(inspected["close_conflict_dates"]),
                    "blockers": blockers, "eligible_for_automatic_repair": False})
        gap_rows = []
        for day, rows in candidates.items():
            values = [row["close"] for row in rows]
            conflict = bool(values) and not all(isclose(values[0], value, rel_tol=1e-8, abs_tol=1e-8) for value in values)
            gap_rows.append({"date": day, "status": "CONFLICTING_RAW_CANDIDATES" if conflict else
                "RAW_CANDIDATE_REQUIRES_REVIEW" if rows else "NO_VALID_CUTOFF_SAFE_RAW_CANDIDATE",
                "candidates": rows, "distinct_payloads": len({row["raw_payload_hash"] for row in rows}),
                "required_review": ["Verify requested scope and row dates.",
                                    "Confirm security identity and price adjustment basis.",
                                    "Use independent evidence; same-provider overlap is not independent validation.",
                                    "Preserve original sources and actual retrieval time; do not relabel as prospective."]})
        references = []
        for raw in connection.execute("""SELECT id,dataset,metadata_json,raw_payload_hash FROM snapshots
                WHERE symbol=? AND dataset IN ('security_identity','corporate_action')
                AND as_of<=? AND retrieved_at<=? ORDER BY id""", (ticker, cutoff_text, cutoff_text)):
            meta = json.loads(raw["metadata_json"])
            references.append({"snapshot_id": raw["id"], "dataset": raw["dataset"],
                "raw_payload_hash": raw["raw_payload_hash"],
                "kind": meta.get("reference_kind", meta.get("enhanced_dataset", "UNKNOWN"))})
        tickers.append({"ticker": ticker, "normalized_rows": len(normalized), "gaps": gap_rows,
            "sources": sources, "reference_inventory": references,
            "identity_status": "CAPTURED_NOT_RECONCILED" if any(r["dataset"] == "security_identity" for r in references) else "NOT_CAPTURED_FOR_SYMBOL",
            "corporate_action_status": "CAPTURED_NOT_RECONCILED" if any(r["dataset"] == "corporate_action" for r in references) else "NOT_CAPTURED_FOR_SYMBOL"})
    return {"schema_version": "price-reconciliation-v1", "status": "REVIEW_REQUIRED",
        "capture_cutoff": dataset["capture_cutoff"], "dataset_sha256": digest(dataset),
        "tickers": tickers, "automatic_repairs": 0, "promotion_eligible": False,
        "boundary": "Read-only diagnostics of cutoff-safe stored versions. No verification flag is granted. Reference inventory is symbol-scoped metadata, not validation of payloads or proof of complete corporate-action coverage. Gaps are internal to observed history; missing leading/trailing history is not inferred.",
        "comparison_tolerance": {"price_relative": 1e-8, "price_absolute": 1e-8, "volume": "exact"}}
