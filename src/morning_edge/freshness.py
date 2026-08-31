"""Session-aware freshness labels for point-in-time dashboard evidence."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Mapping

from .clock import NEW_YORK, eastern, is_nyse_session, previous_nyse_session


COMPLETE_SESSION_GRACE = time(16, 15)


def latest_complete_session(cutoff_at: datetime) -> date:
    cutoff = eastern(cutoff_at)
    if is_nyse_session(cutoff.date()) and cutoff.time() >= COMPLETE_SESSION_GRACE:
        return cutoff.date()
    return previous_nyse_session(cutoff.date(), include_current=False)


def _parse_date(value: object) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return eastern(value).date()
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _session_lag(complete: date, observed: date) -> int | None:
    if observed > complete:
        return None
    lag = 0
    cursor = complete
    while cursor > observed and lag <= 500:
        cursor = previous_nyse_session(cursor, include_current=False)
        lag += 1
    return lag if cursor == observed else None


def dataset_freshness(
    *, cutoff_at: datetime, price_session: object, dataset_dates: Mapping[str, object]
) -> dict[str, object]:
    """Classify dates against the latest session that can be complete at cutoff."""

    cutoff = eastern(cutoff_at)
    complete = latest_complete_session(cutoff)
    observed_dates = {"price": price_session, **dict(dataset_dates)}
    datasets: dict[str, dict[str, object]] = {}
    for name, raw_date in observed_dates.items():
        observed = _parse_date(raw_date)
        if observed is None:
            status, lag = "UNAVAILABLE", None
        elif observed > complete:
            status, lag = "INTRADAY_PARTIAL", None
        else:
            lag = _session_lag(complete, observed)
            status = "CURRENT_COMPLETE" if lag == 0 else "PRIOR_SESSION" if lag == 1 else "STALE"
        datasets[str(name)] = {
            "status": status,
            "observed_session": observed.isoformat() if observed else None,
            "session_lag": lag,
        }
    statuses = {item["status"] for item in datasets.values()}
    if "STALE" in statuses or "UNAVAILABLE" in statuses:
        overall = "DEGRADED"
    elif "PRIOR_SESSION" in statuses:
        overall = "MIXED_SESSION"
    elif "INTRADAY_PARTIAL" in statuses:
        overall = "INTRADAY_PARTIAL"
    else:
        overall = "CURRENT_COMPLETE"
    return {
        "overall": overall,
        "cutoff_et": cutoff.isoformat(),
        "latest_complete_session": complete.isoformat(),
        "datasets": datasets,
        "meaning": "Dates are compared with the latest regular session that could be complete at the scoring cutoff.",
    }
