"""Aggregate REST minute buckets, never repeated snapshots or streaming partials."""

from datetime import UTC, datetime, timedelta
import math
from typing import Any, Mapping, Sequence


VERSION = "rest-minute-greek-flow-v3"
FIELDS = ("dir_delta_flow", "dir_vega_flow", "otm_dir_delta_flow", "otm_dir_vega_flow", "volume", "transactions")


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def summarize(rows: Sequence[Mapping[str, Any]], *, cutoff_at: datetime | None = None) -> dict[str, Any]:
    from .intraday import market_session

    if cutoff_at is not None and cutoff_at.utcoffset() is None:
        raise ValueError("Greek-flow cutoff must be timezone-aware")
    buckets: dict[datetime, dict[str, float | None]] = {}
    rejected = 0
    outside_session = 0
    unfinished = 0
    duplicates = 0
    for row in rows:
        try:
            timestamp = datetime.fromisoformat(str(row.get("timestamp", "")).replace("Z", "+00:00"))
            if timestamp.utcoffset() is None or timestamp.second or timestamp.microsecond:
                raise ValueError("invalid minute timestamp")
            timestamp = timestamp.astimezone(UTC)
        except ValueError:
            rejected += 1
            continue
        if cutoff_at is not None and timestamp + timedelta(minutes=1) > cutoff_at:
            unfinished += 1
            continue
        session = market_session(timestamp)
        if not session.is_open_at(timestamp):
            if session.is_regular_session:
                outside_session += 1
            else:
                rejected += 1
            continue
        values = {field: _number(row.get(field)) for field in FIELDS}
        if timestamp in buckets:
            if buckets[timestamp] != values:
                raise ValueError("conflicting duplicate REST Greek-flow minute; revision order is unknown")
            duplicates += 1
        buckets[timestamp] = values
    if not buckets:
        return {"quality": "empty", "aggregation_version": VERSION, "coverage_status": "UNAVAILABLE", "rejected_rows": rejected, "out_of_session_rows_excluded": outside_session}
    last = max(buckets)
    session = market_session(last)
    ordered = sorted((t, row) for t, row in buckets.items() if session.opens_at <= t < session.closes_at)
    stop = min(cutoff_at or last + timedelta(minutes=1), session.closes_at).replace(second=0, microsecond=0)
    expected = max(0, int((stop - session.opens_at).total_seconds() // 60))
    missing = max(0, expected - len(ordered))

    def totals(selected: Sequence[tuple[datetime, Mapping[str, Any]]]) -> dict[str, float | None]:
        return {field: sum(row[field] for _, row in selected) if selected and all(row[field] is not None for _, row in selected) else None for field in FIELDS}

    total = totals(ordered)
    recent = totals([(t, row) for t, row in ordered if t >= stop - timedelta(minutes=30)])
    quarter_start = session.opens_at + (stop - session.opens_at) * .75
    quarter = totals([(t, row) for t, row in ordered if t >= quarter_start])
    delta = total["dir_delta_flow"]
    sign = 1 if delta is not None and delta > 0 else -1 if delta is not None and delta < 0 else 0
    valid = [row["dir_delta_flow"] for _, row in ordered if row["dir_delta_flow"] is not None]
    persistence = sum((v > 0 if sign > 0 else v < 0) for v in valid) / len(valid) if valid and sign else None
    valid_fields = total["dir_delta_flow"] is not None and total["dir_vega_flow"] is not None
    coverage = "COMPLETE_SESSION" if missing == 0 and stop >= session.closes_at else "COMPLETE_TO_CUTOFF" if missing == 0 else "PARTIAL_OR_UNVERIFIED"
    return {
        "quality": "observed" if valid_fields else "missing_fields",
        "aggregation_version": VERSION,
        "coverage_status": coverage,
        "confirmation_eligible": valid_fields and missing == 0 and rejected == 0,
        "session_date": session.session_date.isoformat(),
        "row_count": len(ordered), "expected_minutes": expected, "missing_minutes": missing,
        "duplicate_rows_removed": duplicates, "rejected_rows": rejected,
        "out_of_session_rows_excluded": outside_session,
        "unfinished_or_future_minutes_excluded": unfinished,
        "first_timestamp": ordered[0][0].isoformat(), "final_timestamp": last.isoformat(),
        "directional_delta_flow": delta,
        "directional_vega_flow": total["dir_vega_flow"],
        "otm_directional_delta_flow": total["otm_dir_delta_flow"],
        "otm_delta_share": abs(total["otm_dir_delta_flow"] / delta) if total["otm_dir_delta_flow"] is not None and delta not in {None, 0} else None,
        "delta_sign_persistence": persistence,
        "last_quarter_delta_flow": quarter["dir_delta_flow"],
        "last_quarter_delta_change": None,
        "session_totals": total, "recent_30m_totals": recent,
        "latest_minute": ordered[-1][1],
        "transactions": total["transactions"], "volume": total["volume"],
        "caveat": "Sum of distinct stored REST minutes, not a running provider total. Missing minutes are not zero activity. OTM/net delta ratio can exceed one. Classification does not establish opening intent or predictive value.",
    }
