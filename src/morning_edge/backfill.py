"""Bounded, resumable raw historical collection.

This module stores *provider responses*, not derived option metrics.  It does
not interpret vendor fields, generate a score, or create a recommendation.
Historical endpoints must first pass the separate provider audit; otherwise a
date parameter alone is not evidence that a response represents that date.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
import json
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .models import Dataset, SnapshotEnvelope, canonical_json, payload_digest
from .providers.unusual_whales import (
    EndpointResponse,
    gex_levels_are_empty,
    gex_unusable_level_names,
)
from .store import SnapshotStore


class CoverageState(StrEnum):
    PLANNED = "planned"
    COLLECTED = "collected"
    EMPTY = "empty"
    FAILED = "failed"
    SCOPE_UNVERIFIED = "scope_unverified"
    BUDGET_EXHAUSTED = "budget_exhausted"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class BackfillDataset:
    name: str
    snapshot_dataset: Dataset
    daily: bool
    pagination_verified: bool
    historical_scope_requires_audit: bool = True
    anchor_to_end_date: bool = False


# These are endpoint families, not claims about the shape of individual rows.
# The option-chain endpoint is exceptional: its documented historical ``date``
# parameter and full-chain ``greeks=true`` response were confirmed in live
# probes at current, 30-, 60-, and 90-day scopes, and it exposes no pagination
# parameter. Each non-empty historical response is also checked against the
# requested date using every row's ``last_tape_time`` before it is marked
# complete.
# Other paginated families remain deliberately incomplete until separately
# verified.
BACKFILL_DATASETS: dict[str, BackfillDataset] = {
    "ohlc": BackfillDataset(
        "ohlc", Dataset.OHLC, daily=False, pagination_verified=True,
        anchor_to_end_date=True,
    ),
    "earnings": BackfillDataset("earnings", Dataset.EARNINGS, daily=False, pagination_verified=True),
    "option_chain": BackfillDataset("option_chain", Dataset.OPTION_CHAIN, daily=True, pagination_verified=True),
    "open_interest": BackfillDataset("open_interest", Dataset.OPEN_INTEREST, daily=True, pagination_verified=False),
    "dealer_exposure": BackfillDataset("dealer_exposure", Dataset.DEALER_EXPOSURE, daily=True, pagination_verified=True),
    "dark_pool": BackfillDataset("dark_pool", Dataset.DARK_POOL, daily=True, pagination_verified=False),
    "flow_alerts": BackfillDataset("flow_alerts", Dataset.OPTION_FLOW, daily=True, pagination_verified=True),
}


OI_PAGE_SIZE = 500
OI_MAX_PAGES_PER_ITEM = 100
DARK_POOL_PAGE_SIZE = 500
DARK_POOL_MAX_PAGES_PER_ITEM = 100
FLOW_ALERT_PAGE_SIZE = 200
FLOW_ALERT_MAX_PAGES_PER_ITEM = 50
NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class BackfillItem:
    plan_id: str
    ticker: str
    dataset: str
    requested_date: date | None

    @property
    def item_key(self) -> str:
        return payload_digest(
            {
                "plan_id": self.plan_id,
                "ticker": self.ticker,
                "dataset": self.dataset,
                "requested_date": self.requested_date.isoformat() if self.requested_date else None,
            }
        )

    def public_dict(self) -> dict[str, str | None]:
        return {
            "item_key": self.item_key,
            "ticker": self.ticker,
            "dataset": self.dataset,
            "requested_date": self.requested_date.isoformat() if self.requested_date else None,
        }


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    provider: str
    start_date: date
    end_date: date
    tickers: tuple[str, ...]
    datasets: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.provider != "unusual_whales":
            raise ValueError("historical collection currently supports unusual_whales only")
        if self.start_date > self.end_date:
            raise ValueError("start_date cannot be after end_date")
        if not self.tickers:
            raise ValueError("at least one ticker is required")
        if not self.datasets:
            raise ValueError("at least one dataset is required")
        unknown = set(self.datasets) - set(BACKFILL_DATASETS)
        if unknown:
            raise ValueError(f"unknown backfill dataset(s): {sorted(unknown)}")

    @property
    def plan_id(self) -> str:
        return payload_digest(self.definition())

    def definition(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "tickers": list(self.tickers),
            "datasets": list(self.datasets),
            "calendar_rule": "weekday_only; exchange holidays remain explicit provider coverage states",
            "schema_policy": "raw_response_only; no field mapping or derived analytics",
            "dataset_parameters": {
                "ohlc": {
                    "timeframe": "1Y",
                    "limit": 2500,
                    "anchor": "plan_end_date",
                    "provider_end_date_offset_days": -1,
                },
                "flow_alerts": {
                    "new_york_session_bounds": True,
                    "provider_unusual_preset": False,
                    "page_limit": FLOW_ALERT_PAGE_SIZE,
                    "cursor_pagination_version": 2,
                }
            },
        }

    def items(self) -> tuple[BackfillItem, ...]:
        weekdays = tuple(_weekdays(self.start_date, self.end_date))
        items: list[BackfillItem] = []
        for ticker in self.tickers:
            for dataset_name in self.datasets:
                definition = BACKFILL_DATASETS[dataset_name]
                dates: Iterable[date | None] = (
                    weekdays if definition.daily
                    else (self.end_date,) if definition.anchor_to_end_date
                    else (None,)
                )
                items.extend(
                    BackfillItem(self.plan_id, ticker, dataset_name, requested_date)
                    for requested_date in dates
                )
        return tuple(items)


def _weekdays(start_date: date, end_date: date) -> Iterable[date]:
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            yield current
        current += timedelta(days=1)


def make_plan(
    *, provider: str, start_date: date, end_date: date, tickers: Sequence[str], datasets: Sequence[str]
) -> BackfillPlan:
    clean_tickers = tuple(dict.fromkeys(ticker.strip().upper() for ticker in tickers if ticker.strip()))
    clean_datasets = tuple(dict.fromkeys(dataset.strip().lower() for dataset in datasets if dataset.strip()))
    return BackfillPlan(provider.strip().lower(), start_date, end_date, clean_tickers, clean_datasets)


class HistoricalClient(Protocol):
    def option_chain(self, ticker: str, *, as_of: date, greeks: bool = True) -> EndpointResponse: ...
    def oi_change(
        self, ticker: str, *, as_of: date, limit: int = 500, page: int | None = None,
        order: str = "desc",
    ) -> EndpointResponse: ...
    def gex_levels(self, ticker: str, *, as_of: date) -> EndpointResponse: ...
    def darkpool_trades(
        self, ticker: str, *, as_of: date, limit: int = 500,
        older_than: str | int | None = None, order: str = "desc", order_by: str = "executed_at",
    ) -> EndpointResponse: ...
    def ohlc(
        self, ticker: str, *, candle_size: str, timeframe: str,
        limit: int, end_date: date | str | None = None,
    ) -> EndpointResponse: ...
    def earnings_history(self, ticker: str) -> EndpointResponse: ...
    def flow_alerts(
        self, ticker: str, *, unusual: bool = False,
        newer_than: str | int | None = None, older_than: str | int | None = None,
        limit: int = 100, page: int | None = None,
    ) -> EndpointResponse: ...


class BackfillStore:
    """Append-only manifest and event log beside immutable raw snapshots."""

    def __init__(self, snapshots: SnapshotStore) -> None:
        self._snapshots = snapshots
        self._create_schema()

    def _create_schema(self) -> None:
        self._snapshots.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS backfill_manifests (
                plan_id TEXT PRIMARY KEY CHECK(length(plan_id) = 64),
                definition_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS backfill_events (
                id INTEGER PRIMARY KEY,
                plan_id TEXT NOT NULL REFERENCES backfill_manifests(plan_id),
                item_key TEXT NOT NULL CHECK(length(item_key) = 64),
                state TEXT NOT NULL CHECK(state IN (
                    'planned','collected','empty','failed','scope_unverified',
                    'budget_exhausted','skipped'
                )),
                snapshot_id INTEGER REFERENCES snapshots(id),
                details_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS backfill_events_lookup
                ON backfill_events (plan_id, item_key, id DESC);
            CREATE TRIGGER IF NOT EXISTS backfill_manifests_no_update
            BEFORE UPDATE ON backfill_manifests BEGIN
                SELECT RAISE(ABORT, 'backfill manifests are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS backfill_manifests_no_delete
            BEFORE DELETE ON backfill_manifests BEGIN
                SELECT RAISE(ABORT, 'backfill manifests are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS backfill_events_no_update
            BEFORE UPDATE ON backfill_events BEGIN
                SELECT RAISE(ABORT, 'backfill events are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS backfill_events_no_delete
            BEFORE DELETE ON backfill_events BEGIN
                SELECT RAISE(ABORT, 'backfill events are immutable');
            END;
            """
        )

    def register(self, plan: BackfillPlan) -> None:
        now = datetime.now(UTC).isoformat()
        self._snapshots.connection.execute(
            "INSERT OR IGNORE INTO backfill_manifests(plan_id, definition_json, created_at) VALUES (?, ?, ?)",
            (plan.plan_id, canonical_json(plan.definition()), now),
        )
        known = self.latest_states(plan.plan_id)
        for item in plan.items():
            if item.item_key not in known:
                self.record(item, CoverageState.PLANNED, details={"reason": "initial_plan"})

    def record(
        self,
        item: BackfillItem,
        state: CoverageState,
        *,
        snapshot_id: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self._snapshots.connection.execute(
            """INSERT INTO backfill_events(plan_id, item_key, state, snapshot_id, details_json, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (item.plan_id, item.item_key, state.value, snapshot_id, canonical_json(dict(details or {})), datetime.now(UTC).isoformat()),
        )

    def latest_states(self, plan_id: str) -> dict[str, CoverageState]:
        rows = self._snapshots.connection.execute(
            """SELECT item_key, state FROM backfill_events
               WHERE plan_id = ? AND id IN (
                   SELECT MAX(id) FROM backfill_events WHERE plan_id = ? GROUP BY item_key
               )""",
            (plan_id, plan_id),
        ).fetchall()
        return {str(row["item_key"]): CoverageState(row["state"]) for row in rows}

    def events(self, item: BackfillItem) -> list[tuple[CoverageState, int | None, dict[str, Any]]]:
        """Read immutable progress events for one item in insertion order."""

        rows = self._snapshots.connection.execute(
            """SELECT state, snapshot_id, details_json FROM backfill_events
               WHERE plan_id = ? AND item_key = ? ORDER BY id""",
            (item.plan_id, item.item_key),
        ).fetchall()
        return [
            (CoverageState(row["state"]), row["snapshot_id"], json.loads(row["details_json"]))
            for row in rows
        ]

    def coverage(self, plan: BackfillPlan) -> dict[str, Any]:
        states = self.latest_states(plan.plan_id)
        counts = {state.value: 0 for state in CoverageState}
        pending: list[dict[str, str | None]] = []
        for item in plan.items():
            state = states.get(item.item_key, CoverageState.PLANNED)
            counts[state.value] += 1
            if state is not CoverageState.COLLECTED:
                pending.append({**item.public_dict(), "state": state.value})
        return {
            "plan_id": plan.plan_id,
            "definition": plan.definition(),
            "total_items": len(plan.items()),
            "state_counts": counts,
            "incomplete_or_unverified": pending,
            "recommendations_enabled": False,
        }


def _call(client: HistoricalClient, item: BackfillItem) -> EndpointResponse:
    if item.dataset == "ohlc":
        return client.ohlc(
            item.ticker, candle_size="1d", timeframe="1Y",
            end_date=item.requested_date - timedelta(days=1) if item.requested_date else None,
            limit=2500,
        )
    if item.dataset == "earnings":
        return client.earnings_history(item.ticker)
    if item.requested_date is None:  # pragma: no cover - defended by plan construction
        raise ValueError(f"{item.dataset} requires a requested date")
    if item.dataset == "option_chain":
        return client.option_chain(item.ticker, as_of=item.requested_date, greeks=True)
    if item.dataset == "open_interest":
        return client.oi_change(item.ticker, as_of=item.requested_date, limit=500)
    if item.dataset == "dealer_exposure":
        return client.gex_levels(item.ticker, as_of=item.requested_date)
    if item.dataset == "dark_pool":
        return client.darkpool_trades(item.ticker, as_of=item.requested_date, limit=500)
    if item.dataset == "flow_alerts":
        start = datetime.combine(item.requested_date, time.min, tzinfo=NEW_YORK)
        end = start + timedelta(days=1)
        return client.flow_alerts(
            item.ticker,
            unusual=False,
            newer_than=start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            older_than=end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            limit=FLOW_ALERT_PAGE_SIZE,
        )
    raise ValueError(f"unsupported dataset: {item.dataset}")  # pragma: no cover


def _provider_timestamp_date(value: object) -> date:
    """Parse the provider's observed ISO or Unix timestamp into a UTC date."""

    if isinstance(value, bool):
        raise ValueError("timestamp must not be boolean")
    if isinstance(value, (int, float)):
        magnitude = abs(float(value))
        seconds = float(value) / 1_000 if magnitude >= 100_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=UTC).date()
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty ISO-8601 string or Unix value")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC).date()


def _historical_scope_check(item: BackfillItem, rows: object) -> tuple[bool, str | None]:
    if item.requested_date is None or rows == []:
        return True, None
    if item.dataset == "ohlc":
        if not isinstance(rows, list):
            return False, "ohlc_data_is_not_a_list"
        observed_dates: list[date] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                return False, f"ohlc_row_{index}_is_not_an_object"
            if row.get("market_time") not in (None, "r"):
                continue
            value = row.get("date")
            try:
                observed_dates.append(date.fromisoformat(str(value)[:10]))
            except ValueError:
                return False, f"ohlc_row_{index}_date_invalid"
        if not observed_dates:
            return False, "ohlc_regular_session_dates_absent"
        last_observed = max(observed_dates)
        if last_observed > item.requested_date:
            return False, f"ohlc_latest_date_{last_observed}_after_requested_{item.requested_date}"
        if (item.requested_date - last_observed).days > 7:
            return False, f"ohlc_latest_date_{last_observed}_too_far_before_requested_{item.requested_date}"
        return True, None
    if item.dataset not in {"option_chain", "flow_alerts"}:
        return True, None
    if not isinstance(rows, list):
        return False, f"{item.dataset}_data_is_not_a_list"
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            return False, f"{item.dataset}_row_{index}_is_not_an_object"
        timestamp_field = "last_tape_time" if item.dataset == "option_chain" else "created_at"
        try:
            if item.dataset == "flow_alerts":
                observed_date = _parse_provider_timestamp(row.get(timestamp_field)).astimezone(NEW_YORK).date()
            else:
                observed_date = _provider_timestamp_date(row.get(timestamp_field))
        except (OverflowError, OSError, TypeError, ValueError):
            return False, f"{item.dataset}_row_{index}_has_invalid_{timestamp_field}"
        if observed_date != item.requested_date:
            return False, (
                f"{item.dataset}_row_{index}_date_{observed_date.isoformat()}_"
                f"does_not_match_{item.requested_date.isoformat()}"
            )
    if item.dataset == "flow_alerts" and len(rows) >= FLOW_ALERT_PAGE_SIZE:
        return False, "flow_alerts_page_limit_reached; session_may_be_truncated"
    return True, None


def _response_metadata(
    item: BackfillItem,
    response: EndpointResponse,
    *,
    historical_scope_verified: bool,
    is_empty: bool = False,
    gex_unusable_levels: tuple[str, ...] = (),
) -> dict[str, Any]:
    definition = BACKFILL_DATASETS[item.dataset]
    raw = response.response.raw
    if item.requested_date is None:
        as_of_source = "retrieval_time; response rows are not normalized"
    elif item.dataset == "option_chain":
        as_of_source = (
            "requested_market_date_at_utc_midnight; response last_tape_time dates validated; "
            "historical scope confirmed by live probe; contract timestamps otherwise unnormalized"
        )
    elif item.dataset == "flow_alerts":
        as_of_source = (
            "requested_New_York_market_date; every alert created_at timestamp validated; "
            "complete alert page requested; opening and owner intent remain unverified"
        )
    elif item.dataset == "ohlc":
        as_of_source = (
            "requested_market_date_at_utc_midnight; regular-session row dates validated; "
            "provider OHLC end_date is sent one calendar day earlier because live probes "
            "show the endpoint returns the next session"
        )
    elif item.dataset == "dealer_exposure" and is_empty:
        as_of_source = (
            "requested_market_date_at_utc_midnight; all provider GEX levels were null; "
            "no provider observation time exists"
        )
    elif item.dataset == "dealer_exposure" and gex_unusable_levels:
        as_of_source = (
            "requested_market_date_at_utc_midnight; adapter validated provider GEX date/time "
            "against the requested session; partial level set is excluded from derived GEX"
        )
    elif item.dataset == "dealer_exposure":
        as_of_source = (
            "requested_market_date_at_utc_midnight; adapter validated provider GEX date/time "
            "against the requested session"
        )
    else:
        as_of_source = "requested_market_date_at_utc_midnight; provider timestamp semantics unverified"
    return {
        "endpoint": response.endpoint,
        "request_url": raw.url,
        "http_status": raw.status_code,
        "response_headers": dict(raw.headers),
        "request_attempts": raw.attempts,
        "requested_market_date": item.requested_date.isoformat() if item.requested_date else None,
        "historical_scope_status": (
            "verified_option_chain_date_scope"
            if item.dataset == "option_chain" and historical_scope_verified
            else "option_chain_date_scope_mismatch"
            if item.dataset == "option_chain"
            else "verified_flow_alert_date_scope"
            if item.dataset == "flow_alerts" and historical_scope_verified
            else "flow_alert_date_scope_or_page_limit_unverified"
            if item.dataset == "flow_alerts"
            else "verified_ohlc_end_date_scope"
            if item.dataset == "ohlc" and historical_scope_verified
            else "ohlc_end_date_scope_mismatch"
            if item.dataset == "ohlc"
            else "empty_gex_no_observation_time"
            if item.dataset == "dealer_exposure" and is_empty
            else "partial_gex_level_set"
            if item.dataset == "dealer_exposure" and gex_unusable_levels
            else "verified_gex_provider_date_time"
            if item.dataset == "dealer_exposure"
            else "endpoint_scope_or_pagination_unverified"
        ),
        "as_of_source": as_of_source,
        "historical_scope_verified": historical_scope_verified,
        "pagination_status": "verified_not_required" if definition.pagination_verified else "not_traversed; coverage incomplete until pagination audit",
        "derived_gex_eligible": (
            None if item.dataset != "dealer_exposure" else not is_empty and not gex_unusable_levels
        ),
        "derived_gex_exclusion_reason": (
            None
            if item.dataset != "dealer_exposure" or (not is_empty and not gex_unusable_levels)
            else "all_gex_levels_null"
            if is_empty
            else "partial_gex_level_set: " + ", ".join(gex_unusable_levels)
        ),
        "gex_unusable_level_names": list(gex_unusable_levels) if item.dataset == "dealer_exposure" else None,
        "backfill_plan_id": item.plan_id,
        "backfill_item_key": item.item_key,
        "raw_only": True,
    }


def _as_of(item: BackfillItem, fetched_at: datetime) -> datetime:
    if item.requested_date is None:
        return fetched_at
    return datetime.combine(item.requested_date, time.min, tzinfo=UTC)


def _oi_metadata(item: BackfillItem, response: EndpointResponse, *, page: int) -> dict[str, Any]:
    metadata = _response_metadata(item, response, historical_scope_verified=False)
    metadata.update({
        "pagination_family": "oi_change",
        "pagination_status": "page_captured; completion_pending",
        "pagination_page": page,
        "pagination_page_size": OI_PAGE_SIZE,
        "pagination_order": "desc",
    })
    return metadata


def _oi_resume_state(
    event_store: BackfillStore,
    snapshots: SnapshotStore,
    item: BackfillItem,
) -> tuple[int | None, set[str], set[str]]:
    """Return the next OI page and immutable evidence seen on prior pages."""

    events = event_store.events(item)
    if not events:
        return 0, set(), set()
    latest_state, _, latest_details = events[-1]
    if latest_state in {CoverageState.COLLECTED, CoverageState.EMPTY}:
        return None, set(), set()
    if latest_state is CoverageState.SCOPE_UNVERIFIED and latest_details.get("pagination_status") != "in_progress":
        return None, set(), set()
    next_page = latest_details.get("next_page", 0)
    if not isinstance(next_page, int) or next_page < 0:
        return None, set(), set()

    symbols: set[str] = set()
    payload_hashes: set[str] = set()
    for _, snapshot_id, details in events:
        if snapshot_id is None or details.get("pagination_family") != "oi_change":
            continue
        snapshot = snapshots.get(int(snapshot_id))
        if snapshot is None:  # pragma: no cover - protected by the event FK
            raise RuntimeError(f"backfill event references missing snapshot {snapshot_id}")
        payload_hashes.add(snapshot.envelope.raw_payload_hash)
        payload = snapshot.envelope.payload
        rows = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            symbols.update(
                row["option_symbol"]
                for row in rows
                if isinstance(row, dict) and isinstance(row.get("option_symbol"), str)
            )
    return next_page, symbols, payload_hashes


def _validate_oi_page(
    rows: object, *, requested_date: date, seen_symbols: set[str]
) -> tuple[str | None, int]:
    """Return a validation failure reason and duplicate count, if any."""

    if not isinstance(rows, list):
        return "response_data_not_list", 0
    page_symbols: set[str] = set()
    duplicates = 0
    requested = requested_date.isoformat()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            return f"row_{index}_not_object", duplicates
        if row.get("curr_date") != requested:
            return f"row_{index}_curr_date_mismatch", duplicates
        option_symbol = row.get("option_symbol")
        if not isinstance(option_symbol, str) or not option_symbol.strip():
            return f"row_{index}_missing_option_symbol", duplicates
        if option_symbol in seen_symbols or option_symbol in page_symbols:
            duplicates += 1
        page_symbols.add(option_symbol)
    return ("duplicate_option_symbol" if duplicates else None), duplicates


@dataclass(frozen=True, slots=True)
class _OiCollectionResult:
    attempted: int
    nonempty: int
    empty: int
    failures: int


def _collect_oi_pages(
    *,
    client: HistoricalClient,
    snapshots: SnapshotStore,
    event_store: BackfillStore,
    item: BackfillItem,
    request_budget: int,
) -> _OiCollectionResult:
    """Collect bounded OI pages, retaining every raw page before validation."""

    if item.requested_date is None:  # pragma: no cover - plan construction defends this
        raise ValueError("open_interest requires a requested date")
    page, seen_symbols, seen_payload_hashes = _oi_resume_state(event_store, snapshots, item)
    if page is None:
        return _OiCollectionResult(0, 0, 0, 0)
    if request_budget <= 0:
        event_store.record(item, CoverageState.BUDGET_EXHAUSTED, details={
            "pagination_family": "oi_change", "next_page": page, "reason": "request_budget_exhausted",
        })
        return _OiCollectionResult(0, 0, 0, 0)

    attempted = nonempty = empty = failures = 0
    while page < OI_MAX_PAGES_PER_ITEM:
        if attempted >= request_budget:
            event_store.record(item, CoverageState.BUDGET_EXHAUSTED, details={
                "pagination_family": "oi_change", "next_page": page, "reason": "request_budget_exhausted",
            })
            return _OiCollectionResult(attempted, nonempty, empty, failures)
        attempted += 1
        try:
            response = client.oi_change(
                item.ticker, as_of=item.requested_date, limit=OI_PAGE_SIZE, page=page, order="desc"
            )
            raw = response.response.raw
            rows = response.data
            snapshot = snapshots.insert(
                SnapshotEnvelope(
                    provider="unusual_whales", dataset=Dataset.OPEN_INTEREST, symbol=item.ticker,
                    as_of=_as_of(item, raw.fetched_at), retrieved_at=raw.fetched_at,
                    payload=response.response.payload, metadata=_oi_metadata(item, response, page=page),
                )
            )
            if not isinstance(rows, list):
                event_store.record(item, CoverageState.SCOPE_UNVERIFIED, snapshot_id=snapshot.id, details={
                    "pagination_family": "oi_change", "page": page, "reason": "response_data_not_list",
                })
                return _OiCollectionResult(attempted, nonempty, empty, failures)
            if not rows:
                state = CoverageState.EMPTY if page == 0 else CoverageState.COLLECTED
                event_store.record(item, state, snapshot_id=snapshot.id, details={
                    "pagination_family": "oi_change", "page": page, "row_count": 0,
                    "pages_captured": page + 1, "stop_reason": "empty_page",
                })
                return _OiCollectionResult(attempted, nonempty, empty + (1 if page == 0 else 0), failures)
            reason, duplicates = _validate_oi_page(
                rows, requested_date=item.requested_date, seen_symbols=seen_symbols
            )
            if snapshot.envelope.raw_payload_hash in seen_payload_hashes:
                reason = "repeated_raw_page_payload"
            if reason is not None:
                event_store.record(item, CoverageState.SCOPE_UNVERIFIED, snapshot_id=snapshot.id, details={
                    "pagination_family": "oi_change", "page": page, "row_count": len(rows),
                    "duplicate_count": duplicates, "reason": reason,
                })
                return _OiCollectionResult(attempted, nonempty + 1, empty, failures)
            page_symbols = {str(row["option_symbol"]) for row in rows}
            seen_symbols.update(page_symbols)
            seen_payload_hashes.add(snapshot.envelope.raw_payload_hash)
            nonempty += 1
            if len(rows) < OI_PAGE_SIZE:
                event_store.record(item, CoverageState.COLLECTED, snapshot_id=snapshot.id, details={
                    "pagination_family": "oi_change", "page": page, "row_count": len(rows),
                    "unique_option_symbols": len(seen_symbols), "pages_captured": page + 1,
                    "stop_reason": "short_page",
                })
                return _OiCollectionResult(attempted, nonempty, empty, failures)
            event_store.record(item, CoverageState.SCOPE_UNVERIFIED, snapshot_id=snapshot.id, details={
                "pagination_family": "oi_change", "pagination_status": "in_progress",
                "page": page, "next_page": page + 1, "row_count": len(rows),
                "unique_option_symbols": len(seen_symbols),
            })
            page += 1
        except Exception as error:  # preserve earlier pages and retry the same cursor
            failures += 1
            event_store.record(item, CoverageState.FAILED, details={
                "pagination_family": "oi_change", "next_page": page,
                "error_type": type(error).__name__, "message": str(error)[:160],
            })
            return _OiCollectionResult(attempted, nonempty, empty, failures)
    event_store.record(item, CoverageState.SCOPE_UNVERIFIED, details={
        "pagination_family": "oi_change", "next_page": page,
        "reason": "max_page_guard_exceeded", "max_pages": OI_MAX_PAGES_PER_ITEM,
    })
    return _OiCollectionResult(attempted, nonempty, empty, failures)


def _parse_provider_timestamp(value: object) -> datetime:
    if isinstance(value, bool):
        raise ValueError("timestamp must not be boolean")
    if isinstance(value, (int, float)):
        magnitude = abs(float(value))
        seconds = float(value) / 1_000 if magnitude >= 100_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=UTC)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty ISO-8601 string or Unix value")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _cursor_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _flow_alert_bounds(requested_date: date) -> tuple[str, str]:
    start = datetime.combine(requested_date, time.min, tzinfo=NEW_YORK).astimezone(UTC)
    return _cursor_text(start), _cursor_text(start + timedelta(days=1))


