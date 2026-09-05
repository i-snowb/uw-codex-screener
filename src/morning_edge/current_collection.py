"""Bounded read-only capture of current Codex Screener evidence.

This module is deliberately an evidence collector.  It stores each provider
response unchanged in :class:`~morning_edge.store.SnapshotStore` and labels the
time semantics needed by a later normalizer.  It does not derive features,
interpret flow, score a ticker, or enable recommendations.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
import json
from pathlib import Path
import sqlite3
from typing import Any, Protocol

from .models import Dataset, SnapshotEnvelope, timestamp_text
from .providers.base import CollectionCircuit, ProviderError
from .providers.budget import WeeklyRequestBudget
from .providers.unusual_whales import (
    EndpointResponse,
    UnusualWhalesClient,
    gex_levels_are_empty,
    gex_unusable_level_names,
)
from .store import SnapshotStore


class CurrentDataset(StrEnum):
    """Supported current-response families and their stable public names."""

    OPTION_CHAIN = "option_chain"
    OPEN_INTEREST = "open_interest"
    DEALER_EXPOSURE = "dealer_exposure"
    OHLC = "ohlc"
    EARNINGS = "earnings"
    FLOW_ALERTS = "flow_alerts"
    DARK_POOL = "dark_pool"
    NEWS = "news"


DEFAULT_CURRENT_DATASETS = tuple(CurrentDataset)


@dataclass(frozen=True, slots=True)
class _DatasetSpec:
    snapshot_dataset: Dataset
    endpoint: str
    as_of_source: str
    timestamp_semantics: str
    collect: Callable[["CurrentClient", str], EndpointResponse]


class CurrentClient(Protocol):
    """The read-only subset of :class:`UnusualWhalesClient` used here."""

    def option_chain(self, ticker: str, *, as_of: object = None, greeks: bool = True) -> EndpointResponse: ...
    def oi_change(
        self, ticker: str, *, as_of: object = None, limit: int = 500,
        page: int | None = None, order: str = "desc",
    ) -> EndpointResponse: ...
    def gex_levels(self, ticker: str, *, as_of: object = None) -> EndpointResponse: ...
    def ohlc(
        self, ticker: str, *, candle_size: str = "1d", timeframe: str = "1Y",
        end_date: object = None, limit: int | None = None,
    ) -> EndpointResponse: ...
    def earnings_history(self, ticker: str) -> EndpointResponse: ...
    def flow_alerts(
        self, ticker: str, *, unusual: bool = False, min_premium: float | None = None,
        max_dte: int | None = None, limit: int = 100, page: int | None = None,
    ) -> EndpointResponse: ...
    def darkpool_trades(
        self, ticker: str, *, as_of: object = None, newer_than: object = None,
        older_than: object = None, min_premium: float | None = None, limit: int = 500,
        order: str = "desc", order_by: str = "executed_at",
    ) -> EndpointResponse: ...
    def news_headlines(
        self, ticker: str, *, major_only: bool = False, limit: int = 100,
        page: int | None = None,
    ) -> EndpointResponse: ...


def _chain(client: CurrentClient, ticker: str) -> EndpointResponse:
    return client.option_chain(ticker, greeks=True)


def _open_interest(client: CurrentClient, ticker: str) -> EndpointResponse:
    return client.oi_change(ticker, limit=500, page=None, order="desc")


def _gex(client: CurrentClient, ticker: str) -> EndpointResponse:
    return client.gex_levels(ticker)


def _ohlc(client: CurrentClient, ticker: str) -> EndpointResponse:
    return client.ohlc(ticker, candle_size="1d", timeframe="1Y", limit=365)


def _earnings(client: CurrentClient, ticker: str) -> EndpointResponse:
    return client.earnings_history(ticker)


def _flow(client: CurrentClient, ticker: str) -> EndpointResponse:
    return client.flow_alerts(ticker, unusual=False, limit=100)


def _dark_pool(client: CurrentClient, ticker: str) -> EndpointResponse:
    return client.darkpool_trades(ticker, limit=500, order="desc", order_by="executed_at")


def _news(client: CurrentClient, ticker: str) -> EndpointResponse:
    return client.news_headlines(ticker, major_only=False, limit=100)


SPECS: Mapping[CurrentDataset, _DatasetSpec] = {
    CurrentDataset.OPTION_CHAIN: _DatasetSpec(
        Dataset.OPTION_CHAIN,
        "/api/stock/{ticker}/option-chains",
        "retrieval_time_only; no response-wide provider as_of for current chain",
        "Current endpoint request. Contract last_tape_time is row-level state, not an executable quote timestamp; normalize and validate each row before option analysis.",
        _chain,
    ),
    CurrentDataset.OPEN_INTEREST: _DatasetSpec(
        Dataset.OPEN_INTEREST,
        "/api/stock/{ticker}/oi-change",
        "retrieval_time_only; individual rows retain curr_date and last_date source dates",
        "Current bounded OI-change page. Its curr_date/last_date are source-session labels; pagination is not established by this one-page capture, so it is a cross-check until consecutive full chains derive canonical deltas.",
        _open_interest,
    ),
    CurrentDataset.DEALER_EXPOSURE: _DatasetSpec(
        Dataset.DEALER_EXPOSURE,
        "/api/stock/{ticker}/gex-levels",
        "provider time when valid, otherwise retrieval time for explicit empty GEX",
        "Current provider GEX level set. Its date/time identifies a modeled positioning observation, not a guarantee of support, resistance, or direction.",
        _gex,
    ),
    CurrentDataset.OHLC: _DatasetSpec(
        Dataset.OHLC,
        "/api/stock/{ticker}/ohlc/1d",
        "retrieval_time_only; individual bars retain provider session-date/end-time fields",
        "One-year daily-bar response retrieved now. Before the regular session it is prior-session reference data, not a current executable equity quote.",
        _ohlc,
    ),
    CurrentDataset.EARNINGS: _DatasetSpec(
        Dataset.EARNINGS,
        "/api/earnings/{ticker}",
        "retrieval_time_only; individual records retain report-date/report-time fields",
        "Historical earnings calendar retrieved now. It is event context, not a current market-price observation.",
        _earnings,
    ),
    CurrentDataset.FLOW_ALERTS: _DatasetSpec(
        Dataset.OPTION_FLOW,
        "/api/option-trades/flow-alerts",
        "retrieval_time_only; individual alerts retain created_at fields",
        "Current provider alert feed. An alert is lead evidence only; direction/opening status needs row-level trade context and later OI confirmation.",
        _flow,
    ),
    CurrentDataset.DARK_POOL: _DatasetSpec(
        Dataset.DARK_POOL,
        "/api/darkpool/{ticker}",
        "retrieval_time_only; individual prints retain executed_at/trf_executed_at fields",
        "Current bounded dark-pool page. It can be incomplete without historical cursor traversal and does not identify a beneficial owner or thesis.",
        _dark_pool,
    ),
    CurrentDataset.NEWS: _DatasetSpec(
        Dataset.NEWS,
        "/api/news/headlines",
        "retrieval_time_only; individual headlines retain created_at fields",
        "Current bounded headline page. Headlines are catalyst leads and require source review before they support a factual claim.",
        _news,
    ),
}

SNAPSHOT_DATASET_TO_CURRENT: Mapping[str, CurrentDataset] = {
    spec.snapshot_dataset.value: dataset for dataset, spec in SPECS.items()
}


class CurrentCaptureStatus(StrEnum):
    CAPTURED = "captured"
    EMPTY = "empty"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    SCHEMA_MISMATCH = "schema_mismatch"
    BUDGET_BLOCKED = "budget_blocked"


@dataclass(frozen=True, slots=True)
class CurrentCaptureItem:
    ticker: str
    dataset: CurrentDataset
    status: CurrentCaptureStatus
    endpoint: str
    snapshot_id: int | None
    fetched_at: str | None
    row_count: int | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CurrentCaptureReport:
    generated_at: str
    tickers: tuple[str, ...]
    datasets: tuple[CurrentDataset, ...]
    preflight_passed: bool
    max_transport_attempts: int
    remaining_transport_attempt_capacity_before_run: int
    results: tuple[CurrentCaptureItem, ...]

    @property
    def recommendations_enabled(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "tickers": list(self.tickers),
            "datasets": [item.value for item in self.datasets],
            "preflight_passed": self.preflight_passed,
            "max_transport_attempts": self.max_transport_attempts,
            "remaining_transport_attempt_capacity_before_run": self.remaining_transport_attempt_capacity_before_run,
            "recommendations_enabled": False,
            "results": [asdict(item) | {"dataset": item.dataset.value, "status": item.status.value} for item in self.results],
        }


def _clean_tickers(tickers: Iterable[str]) -> tuple[str, ...]:
    clean = tuple(dict.fromkeys(item.strip().upper() for item in tickers if item.strip()))
    if not clean:
        raise ValueError("at least one ticker is required")
    for ticker in clean:
        if not ticker.replace(".", "").replace("-", "").isalnum():
            raise ValueError("ticker must contain letters, digits, '.' or '-'")
    return clean


def _clean_datasets(datasets: Iterable[CurrentDataset | str] | None) -> tuple[CurrentDataset, ...]:
    source = DEFAULT_CURRENT_DATASETS if datasets is None else datasets
    clean = tuple(dict.fromkeys(CurrentDataset(item) for item in source))
    if not clean:
        raise ValueError("at least one current dataset is required")
    return clean


def _rows(response: EndpointResponse) -> list[object]:
    data = response.data
    if isinstance(data, list):
        return list(data)
    if isinstance(data, dict):
        return [data]
    return []


def _provider_gex_time(response: EndpointResponse) -> datetime | None:
    data = response.data
    if not isinstance(data, Mapping) or gex_levels_are_empty(data):
        return None
    value = data.get("time")
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _item_status(dataset: CurrentDataset, response: EndpointResponse) -> tuple[CurrentCaptureStatus, str | None]:
    if dataset is CurrentDataset.DEALER_EXPOSURE:
        data = response.data
        if gex_levels_are_empty(data):
            return CurrentCaptureStatus.EMPTY, "provider returned explicit empty GEX level set"
        missing = gex_unusable_level_names(data)
        if missing:
            return CurrentCaptureStatus.PARTIAL, "partial GEX excluded from derived dealer exposure: " + ", ".join(missing)
    return (CurrentCaptureStatus.EMPTY, "provider returned an explicit empty response") if not _rows(response) else (CurrentCaptureStatus.CAPTURED, None)


def _metadata(dataset: CurrentDataset, response: EndpointResponse, *, status: CurrentCaptureStatus) -> dict[str, Any]:
    spec = SPECS[dataset]
    raw = response.response.raw
    return {
        "capture_mode": "current",
        "raw_only": True,
        "recommendations_enabled": False,
        "current_vs_prior_session": spec.timestamp_semantics,
        "as_of_source": spec.as_of_source,
        "provider_endpoint": response.endpoint,
        "response_status": status.value,
        "response_row_count": len(_rows(response)),
        "transport_attempts": raw.attempts,
        "rate_limit_remaining": raw.rate_limit.remaining,
    }


def collect_current(
    *,
    client: UnusualWhalesClient | CurrentClient,
    snapshots: SnapshotStore,
    request_budget: WeeklyRequestBudget,
    tickers: Sequence[str],
    datasets: Sequence[CurrentDataset | str] | None = None,
    max_transport_attempts_per_item: int = 3,
    generated_at: datetime | None = None,
) -> CurrentCaptureReport:
    """Capture one bounded current response per requested ticker and dataset.

    Preflight uses the same local budget that the real client consumes per HTTP
    attempt.  It reserves enough capacity for the worst-case retry count before
    any provider method is called; a failed preflight produces one explicit
    blocked result per requested item and stores no partial run.
    """

    if (
        isinstance(max_transport_attempts_per_item, bool)
        or not isinstance(max_transport_attempts_per_item, int)
        or max_transport_attempts_per_item < 1
    ):
        raise ValueError("max_transport_attempts_per_item must be a positive integer")
    clean_tickers = _clean_tickers(tickers)
    clean_datasets = _clean_datasets(datasets)
    now = (generated_at or datetime.now(UTC)).astimezone(UTC)
    if generated_at is not None and (generated_at.tzinfo is None or generated_at.utcoffset() is None):
        raise ValueError("generated_at must be timezone-aware")
    max_attempts = len(clean_tickers) * len(clean_datasets) * max_transport_attempts_per_item
    usage = request_budget.usage(now=now)
    if max_attempts > usage.remaining_before_reserve:
        blocked = tuple(
            CurrentCaptureItem(
                ticker=ticker, dataset=dataset, status=CurrentCaptureStatus.BUDGET_BLOCKED,
                endpoint=SPECS[dataset].endpoint.format(ticker=ticker), snapshot_id=None,
                fetched_at=None, row_count=None,
                reason=("preflight blocked: maximum transport attempts " f"{max_attempts} exceeds remaining capacity {usage.remaining_before_reserve}"),
            )
            for ticker in clean_tickers for dataset in clean_datasets
        )
        return CurrentCaptureReport(
            generated_at=now.isoformat(), tickers=clean_tickers, datasets=clean_datasets,
            preflight_passed=False, max_transport_attempts=max_attempts,
            remaining_transport_attempt_capacity_before_run=usage.remaining_before_reserve,
            results=blocked,
        )

    results: list[CurrentCaptureItem] = []
    circuit = CollectionCircuit()
    for ticker in clean_tickers:
        for dataset in clean_datasets:
            spec = SPECS[dataset]
            endpoint = spec.endpoint.format(ticker=ticker)
            if circuit.reason:
                results.append(CurrentCaptureItem(
                    ticker, dataset, CurrentCaptureStatus.UNAVAILABLE, endpoint,
                    None, None, None, circuit.reason,
                ))
                continue
            try:
                response = spec.collect(client, ticker)
                circuit.success()
                status, reason = _item_status(dataset, response)
                raw = response.response.raw
                as_of = _provider_gex_time(response) if dataset is CurrentDataset.DEALER_EXPOSURE else None
                if as_of is not None and as_of > raw.fetched_at:
                    raise ValueError("provider GEX observation time is later than retrieval time")
                stored = snapshots.insert(
                    SnapshotEnvelope(
                        provider="unusual_whales", dataset=spec.snapshot_dataset, symbol=ticker,
                        as_of=as_of or raw.fetched_at, retrieved_at=raw.fetched_at,
                        payload=response.response.payload, metadata=_metadata(dataset, response, status=status),
                    )
                )
                results.append(CurrentCaptureItem(
                    ticker=ticker, dataset=dataset, status=status, endpoint=response.endpoint,
                    snapshot_id=stored.id, fetched_at=raw.fetched_at.astimezone(UTC).isoformat(),
                    row_count=len(_rows(response)), reason=reason,
                ))
            except (ProviderError, ValueError, OSError) as error:
                circuit.failure(error)
                status = CurrentCaptureStatus.SCHEMA_MISMATCH if error.__class__.__name__ == "ProviderSchemaError" else CurrentCaptureStatus.UNAVAILABLE
                results.append(CurrentCaptureItem(
                    ticker=ticker, dataset=dataset, status=status, endpoint=endpoint,
                    snapshot_id=None, fetched_at=None, row_count=None,
                    reason=f"{type(error).__name__}: {str(error)[:160]}",
                ))
    return CurrentCaptureReport(
        generated_at=now.isoformat(), tickers=clean_tickers, datasets=clean_datasets,
        preflight_passed=True, max_transport_attempts=max_attempts,
        remaining_transport_attempt_capacity_before_run=usage.remaining_before_reserve,
        results=tuple(results),
    )


def _recovery_timestamp(value: datetime, *, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def reconstruct_current_report(
    database: str | Path,
    since: datetime,
    cutoff: datetime,
    tickers: Sequence[str],
    datasets: Sequence[CurrentDataset | str] | None = None,
    remaining_before_run: int = 0,
) -> CurrentCaptureReport:
    """Reconstruct a current-capture report without touching the database.

    The recovery window is based on ``retrieved_at``: it answers which raw
    responses were actually available to the interrupted run by its cutoff.
    Historical captures and current responses from an earlier run are excluded
    through the immutable ``capture_mode`` marker.  Missing expected pairs are
    represented as explicit unavailable results rather than inherited data.
    """

    start = _recovery_timestamp(since, name="since")
    end = _recovery_timestamp(cutoff, name="cutoff")
    if start > end:
        raise ValueError("since cannot be later than cutoff")
    if isinstance(remaining_before_run, bool) or not isinstance(remaining_before_run, int) or remaining_before_run < 0:
        raise ValueError("remaining_before_run must be a non-negative integer")
    clean_tickers = _clean_tickers(tickers)
    clean_datasets = _clean_datasets(datasets)
    expected = {(ticker, dataset) for ticker in clean_tickers for dataset in clean_datasets}
    found: dict[tuple[str, CurrentDataset], CurrentCaptureItem] = {}
    path = Path(database)
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as error:
        raise ValueError(f"cannot open current-capture database read-only: {type(error).__name__}") from error
    try:
        placeholders = ",".join("?" for _ in clean_tickers)
        rows = connection.execute(
            f"""
            SELECT id, dataset, symbol, retrieved_at, metadata_json
            FROM snapshots
            WHERE symbol IN ({placeholders})
              AND retrieved_at >= ? AND retrieved_at <= ?
            ORDER BY retrieved_at DESC, id DESC
            """,
            (*clean_tickers, timestamp_text(start), timestamp_text(end)),
        ).fetchall()
    except sqlite3.Error as error:
        raise ValueError(f"cannot query current-capture snapshots: {type(error).__name__}") from error
    finally:
        connection.close()

    for row in rows:
        dataset = SNAPSHOT_DATASET_TO_CURRENT.get(str(row["dataset"]))
        ticker = str(row["symbol"] or "").upper()
        if dataset is None or (ticker, dataset) not in expected or (ticker, dataset) in found:
            continue
        try:
            metadata = json.loads(str(row["metadata_json"]))
        except json.JSONDecodeError:
            continue
        if not isinstance(metadata, dict) or metadata.get("capture_mode") != "current":
            continue
        raw_status = metadata.get("response_status")
        try:
            status = CurrentCaptureStatus(raw_status)
        except ValueError:
            status = CurrentCaptureStatus.SCHEMA_MISMATCH
        row_count = metadata.get("response_row_count")
        found[(ticker, dataset)] = CurrentCaptureItem(
            ticker=ticker,
            dataset=dataset,
            status=status,
            endpoint=str(metadata.get("provider_endpoint") or SPECS[dataset].endpoint.format(ticker=ticker)),
            snapshot_id=int(row["id"]),
            fetched_at=str(row["retrieved_at"]),
            row_count=row_count if isinstance(row_count, int) and not isinstance(row_count, bool) else None,
            reason=(
                "recovered current snapshot has an unknown response status"
                if status is CurrentCaptureStatus.SCHEMA_MISMATCH and raw_status != status.value
                else None
            ),
        )

    results: list[CurrentCaptureItem] = []
    for ticker in clean_tickers:
        for dataset in clean_datasets:
            item = found.get((ticker, dataset))
            if item is None:
                item = CurrentCaptureItem(
                    ticker=ticker, dataset=dataset, status=CurrentCaptureStatus.UNAVAILABLE,
                    endpoint=SPECS[dataset].endpoint.format(ticker=ticker), snapshot_id=None,
                    fetched_at=None, row_count=None,
                    reason="no current-capture snapshot available in the recovery window",
                )
            results.append(item)
    return CurrentCaptureReport(
        generated_at=start.isoformat(), tickers=clean_tickers, datasets=clean_datasets,
        preflight_passed=True, max_transport_attempts=0,
        remaining_transport_attempt_capacity_before_run=remaining_before_run,
        results=tuple(results),
    )
