"""Trial-time availability, schema, and timestamp audit for Codex Screener feeds."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from .providers.base import ProviderError
from .providers.unusual_whales import (
    EndpointResponse,
    UnusualWhalesClient,
    gex_levels_are_empty,
    gex_unusable_level_names,
)


class AuditStatus(str, Enum):
    AVAILABLE = "available"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    SCHEMA_MISMATCH = "schema_mismatch"
    SCOPE_UNVERIFIED = "scope_unverified"


@dataclass(frozen=True)
class TimestampCheck:
    field: str
    documented_meaning: str
    samples: tuple[str, ...]
    status: AuditStatus


@dataclass(frozen=True)
class EndpointAuditResult:
    dataset: str
    endpoint: str
    status: AuditStatus
    fetched_at: str | None
    http_status: int | None
    row_count: int | None
    rate_limit_remaining: int | None
    timestamp_checks: tuple[TimestampCheck, ...]
    requested_scope: str | None = None
    scope_parameter_applied: bool | None = None
    note: str | None = None


@dataclass(frozen=True)
class TrialAuditReport:
    ticker: str
    requested_date: str | None
    generated_at: str
    results: tuple[EndpointAuditResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# These meanings are taken from the official endpoint documents listed in the
# companion endpoint-audit document. They are assertions to verify against trial
# payloads, not substitute market timestamps.
TIMESTAMP_SEMANTICS: dict[str, tuple[tuple[str, str], ...]] = {
    "option_chain": (
        (
            "last_tape_time",
            "provider timestamp for the option-state row; live payload observed as ISO-8601 UTC",
        ),
    ),
    "flow_alerts": (("created_at", "UTC timestamp at which the provider created the flow alert"),),
    "option_trade_tape": (("executed_at", "timestamp at which an underlying option trade executed"),),
    "oi_change": (
        ("curr_date", "current OI source date, ISO date"),
        ("last_date", "previous OI source date, ISO date"),
    ),
    "gex_levels": (
        ("date", "provider market date for the GEX level set, ISO date"),
        ("time", "provider observation time for the GEX level set, timezone-aware ISO-8601"),
    ),
    "darkpool": (
        ("executed_at", "time the print hit the public tape, with timezone"),
        ("trf_executed_at", "actual TRF execution time; available only from 2025-05-01 onward"),
    ),
    "ohlc": (("end_time", "candle end time as a UTC timestamp"),),
    "news": (("created_at", "timestamp when the headline was created or published"),),
    "earnings": (
        ("report_date", "earnings report date"),
        ("report_time", "reported release timing, such as premarket or postmarket"),
    ),
}

# The current daily OHLC response identifies its observation at session-day
# granularity. ``date`` is accepted as an alias for audit coverage, but is not
# normalized as an ``end_time``: it has no intraday UTC instant. Keeping the raw
# field name in TimestampCheck makes that loss of precision visible downstream.
TIMESTAMP_ALIASES: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {
    "ohlc": {
        "end_time": (
            (
                "date",
                "trading-session date (day-granularity); not a UTC candle-end instant",
            ),
        ),
    },
}


def _rows(response: EndpointResponse) -> list[dict[str, Any]]:
    data = response.data
    return data if isinstance(data, list) else [data] if isinstance(data, dict) else []


def _timestamp_checks(dataset: str, response: EndpointResponse) -> tuple[TimestampCheck, ...]:
    rows = _rows(response)
    checks: list[TimestampCheck] = []
    for field, meaning in TIMESTAMP_SEMANTICS[dataset]:
        candidates = ((field, meaning),) + TIMESTAMP_ALIASES.get(dataset, {}).get(field, ())
        matched_field, matched_meaning = candidates[0]
        values: tuple[str, ...] = ()
        for candidate_field, candidate_meaning in candidates:
            candidate_values = tuple(
                str(row[candidate_field]) for row in rows[:3] if row.get(candidate_field) is not None
            )
            if candidate_values:
                matched_field, matched_meaning, values = (
                    candidate_field,
                    candidate_meaning,
                    candidate_values,
                )
                break
        checks.append(
            TimestampCheck(
                field=matched_field,
                documented_meaning=matched_meaning,
                samples=values,
                status=AuditStatus.AVAILABLE if values else AuditStatus.SCHEMA_MISMATCH,
            )
        )
    return tuple(checks)


def _result(
    dataset: str,
    response: EndpointResponse,
    *,
    requested_scope: str | None = None,
    scope_parameter_applied: bool | None = None,
) -> EndpointAuditResult:
    rows = _rows(response)
    gex_empty = dataset == "gex_levels" and gex_levels_are_empty(response.data)
    gex_unusable_levels = gex_unusable_level_names(response.data) if dataset == "gex_levels" else ()
    status = AuditStatus.EMPTY if gex_empty or not rows else AuditStatus.AVAILABLE
    timestamp_checks = _timestamp_checks(dataset, response)
    note: str | None = None
    if gex_empty:
        # Null walls/flips are a provider-declared absence of GEX coverage.
        # Timestamp fields are intentionally not required for that empty state.
        timestamp_checks = ()
        note = "Provider returned no usable GEX levels (all walls/flips are null)"
    elif gex_unusable_levels:
        status = AuditStatus.SCOPE_UNVERIFIED
        note = (
            "Provider returned a partial GEX level set; retain raw evidence but exclude it from derived GEX: "
            + ", ".join(gex_unusable_levels)
        )
    missing_timestamps = [
        check.field for check in timestamp_checks if check.status is not AuditStatus.AVAILABLE
    ]
    if rows and not gex_empty and not gex_unusable_levels and missing_timestamps:
        status = AuditStatus.SCHEMA_MISMATCH
        note = f"Missing required timestamp field(s): {', '.join(missing_timestamps)}"
    elif not gex_empty and not gex_unusable_levels and rows and requested_scope is not None and scope_parameter_applied is False:
        status = AuditStatus.SCOPE_UNVERIFIED
        note = "Requested historical date was not applied to this endpoint probe"
    elif not gex_empty and not gex_unusable_levels and rows and requested_scope is not None and scope_parameter_applied is True:
        note = "Historical date parameter was sent; verify returned rows honor it"
    return EndpointAuditResult(
        dataset=dataset,
        endpoint=response.endpoint,
        status=status,
        fetched_at=response.response.raw.fetched_at.astimezone(UTC).isoformat(),
        http_status=response.response.raw.status_code,
        row_count=len(rows),
        rate_limit_remaining=response.response.raw.rate_limit.remaining,
        timestamp_checks=timestamp_checks,
        requested_scope=requested_scope,
        scope_parameter_applied=scope_parameter_applied,
        note=note,
    )


def _unavailable(
    dataset: str,
    endpoint: str,
    error: Exception,
    *,
    requested_scope: str | None = None,
    scope_parameter_applied: bool | None = None,
) -> EndpointAuditResult:
    status = AuditStatus.SCHEMA_MISMATCH if error.__class__.__name__ == "ProviderSchemaError" else AuditStatus.UNAVAILABLE
    return EndpointAuditResult(
        dataset=dataset,
        endpoint=endpoint,
        status=status,
        fetched_at=None,
        http_status=None,
        row_count=None,
        rate_limit_remaining=None,
        timestamp_checks=(),
        requested_scope=requested_scope,
        scope_parameter_applied=scope_parameter_applied,
        # Do not include exception repr: transport implementations can include URLs
        # with user-selected query parameters. Provider code never includes tokens.
        note=f"{error.__class__.__name__}: {str(error)[:160]}",
    )


def run_trial_audit(
    client: UnusualWhalesClient,
    ticker: str,
    *,
    as_of: date | str | None = None,
) -> TrialAuditReport:
    """Probe all required Codex Screener datasets without making a trading decision.

    Each endpoint is isolated so a missing entitlement or stale dataset becomes a
    visible audit result instead of silently affecting a recommendation score.
    The function is intentionally synchronous and suitable for a scheduled
    snapshot task after the user has supplied an API key through the environment.
    """

    requested_date = as_of.isoformat() if isinstance(as_of, date) else as_of
    probes: tuple[tuple[str, str, bool, Callable[[], EndpointResponse]], ...] = (
        ("option_chain", f"/api/stock/{ticker}/option-chains", True, lambda: client.option_chain(ticker, as_of=as_of, greeks=True)),
        ("flow_alerts", "/api/option-trades/flow-alerts", False, lambda: client.flow_alerts(ticker, unusual=False, limit=100)),
        ("option_trade_tape", "/api/option-trades", False, lambda: client.option_trades(ticker, limit=500)),
        ("oi_change", f"/api/stock/{ticker}/oi-change", True, lambda: client.oi_change(ticker, as_of=as_of, limit=500)),
        ("gex_levels", f"/api/stock/{ticker}/gex-levels", True, lambda: client.gex_levels(ticker, as_of=as_of)),
        ("darkpool", f"/api/darkpool/{ticker}", True, lambda: client.darkpool_trades(ticker, as_of=as_of, limit=500)),
        ("ohlc", f"/api/stock/{ticker}/ohlc/1d", True, lambda: client.ohlc(ticker, candle_size="1d", timeframe="1Y", end_date=as_of)),
        ("news", "/api/news/headlines", False, lambda: client.news_headlines(ticker, limit=100)),
        ("earnings", f"/api/earnings/{ticker}", False, lambda: client.earnings_history(ticker)),
    )
    results: list[EndpointAuditResult] = []
    for dataset, endpoint, supports_historical_scope, probe in probes:
        try:
            results.append(
                _result(
                    dataset,
                    probe(),
                    requested_scope=requested_date,
                    scope_parameter_applied=(supports_historical_scope if requested_date else None),
                )
            )
        except ProviderError as error:
            results.append(
                _unavailable(
                    dataset,
                    endpoint,
                    error,
                    requested_scope=requested_date,
                    scope_parameter_applied=(supports_historical_scope if requested_date else None),
                )
            )
        except ValueError as error:
            # Parameter rejection is also an audit failure, but isolate it from
            # other coverage checks.
            results.append(
                _unavailable(
                    dataset,
                    endpoint,
                    error,
                    requested_scope=requested_date,
                    scope_parameter_applied=(supports_historical_scope if requested_date else None),
                )
            )

    return TrialAuditReport(
        ticker=ticker.upper(),
        requested_date=requested_date,
        generated_at=datetime.now(UTC).isoformat(),
        results=tuple(results),
    )