def _flow_alert_resume_state(
    event_store: BackfillStore,
    snapshots: SnapshotStore,
    item: BackfillItem,
) -> tuple[str | None, int | None, set[str], set[str]]:
    events = event_store.events(item)
    if not events:
        return None, 0, set(), set()
    latest_state, _, latest_details = events[-1]
    if latest_state in {CoverageState.COLLECTED, CoverageState.EMPTY}:
        return None, None, set(), set()
    if latest_state is CoverageState.SCOPE_UNVERIFIED and latest_details.get("pagination_status") != "in_progress":
        return None, None, set(), set()
    cursor = latest_details.get("next_cursor")
    page = latest_details.get("next_page", 0)
    if cursor is not None and not isinstance(cursor, str):
        return None, None, set(), set()
    if not isinstance(page, int) or page < 0:
        return None, None, set(), set()
    identities: set[str] = set()
    payload_hashes: set[str] = set()
    for _, snapshot_id, details in events:
        if snapshot_id is None or details.get("pagination_family") != "flow_alert_cursor":
            continue
        snapshot = snapshots.get(int(snapshot_id))
        if snapshot is None:  # pragma: no cover - protected by the event FK
            raise RuntimeError(f"backfill event references missing snapshot {snapshot_id}")
        payload_hashes.add(snapshot.envelope.raw_payload_hash)
        payload = snapshot.envelope.payload
        rows = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            identities.update(payload_digest(row) for row in rows if isinstance(row, Mapping))
    return cursor, page, identities, payload_hashes


def _flow_alert_metadata(
    item: BackfillItem, response: EndpointResponse, *, page: int,
    newer_than: str, older_than: str,
) -> dict[str, Any]:
    metadata = _response_metadata(item, response, historical_scope_verified=True)
    metadata.update({
        "pagination_family": "flow_alert_cursor",
        "pagination_status": "page_captured; completion_pending",
        "pagination_page": page,
        "pagination_page_size": FLOW_ALERT_PAGE_SIZE,
        "pagination_newer_than": newer_than,
        "pagination_older_than": older_than,
        "provider_unusual_preset": False,
    })
    return metadata


def _collect_flow_alert_pages(
    *,
    client: HistoricalClient,
    snapshots: SnapshotStore,
    event_store: BackfillStore,
    item: BackfillItem,
    request_budget: int,
) -> _OiCollectionResult:
    if item.requested_date is None:  # pragma: no cover - plan construction defends this
        raise ValueError("flow_alerts requires a requested date")
    cursor, page, seen_ids, seen_payload_hashes = _flow_alert_resume_state(event_store, snapshots, item)
    if page is None:
        return _OiCollectionResult(0, 0, 0, 0)
    newer_than, session_end = _flow_alert_bounds(item.requested_date)
    attempted = nonempty = empty = failures = 0
    while page < FLOW_ALERT_MAX_PAGES_PER_ITEM:
        if attempted >= request_budget:
            event_store.record(item, CoverageState.BUDGET_EXHAUSTED, details={
                "pagination_family": "flow_alert_cursor", "next_cursor": cursor,
                "next_page": page, "reason": "request_budget_exhausted",
            })
            return _OiCollectionResult(attempted, nonempty, empty, failures)
        attempted += 1
        older_than = cursor or session_end
        try:
            response = client.flow_alerts(
                item.ticker, unusual=False, newer_than=newer_than,
                older_than=older_than, limit=FLOW_ALERT_PAGE_SIZE,
            )
            raw = response.response.raw
            rows = response.data
            snapshot = snapshots.insert(SnapshotEnvelope(
                provider="unusual_whales", dataset=Dataset.OPTION_FLOW, symbol=item.ticker,
                as_of=_as_of(item, raw.fetched_at), retrieved_at=raw.fetched_at,
                payload=response.response.payload,
                metadata=_flow_alert_metadata(
                    item, response, page=page, newer_than=newer_than, older_than=older_than,
                ),
            ))
            if not isinstance(rows, list):
                event_store.record(item, CoverageState.SCOPE_UNVERIFIED, snapshot_id=snapshot.id, details={
                    "pagination_family": "flow_alert_cursor", "page": page,
                    "reason": "response_data_not_list",
                })
                return _OiCollectionResult(attempted, nonempty, empty, failures)
            if not rows:
                state = CoverageState.EMPTY if page == 0 else CoverageState.COLLECTED
                event_store.record(item, state, snapshot_id=snapshot.id, details={
                    "pagination_family": "flow_alert_cursor", "page": page,
                    "row_count": 0, "pages_captured": page + 1, "stop_reason": "empty_page",
                })
                return _OiCollectionResult(attempted, nonempty, empty + (1 if page == 0 else 0), failures)
            timestamps: list[datetime] = []
            requested_rows: list[Mapping[str, Any]] = []
            crossed_older_boundary = False
            for index, row in enumerate(rows):
                if not isinstance(row, Mapping):
                    raise ValueError(f"flow_alerts_row_{index}_is_not_an_object")
                observed = _parse_provider_timestamp(row.get("created_at"))
                timestamps.append(observed)
                observed_date = observed.astimezone(NEW_YORK).date()
                if observed_date > item.requested_date:
                    raise ValueError(f"flow_alerts_row_{index}_after_requested_New_York_date")
                if observed_date < item.requested_date:
                    crossed_older_boundary = True
                    continue
                if crossed_older_boundary:
                    raise ValueError("requested_date_after_older_boundary")
                requested_rows.append(row)
            if any(right > left for left, right in zip(timestamps, timestamps[1:])):
                raise ValueError("flow_alert_created_at_not_descending")
            row_ids = {payload_digest(row) for row in requested_rows}
            if snapshot.envelope.raw_payload_hash in seen_payload_hashes:
                raise ValueError("repeated_raw_page_payload")
            if crossed_older_boundary:
                seen_ids.update(row_ids - seen_ids)
                state = CoverageState.EMPTY if not requested_rows and page == 0 else CoverageState.COLLECTED
                event_store.record(item, state, snapshot_id=snapshot.id, details={
                    "pagination_family": "flow_alert_cursor", "page": page,
                    "row_count": len(rows), "requested_row_count": len(requested_rows),
                    "ignored_older_row_count": len(rows) - len(requested_rows),
                    "unique_alerts": len(seen_ids), "pages_captured": page + 1,
                    "stop_reason": "date_boundary_crossed",
                })
                return _OiCollectionResult(
                    attempted, nonempty + (1 if requested_rows else 0),
                    empty + (1 if state is CoverageState.EMPTY else 0), failures,
                )
            new_ids = row_ids - seen_ids
            if not new_ids:
                raise ValueError("flow_alert_cursor_made_no_progress")
            seen_ids.update(new_ids)
            seen_payload_hashes.add(snapshot.envelope.raw_payload_hash)
            nonempty += 1
            if len(rows) < FLOW_ALERT_PAGE_SIZE:
                event_store.record(item, CoverageState.COLLECTED, snapshot_id=snapshot.id, details={
                    "pagination_family": "flow_alert_cursor", "page": page,
                    "row_count": len(rows), "unique_alerts": len(seen_ids),
                    "pages_captured": page + 1, "stop_reason": "short_page",
                })
                return _OiCollectionResult(attempted, nonempty, empty, failures)
            oldest = min(timestamps)
            next_cursor = _cursor_text(oldest + timedelta(microseconds=1))
            if cursor is not None and _parse_provider_timestamp(next_cursor) >= _parse_provider_timestamp(cursor):
                raise ValueError("flow_alert_cursor_made_no_progress")
            event_store.record(item, CoverageState.SCOPE_UNVERIFIED, snapshot_id=snapshot.id, details={
                "pagination_family": "flow_alert_cursor", "pagination_status": "in_progress",
                "page": page, "next_page": page + 1, "next_cursor": next_cursor,
                "row_count": len(rows), "unique_alerts": len(seen_ids),
            })
            cursor, page = next_cursor, page + 1
        except ValueError as error:
            event_store.record(item, CoverageState.SCOPE_UNVERIFIED, details={
                "pagination_family": "flow_alert_cursor", "next_cursor": cursor,
                "next_page": page, "reason": str(error)[:160],
            })
            return _OiCollectionResult(attempted, nonempty, empty, failures)
        except Exception as error:
            failures += 1
            event_store.record(item, CoverageState.FAILED, details={
                "pagination_family": "flow_alert_cursor", "next_cursor": cursor,
                "next_page": page, "error_type": type(error).__name__,
                "message": str(error)[:160],
            })
            return _OiCollectionResult(attempted, nonempty, empty, failures)
    event_store.record(item, CoverageState.SCOPE_UNVERIFIED, details={
        "pagination_family": "flow_alert_cursor", "next_cursor": cursor,
        "next_page": page, "reason": "max_page_guard_exceeded",
    })
    return _OiCollectionResult(attempted, nonempty, empty, failures)


def _dark_pool_metadata(
    item: BackfillItem, response: EndpointResponse, *, page: int, cursor_before: str | None
) -> dict[str, Any]:
    metadata = _response_metadata(item, response, historical_scope_verified=False)
    metadata.update({
        "pagination_family": "dark_pool_cursor",
        "pagination_status": "page_captured; completion_pending",
        "pagination_page": page,
        "pagination_page_size": DARK_POOL_PAGE_SIZE,
        "pagination_order": "desc",
        "pagination_order_by": "executed_at",
        "pagination_cursor_before": cursor_before,
    })
    return metadata


def _dark_pool_resume_state(
    event_store: BackfillStore,
    snapshots: SnapshotStore,
    item: BackfillItem,
) -> tuple[str | None, int | None, set[str], set[str]]:
    events = event_store.events(item)
    if not events:
        return None, 0, set(), set()
    latest_state, _, latest_details = events[-1]
    if latest_state in {CoverageState.COLLECTED, CoverageState.EMPTY}:
        return None, None, set(), set()
    if latest_state is CoverageState.SCOPE_UNVERIFIED and latest_details.get("pagination_status") != "in_progress":
        return None, None, set(), set()
    cursor = latest_details.get("next_cursor")
    page = latest_details.get("next_page", 0)
    if cursor is not None and not isinstance(cursor, str):
        return None, None, set(), set()
    if not isinstance(page, int) or page < 0:
        return None, None, set(), set()
    identities: set[str] = set()
    payload_hashes: set[str] = set()
    for _, snapshot_id, details in events:
        if snapshot_id is None or details.get("pagination_family") != "dark_pool_cursor":
            continue
        snapshot = snapshots.get(int(snapshot_id))
        if snapshot is None:  # pragma: no cover - protected by the event FK
            raise RuntimeError(f"backfill event references missing snapshot {snapshot_id}")
        payload_hashes.add(snapshot.envelope.raw_payload_hash)
        payload = snapshot.envelope.payload
        rows = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            identities.update(
                str(row["tracking_id"])
                for row in rows
                if isinstance(row, dict) and row.get("tracking_id") is not None
            )
    return cursor, page, identities, payload_hashes


def _validate_dark_pool_page(
    rows: object, *, requested_date: date
) -> tuple[str | None, list[tuple[str, datetime]], list[tuple[str, datetime]], bool]:
    """Validate a non-increasing requested-date page and its terminal boundary.

    The provider cursor can return a final page containing the requested New
    York market date followed by older prints. Those older prints are retained
    as raw evidence, but are outside this item's coverage and therefore never
    contribute tracking IDs or counts.
    """

    if not isinstance(rows, list):
        return "response_data_not_list", [], [], False
    validated: list[tuple[str, datetime]] = []
    requested_rows: list[tuple[str, datetime]] = []
    crossed_older_boundary = False
    prior_timestamp: datetime | None = None
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            return f"row_{index}_not_object", [], [], False
        tracking_id = row.get("tracking_id")
        if tracking_id is None or isinstance(tracking_id, bool) or (
            isinstance(tracking_id, str) and not tracking_id.strip()
        ):
            return f"row_{index}_missing_tracking_id", [], [], False
        try:
            executed_at = _parse_provider_timestamp(row.get("executed_at"))
        except (OverflowError, OSError, TypeError, ValueError):
            return f"row_{index}_invalid_executed_at", [], [], False
        # Multiple prints can legitimately share the provider's one-second
        # timestamp. Reject only an actual increase; tracking_id handles ties.
        if prior_timestamp is not None and executed_at > prior_timestamp:
            return "timestamps_not_descending", [], [], False
        prior_timestamp = executed_at
        observed_date = executed_at.astimezone(NEW_YORK).date()
        if observed_date > requested_date:
            return f"row_{index}_future_market_date", [], [], False
        identity = str(tracking_id)
        validated.append((identity, executed_at))
        if observed_date < requested_date:
            crossed_older_boundary = True
            continue
        if crossed_older_boundary:
            return "requested_date_after_older_boundary", [], [], False
        requested_rows.append((identity, executed_at))
    return None, validated, requested_rows, crossed_older_boundary


def _collect_dark_pool_pages(
    *,
    client: HistoricalClient,
    snapshots: SnapshotStore,
    event_store: BackfillStore,
    item: BackfillItem,
    request_budget: int,
) -> _OiCollectionResult:
    """Collect historical dark-pool prints using the provider's time cursor."""

    if item.requested_date is None:  # pragma: no cover - plan construction defends this
        raise ValueError("dark_pool requires a requested date")
    cursor, page, seen_ids, seen_payload_hashes = _dark_pool_resume_state(event_store, snapshots, item)
    if page is None:
        return _OiCollectionResult(0, 0, 0, 0)
    if request_budget <= 0:
        event_store.record(item, CoverageState.BUDGET_EXHAUSTED, details={
            "pagination_family": "dark_pool_cursor", "next_cursor": cursor, "next_page": page,
            "reason": "request_budget_exhausted",
        })
        return _OiCollectionResult(0, 0, 0, 0)

    attempted = nonempty = empty = failures = 0
    prior_cursor = _parse_provider_timestamp(cursor) if cursor is not None else None
    while page < DARK_POOL_MAX_PAGES_PER_ITEM:
        if attempted >= request_budget:
            event_store.record(item, CoverageState.BUDGET_EXHAUSTED, details={
                "pagination_family": "dark_pool_cursor", "next_cursor": cursor, "next_page": page,
                "reason": "request_budget_exhausted",
            })
            return _OiCollectionResult(attempted, nonempty, empty, failures)
        attempted += 1
        try:
            response = client.darkpool_trades(
                item.ticker, as_of=item.requested_date, older_than=cursor, limit=DARK_POOL_PAGE_SIZE,
                order="desc", order_by="executed_at",
            )
            raw = response.response.raw
            rows = response.data
            snapshot = snapshots.insert(
                SnapshotEnvelope(
                    provider="unusual_whales", dataset=Dataset.DARK_POOL, symbol=item.ticker,
                    as_of=_as_of(item, raw.fetched_at), retrieved_at=raw.fetched_at,
                    payload=response.response.payload,
                    metadata=_dark_pool_metadata(item, response, page=page, cursor_before=cursor),
                )
            )
            if not isinstance(rows, list):
                event_store.record(item, CoverageState.SCOPE_UNVERIFIED, snapshot_id=snapshot.id, details={
                    "pagination_family": "dark_pool_cursor", "page": page, "reason": "response_data_not_list",
                })
                return _OiCollectionResult(attempted, nonempty, empty, failures)
            if not rows:
                state = CoverageState.EMPTY if page == 0 else CoverageState.COLLECTED
                event_store.record(item, state, snapshot_id=snapshot.id, details={
                    "pagination_family": "dark_pool_cursor", "page": page, "row_count": 0,
                    "pages_captured": page + 1, "stop_reason": "empty_page",
                })
                return _OiCollectionResult(attempted, nonempty, empty + (1 if page == 0 else 0), failures)
            reason, validated, requested_rows, crossed_date_boundary = _validate_dark_pool_page(
                rows, requested_date=item.requested_date
            )
            if snapshot.envelope.raw_payload_hash in seen_payload_hashes:
                reason = "repeated_raw_page_payload"
            if reason is not None:
                event_store.record(item, CoverageState.SCOPE_UNVERIFIED, snapshot_id=snapshot.id, details={
                    "pagination_family": "dark_pool_cursor", "page": page, "row_count": len(rows), "reason": reason,
                })
                return _OiCollectionResult(attempted, nonempty + 1, empty, failures)
            oldest = min(executed_at for _, executed_at in validated)
            if prior_cursor is not None and oldest >= prior_cursor:
                event_store.record(item, CoverageState.SCOPE_UNVERIFIED, snapshot_id=snapshot.id, details={
                    "pagination_family": "dark_pool_cursor", "page": page, "row_count": len(rows),
                    "reason": "cursor_made_no_progress", "cursor_before": cursor,
                })
                return _OiCollectionResult(attempted, nonempty + 1, empty, failures)
            requested_ids = {tracking_id for tracking_id, _ in requested_rows}
            new_ids = requested_ids - seen_ids
            if crossed_date_boundary:
                # A strict transition into older NY dates closes this requested
                # session. The raw older rows remain in the page snapshot, but
                # cannot affect requested-session coverage or dedupe counts.
                if not requested_rows and page == 0 and len(rows) == DARK_POOL_PAGE_SIZE:
                    event_store.record(item, CoverageState.SCOPE_UNVERIFIED, snapshot_id=snapshot.id, details={
                        "pagination_family": "dark_pool_cursor", "page": page, "row_count": len(rows),
                        "reason": "initial_page_has_no_requested_rows_without_short_boundary",
                    })
                    return _OiCollectionResult(attempted, nonempty + 1, empty, failures)
                seen_ids.update(new_ids)
                state = CoverageState.EMPTY if not requested_rows and page == 0 else CoverageState.COLLECTED
                event_store.record(item, state, snapshot_id=snapshot.id, details={
                    "pagination_family": "dark_pool_cursor", "page": page, "row_count": len(rows),
                    "requested_row_count": len(requested_rows), "ignored_older_row_count": len(rows) - len(requested_rows),
                    "unique_tracking_ids": len(seen_ids), "pages_captured": page + 1,
                    "stop_reason": "date_boundary_crossed",
                })
                return _OiCollectionResult(
                    attempted, nonempty + (1 if requested_rows else 0), empty + (1 if state is CoverageState.EMPTY else 0), failures
                )
            if not new_ids:
                event_store.record(item, CoverageState.SCOPE_UNVERIFIED, snapshot_id=snapshot.id, details={
                    "pagination_family": "dark_pool_cursor", "page": page, "row_count": len(rows),
                    "reason": "no_new_tracking_ids",
                })
                return _OiCollectionResult(attempted, nonempty + 1, empty, failures)
            seen_ids.update(new_ids)
            seen_payload_hashes.add(snapshot.envelope.raw_payload_hash)
            nonempty += 1
            # The provider does not document whether ``older_than`` is
            # inclusive. Move the bound one microsecond later so records tied
            # at the oldest timestamp are deliberately overlapped, then
            # de-duplicate them by tracking_id.
            next_cursor = _cursor_text(oldest + timedelta(microseconds=1))
            if len(rows) < DARK_POOL_PAGE_SIZE:
                event_store.record(item, CoverageState.COLLECTED, snapshot_id=snapshot.id, details={
                    "pagination_family": "dark_pool_cursor", "page": page, "row_count": len(rows),
                    "unique_tracking_ids": len(seen_ids), "pages_captured": page + 1,
                    "stop_reason": "short_page",
                })
                return _OiCollectionResult(attempted, nonempty, empty, failures)
            event_store.record(item, CoverageState.SCOPE_UNVERIFIED, snapshot_id=snapshot.id, details={
                "pagination_family": "dark_pool_cursor", "pagination_status": "in_progress",
                "page": page, "next_page": page + 1, "next_cursor": next_cursor,
                "row_count": len(rows), "unique_tracking_ids": len(seen_ids),
            })
            cursor, prior_cursor, page = next_cursor, oldest, page + 1
        except Exception as error:
            failures += 1
            event_store.record(item, CoverageState.FAILED, details={
                "pagination_family": "dark_pool_cursor", "next_cursor": cursor, "next_page": page,
                "error_type": type(error).__name__, "message": str(error)[:160],
            })
            return _OiCollectionResult(attempted, nonempty, empty, failures)
    event_store.record(item, CoverageState.SCOPE_UNVERIFIED, details={
        "pagination_family": "dark_pool_cursor", "next_cursor": cursor, "next_page": page,
        "reason": "max_page_guard_exceeded", "max_pages": DARK_POOL_MAX_PAGES_PER_ITEM,
    })
    return _OiCollectionResult(attempted, nonempty, empty, failures)


def collect(
    *,
    client: HistoricalClient,
    snapshots: SnapshotStore,
    plan: BackfillPlan,
    max_requests: int,
    audit_accepted: bool,
) -> dict[str, Any]:
    """Collect a bounded number of raw responses and leave a resumable trail.

    ``audit_accepted`` is deliberately explicit. It means an operator reviewed
    the endpoint audit for this subscription; it does not make the payload
    schema trusted or enable recommendations.
    """
    if not audit_accepted:
        raise ValueError("live backfill requires explicit audit acceptance")
    if not 1 <= max_requests <= 2_000:
        raise ValueError("max_requests must be between 1 and 2000")
    event_store = BackfillStore(snapshots)
    event_store.register(plan)
    states = event_store.latest_states(plan.plan_id)
    attempted = collected = empty = failures = 0
    for item in plan.items():
        if item.dataset == "flow_alerts":
            flow_result = _collect_flow_alert_pages(
                client=client,
                snapshots=snapshots,
                event_store=event_store,
                item=item,
                request_budget=max_requests - attempted,
            )
            attempted += flow_result.attempted
            collected += flow_result.nonempty
            empty += flow_result.empty
            failures += flow_result.failures
            if attempted >= max_requests:
                break
            continue
        if item.dataset == "dark_pool":
            dark_pool_result = _collect_dark_pool_pages(
                client=client,
                snapshots=snapshots,
                event_store=event_store,
                item=item,
                request_budget=max_requests - attempted,
            )
            attempted += dark_pool_result.attempted
            collected += dark_pool_result.nonempty
            empty += dark_pool_result.empty
            failures += dark_pool_result.failures
            if attempted >= max_requests:
                break
            continue
        if item.dataset == "open_interest":
            oi_result = _collect_oi_pages(
                client=client,
                snapshots=snapshots,
                event_store=event_store,
                item=item,
                request_budget=max_requests - attempted,
            )
            attempted += oi_result.attempted
            collected += oi_result.nonempty
            empty += oi_result.empty
            failures += oi_result.failures
            if attempted >= max_requests:
                break
            continue
        if attempted >= max_requests:
            break
        state = states.get(item.item_key, CoverageState.PLANNED)
        if state in {CoverageState.COLLECTED, CoverageState.EMPTY, CoverageState.SCOPE_UNVERIFIED}:
            continue
        attempted += 1
        try:
            response = _call(client, item)
            raw = response.response.raw
            rows = response.data
            scope_verified, scope_reason = _historical_scope_check(item, rows)
            gex_unusable_levels = (
                gex_unusable_level_names(rows) if item.dataset == "dealer_exposure" else ()
            )
            is_empty = (
                isinstance(rows, (list, dict))
                and len(rows) == 0
            ) or (
                item.dataset == "dealer_exposure" and gex_levels_are_empty(rows)
            )
            if gex_unusable_levels and not is_empty:
                scope_verified = False
                scope_reason = "partial_gex_level_set: " + ", ".join(gex_unusable_levels)
            snapshot = snapshots.insert(
                SnapshotEnvelope(
                    provider="unusual_whales",
                    dataset=BACKFILL_DATASETS[item.dataset].snapshot_dataset,
                    symbol=item.ticker,
                    as_of=_as_of(item, raw.fetched_at),
                    retrieved_at=raw.fetched_at,
                    payload=response.response.payload,
                    metadata=_response_metadata(
                        item,
                        response,
                        historical_scope_verified=scope_verified,
                        is_empty=is_empty,
                        gex_unusable_levels=gex_unusable_levels,
                    ),
                )
            )
            definition = BACKFILL_DATASETS[item.dataset]
            outcome = CoverageState.EMPTY if is_empty else CoverageState.SCOPE_UNVERIFIED if not scope_verified else (
                CoverageState.COLLECTED if definition.pagination_verified else CoverageState.SCOPE_UNVERIFIED
            )
            event_store.record(
                item, outcome, snapshot_id=snapshot.id,
                details={
                    "row_shape": type(rows).__name__,
                    "pagination_verified": definition.pagination_verified,
                    "empty_reason": "all_gex_levels_null" if item.dataset == "dealer_exposure" and is_empty else None,
                    "gex_unusable_level_names": list(gex_unusable_levels) if item.dataset == "dealer_exposure" else None,
                    "scope_validation_error": scope_reason,
                },
            )
            if is_empty:
                empty += 1
            else:
                collected += 1
        except Exception as error:  # preserve the plan and let later runs resume
            failures += 1
            event_store.record(item, CoverageState.FAILED, details={"error_type": type(error).__name__, "message": str(error)[:160]})
    coverage = event_store.coverage(plan)
    coverage.update({
        # A resumed, fully terminal plan registers/reads local evidence only.
        # Do not report network activity merely because this is the live-capable
        # collection path.
        "network_called": attempted > 0,
        "attempted_logical_items": attempted,
        "captured_nonempty_responses": collected,
        "empty_responses": empty,
        "failed_requests": failures,
        "logical_item_cap": max_requests,
    })
    return coverage


def plan_preview(plan: BackfillPlan, *, logical_item_cap: int) -> dict[str, Any]:
    if logical_item_cap < 1:
        raise ValueError("logical_item_cap must be positive")
    items = plan.items()
    return {
        "status": "dry_run",
        "plan_id": plan.plan_id,
        "definition": plan.definition(),
        "planned_items": len(items),
        "logical_item_cap": logical_item_cap,
        "maximum_transport_attempts": logical_item_cap * 3,
        "will_defer": max(0, len(items) - logical_item_cap),
        "sample_items": [item.public_dict() for item in items[:10]],
        "network_called": False,
        "recommendations_enabled": False,
        "notice": "No exchange-holiday calendar or field mapping is assumed. Historical responses stay raw until audit verification.",
    }
