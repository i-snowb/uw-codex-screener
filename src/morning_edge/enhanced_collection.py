"""Quota-bounded capture for compact Unusual Whales analytical datasets.

These feeds supplement the base chain/GEX/flow collector. Responses remain raw,
append-only evidence. Derived scores belong in the feature layer and must retain
the snapshot IDs produced here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from .models import Dataset, SnapshotEnvelope
from .providers.base import ProviderError
from .providers.budget import WeeklyRequestBudget
from .providers.unusual_whales import EndpointResponse
from .store import SnapshotStore


class EnhancedDataset(StrEnum):
    STOCK_STATE = "stock_state"
    OPTION_PRICE_LEVELS = "option_price_levels"
    GREEK_EXPOSURE_STRIKE = "greek_exposure_strike"
    GREEK_FLOW = "greek_flow"
    IV_TERM_STRUCTURE = "iv_term_structure"
    VOLATILITY_STATS = "volatility_stats"
    INTERPOLATED_IV = "interpolated_iv"
    VOLATILITY_ANOMALY = "volatility_anomaly"
    VOLATILITY_CHARACTER = "volatility_character"
    VARIANCE_RISK_PREMIUM = "variance_risk_premium"
    DARK_POOL_LEVELS = "dark_pool_levels"
    SHORT_INTEREST = "short_interest"
    SHORT_BORROW = "short_borrow"
    SHORT_VOLUME = "short_volume"
    MARKET_TIDE = "market_tide"
    SECTOR_TIDE_TECHNOLOGY = "sector_tide_technology"
    SECTOR_TIDE_COMMUNICATION = "sector_tide_communication_services"
    ETF_TIDE_QQQ = "etf_tide_qqq"
    ETF_TIDE_SMH = "etf_tide_smh"
    ETF_TIDE_SOXX = "etf_tide_soxx"
    ECONOMIC_CALENDAR = "economic_calendar"


TICKER_DATASETS = (
    EnhancedDataset.STOCK_STATE,
    EnhancedDataset.OPTION_PRICE_LEVELS,
    EnhancedDataset.GREEK_EXPOSURE_STRIKE,
    EnhancedDataset.GREEK_FLOW,
    EnhancedDataset.IV_TERM_STRUCTURE,
    EnhancedDataset.VOLATILITY_STATS,
    EnhancedDataset.INTERPOLATED_IV,
    EnhancedDataset.VOLATILITY_ANOMALY,
    EnhancedDataset.VOLATILITY_CHARACTER,
    EnhancedDataset.VARIANCE_RISK_PREMIUM,
    EnhancedDataset.DARK_POOL_LEVELS,
    EnhancedDataset.SHORT_INTEREST,
    EnhancedDataset.SHORT_BORROW,
    EnhancedDataset.SHORT_VOLUME,
)

GLOBAL_DATASETS = (
    EnhancedDataset.MARKET_TIDE,
    EnhancedDataset.SECTOR_TIDE_TECHNOLOGY,
    EnhancedDataset.SECTOR_TIDE_COMMUNICATION,
    EnhancedDataset.ETF_TIDE_QQQ,
    EnhancedDataset.ETF_TIDE_SMH,
    EnhancedDataset.ETF_TIDE_SOXX,
    EnhancedDataset.ECONOMIC_CALENDAR,
)

DEFAULT_ENHANCED_DATASETS = TICKER_DATASETS + GLOBAL_DATASETS

# Short-interest and borrow fields move more slowly and need not consume daily
# capacity after the initial capture. They remain available through --dataset.
DEFAULT_DAILY_ENHANCED_DATASETS = tuple(
    item
    for item in DEFAULT_ENHANCED_DATASETS
    if item not in {
        EnhancedDataset.SHORT_INTEREST,
        EnhancedDataset.SHORT_BORROW,
        EnhancedDataset.SHORT_VOLUME,
    }
)


class EnhancedClient(Protocol):
    def stock_state(self, ticker: str) -> EndpointResponse: ...
    def option_price_levels(self, ticker: str, *, as_of: object = None) -> EndpointResponse: ...
    def greek_exposure_by_strike(self, ticker: str, *, as_of: object = None) -> EndpointResponse: ...
    def greek_flow(self, ticker: str, *, as_of: object = None) -> EndpointResponse: ...
    def iv_term_structure(self, ticker: str, *, as_of: object = None) -> EndpointResponse: ...
    def volatility_stats(self, ticker: str, *, as_of: object = None) -> EndpointResponse: ...
    def interpolated_iv(self, ticker: str, *, as_of: object = None) -> EndpointResponse: ...
    def volatility_anomaly(self, ticker: str, *, as_of: object = None) -> EndpointResponse: ...
    def volatility_character(self, ticker: str, *, as_of: object = None) -> EndpointResponse: ...
    def variance_risk_premium(self, ticker: str, *, as_of: object = None) -> EndpointResponse: ...
    def darkpool_price_levels(self, ticker: str, *, as_of: object = None) -> EndpointResponse: ...
    def short_interest_float(self, ticker: str) -> EndpointResponse: ...
    def short_borrow(self, ticker: str) -> EndpointResponse: ...
    def short_volume_ratio(self, ticker: str) -> EndpointResponse: ...
    def market_tide(
        self, *, as_of: object = None, otm_only: bool = False, interval_5m: bool = True
    ) -> EndpointResponse: ...
    def sector_tide(self, sector: str, *, as_of: object = None) -> EndpointResponse: ...
    def etf_tide(self, ticker: str, *, as_of: object = None) -> EndpointResponse: ...
    def economic_calendar(self, *, as_of: object = None) -> EndpointResponse: ...


@dataclass(frozen=True, slots=True)
class _Spec:
    snapshot_dataset: Dataset
    endpoint: str
    scope: str
    symbol: str | None
    timestamp_semantics: str
    collect: Callable[[EnhancedClient, str | None], EndpointResponse]


def _ticker_call(method: str) -> Callable[[EnhancedClient, str | None], EndpointResponse]:
    def collect(client: EnhancedClient, ticker: str | None) -> EndpointResponse:
        if ticker is None:
            raise ValueError("ticker-scoped enhanced dataset requires a symbol")
        return getattr(client, method)(ticker)

    return collect


def _market_tide(client: EnhancedClient, _symbol: str | None) -> EndpointResponse:
    return client.market_tide(otm_only=False, interval_5m=True)


def _technology_tide(client: EnhancedClient, _symbol: str | None) -> EndpointResponse:
    return client.sector_tide("Technology")


def _communication_tide(client: EnhancedClient, _symbol: str | None) -> EndpointResponse:
    return client.sector_tide("Communication Services")


def _etf_tide(symbol: str) -> Callable[[EnhancedClient, str | None], EndpointResponse]:
    def collect(client: EnhancedClient, _planned_symbol: str | None) -> EndpointResponse:
        return client.etf_tide(symbol)
    return collect


def _economic_calendar(client: EnhancedClient, _symbol: str | None) -> EndpointResponse:
    return client.economic_calendar()


SPECS: Mapping[EnhancedDataset, _Spec] = {
    EnhancedDataset.STOCK_STATE: _Spec(
        Dataset.EQUITY_QUOTE, "/api/stock/{ticker}/stock-state", "ticker", None,
        "Latest provider stock state retains market and tape labels; it reconciles freshness but does not replace the last regular-session close.",
        _ticker_call("stock_state"),
    ),
    EnhancedDataset.OPTION_PRICE_LEVELS: _Spec(
        Dataset.OPTION_PRICE_LEVELS, "/api/stock/{ticker}/option/stock-price-levels", "ticker", None,
        "Call and put volume by underlying price is activity concentration, not opening direction or owner intent.",
        _ticker_call("option_price_levels"),
    ),
    EnhancedDataset.GREEK_EXPOSURE_STRIKE: _Spec(
        Dataset.GREEK_EXPOSURE, "/api/stock/{ticker}/greek-exposure/strike", "ticker", None,
        "Provider exposure surface for the latest available market session; exposure is modeled from listed options, not verified dealer inventory.",
        _ticker_call("greek_exposure_by_strike"),
    ),
    EnhancedDataset.GREEK_FLOW: _Spec(
        Dataset.GREEK_FLOW, "/api/stock/{ticker}/greek-flow", "ticker", None,
        "Minute rows retain provider timestamps. Directional Greek flow is classified option activity, not proof of opening buyer intent.",
        _ticker_call("greek_flow"),
    ),
    EnhancedDataset.IV_TERM_STRUCTURE: _Spec(
        Dataset.IV_TERM_STRUCTURE, "/api/stock/{ticker}/volatility/term-structure", "ticker", None,
        "Expiry rows retain provider dates and describe modeled ATM IV and implied move, not an executable option quote.",
        _ticker_call("iv_term_structure"),
    ),
    EnhancedDataset.VOLATILITY_STATS: _Spec(
        Dataset.VOLATILITY_STATS, "/api/stock/{ticker}/volatility/stats", "ticker", None,
        "Provider IV/RV statistics retain their source dates and lookback semantics.",
        _ticker_call("volatility_stats"),
    ),
    EnhancedDataset.INTERPOLATED_IV: _Spec(
        Dataset.INTERPOLATED_IV, "/api/stock/{ticker}/interpolated-iv", "ticker", None,
        "Fixed-horizon interpolated IV and implied moves are modeled volatility observations.",
        _ticker_call("interpolated_iv"),
    ),
    EnhancedDataset.VOLATILITY_ANOMALY: _Spec(
        Dataset.VOLATILITY_DIAGNOSTICS, "/api/stock/{ticker}/volatility/anomaly", "ticker", None,
        "Provider anomaly history is volatility context; it is not a directional forecast.",
        _ticker_call("volatility_anomaly"),
    ),
    EnhancedDataset.VOLATILITY_CHARACTER: _Spec(
        Dataset.VOLATILITY_DIAGNOSTICS, "/api/stock/{ticker}/volatility/character", "ticker", None,
        "Provider volatility character is descriptive regime context.",
        _ticker_call("volatility_character"),
    ),
    EnhancedDataset.VARIANCE_RISK_PREMIUM: _Spec(
        Dataset.VOLATILITY_DIAGNOSTICS, "/api/stock/{ticker}/volatility/variance-risk-premium", "ticker", None,
        "Variance-risk-premium history can lag the current session and must retain its latest provider date.",
        _ticker_call("variance_risk_premium"),
    ),
    EnhancedDataset.DARK_POOL_LEVELS: _Spec(
        Dataset.DARK_POOL_LEVELS, "/api/darkpool/{ticker}/price-levels", "ticker", None,
        "Price-level aggregation does not identify beneficial owner, opening status, or trade thesis.",
        _ticker_call("darkpool_price_levels"),
    ),
    EnhancedDataset.SHORT_INTEREST: _Spec(
        Dataset.SHORT_INTEREST, "/api/shorts/{ticker}/interest-float/v2", "ticker", None,
        "Short interest and float fields update on provider/source schedules and are not necessarily current-session observations.",
        _ticker_call("short_interest_float"),
    ),
    EnhancedDataset.SHORT_BORROW: _Spec(
        Dataset.BORROW, "/api/shorts/{ticker}/data", "ticker", None,
        "Borrow availability, fee, and rebate rows retain provider timestamps and can be venue-specific estimates.",
        _ticker_call("short_borrow"),
    ),
    EnhancedDataset.SHORT_VOLUME: _Spec(
        Dataset.SHORT_VOLUME, "/api/shorts/{ticker}/volume-and-ratio", "ticker", None,
        "Short volume is transaction classification, not a change in outstanding short interest.",
        _ticker_call("short_volume_ratio"),
    ),
    EnhancedDataset.MARKET_TIDE: _Spec(
        Dataset.MARKET_TIDE, "/api/market/market-tide", "global", "MARKET",
        "Market-wide five-minute net premium/volume rows retain provider timestamps.", _market_tide,
    ),
    EnhancedDataset.SECTOR_TIDE_TECHNOLOGY: _Spec(
        Dataset.SECTOR_TIDE, "/api/market/Technology/sector-tide", "global", "SECTOR:TECHNOLOGY",
        "Technology-sector net premium/volume rows retain provider timestamps.", _technology_tide,
    ),
    EnhancedDataset.SECTOR_TIDE_COMMUNICATION: _Spec(
        Dataset.SECTOR_TIDE, "/api/market/Communication%20Services/sector-tide", "global", "SECTOR:COMMUNICATION_SERVICES",
        "Communication Services net premium/volume rows retain provider timestamps.", _communication_tide,
    ),
    EnhancedDataset.ETF_TIDE_QQQ: _Spec(
        Dataset.MARKET_TIDE, "/api/market/QQQ/etf-tide", "global", "ETF:QQQ",
        "QQQ option tide is benchmark-demand context, not a standalone forecast.", _etf_tide("QQQ"),
    ),
    EnhancedDataset.ETF_TIDE_SMH: _Spec(
        Dataset.MARKET_TIDE, "/api/market/SMH/etf-tide", "global", "ETF:SMH",
        "SMH option tide is semiconductor-demand context, not a standalone forecast.", _etf_tide("SMH"),
    ),
    EnhancedDataset.ETF_TIDE_SOXX: _Spec(
        Dataset.MARKET_TIDE, "/api/market/SOXX/etf-tide", "global", "ETF:SOXX",
        "SOXX option tide is semiconductor-demand context, not a standalone forecast.", _etf_tide("SOXX"),
    ),
    EnhancedDataset.ECONOMIC_CALENDAR: _Spec(
        Dataset.CATALYST, "/api/market/economic-calendar", "global", "MARKET:CALENDAR",
        "Scheduled macro events are catalyst-risk context; forecast and prior values can be missing or revised.", _economic_calendar,
    ),
}


class EnhancedCaptureStatus(StrEnum):
    CAPTURED = "captured"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    SCHEMA_MISMATCH = "schema_mismatch"
    BUDGET_BLOCKED = "budget_blocked"


@dataclass(frozen=True, slots=True)
class EnhancedCaptureItem:
    dataset: EnhancedDataset
    symbol: str | None
    status: EnhancedCaptureStatus
    endpoint: str
    snapshot_id: int | None
    fetched_at: str | None
    row_count: int | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class EnhancedCaptureReport:
    generated_at: str
    tickers: tuple[str, ...]
    datasets: tuple[EnhancedDataset, ...]
    preflight_passed: bool
    logical_items: int
    max_transport_attempts: int
    remaining_transport_attempt_capacity_before_run: int
    results: tuple[EnhancedCaptureItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "tickers": list(self.tickers),
            "datasets": [item.value for item in self.datasets],
            "preflight_passed": self.preflight_passed,
            "logical_items": self.logical_items,
            "max_transport_attempts": self.max_transport_attempts,
            "remaining_transport_attempt_capacity_before_run": self.remaining_transport_attempt_capacity_before_run,
            "recommendations_enabled": False,
            "results": [
                asdict(item) | {"dataset": item.dataset.value, "status": item.status.value}
                for item in self.results
            ],
        }


def _clean_tickers(tickers: Sequence[str]) -> tuple[str, ...]:
    clean = tuple(dict.fromkeys(item.strip().upper() for item in tickers if item.strip()))
    if not clean:
        raise ValueError("at least one ticker is required")
    if any(not item.replace(".", "").replace("-", "").isalnum() for item in clean):
        raise ValueError("ticker must contain letters, digits, '.' or '-'")
    return clean


def _clean_datasets(datasets: Sequence[EnhancedDataset | str] | None) -> tuple[EnhancedDataset, ...]:
    source = DEFAULT_ENHANCED_DATASETS if datasets is None else datasets
    clean = tuple(dict.fromkeys(EnhancedDataset(item) for item in source))
    if not clean:
        raise ValueError("at least one enhanced dataset is required")
    return clean


def _row_count(response: EndpointResponse) -> int:
    data = response.data
    if isinstance(data, list):
        return len(data)
    return 1 if isinstance(data, dict) and data else 0


def _items(tickers: tuple[str, ...], datasets: tuple[EnhancedDataset, ...]) -> tuple[tuple[EnhancedDataset, str | None], ...]:
    planned: list[tuple[EnhancedDataset, str | None]] = []
    for dataset in datasets:
        spec = SPECS[dataset]
        if spec.scope == "ticker":
            planned.extend((dataset, ticker) for ticker in tickers)
        else:
            planned.append((dataset, spec.symbol))
    return tuple(planned)


def collect_enhanced(
    *,
    client: EnhancedClient,
    snapshots: SnapshotStore,
    request_budget: WeeklyRequestBudget,
    tickers: Sequence[str],
    datasets: Sequence[EnhancedDataset | str] | None = None,
    max_transport_attempts_per_item: int = 3,
    generated_at: datetime | None = None,
) -> EnhancedCaptureReport:
    """Capture compact current analytical feeds without consuming reserve."""

    if isinstance(max_transport_attempts_per_item, bool) or not isinstance(max_transport_attempts_per_item, int) or max_transport_attempts_per_item < 1:
        raise ValueError("max_transport_attempts_per_item must be a positive integer")
    clean_tickers = _clean_tickers(tickers)
    clean_datasets = _clean_datasets(datasets)
    now = generated_at or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    now = now.astimezone(UTC)
    planned = _items(clean_tickers, clean_datasets)
    maximum = len(planned) * max_transport_attempts_per_item
    usage = request_budget.usage(now=now)
    if maximum > usage.remaining_before_reserve:
        blocked = tuple(
            EnhancedCaptureItem(
                dataset=dataset, symbol=symbol, status=EnhancedCaptureStatus.BUDGET_BLOCKED,
                endpoint=SPECS[dataset].endpoint.format(ticker=symbol or ""), snapshot_id=None,
                fetched_at=None, row_count=None,
                reason=f"preflight blocked: maximum transport attempts {maximum} exceeds remaining capacity {usage.remaining_before_reserve}",
            )
            for dataset, symbol in planned
        )
        return EnhancedCaptureReport(
            now.isoformat(), clean_tickers, clean_datasets, False, len(planned), maximum,
            usage.remaining_before_reserve, blocked,
        )

    results: list[EnhancedCaptureItem] = []
    for dataset, planned_symbol in planned:
        spec = SPECS[dataset]
        ticker = planned_symbol if spec.scope == "ticker" else None
        endpoint = spec.endpoint.format(ticker=ticker or "")
        try:
            response = spec.collect(client, ticker)
            count = _row_count(response)
            status = EnhancedCaptureStatus.CAPTURED if count else EnhancedCaptureStatus.EMPTY
            raw = response.response.raw
            stored = snapshots.insert(SnapshotEnvelope(
                provider="unusual_whales",
                dataset=spec.snapshot_dataset,
                symbol=planned_symbol,
                as_of=raw.fetched_at,
                retrieved_at=raw.fetched_at,
                payload=response.response.payload,
                metadata={
                    "capture_mode": "enhanced_current",
                    "raw_only": True,
                    "recommendations_enabled": False,
                    "enhanced_dataset": dataset.value,
                    "scope": spec.scope,
                    "provider_endpoint": response.endpoint,
                    "response_status": status.value,
                    "response_row_count": count,
                    "timestamp_semantics": spec.timestamp_semantics,
                    "transport_attempts": raw.attempts,
                    "rate_limit_remaining": raw.rate_limit.remaining,
                },
            ))
            results.append(EnhancedCaptureItem(
                dataset, planned_symbol, status, response.endpoint, stored.id,
                raw.fetched_at.astimezone(UTC).isoformat(), count,
            ))
        except (ProviderError, ValueError, OSError) as error:
            status = EnhancedCaptureStatus.SCHEMA_MISMATCH if error.__class__.__name__ == "ProviderSchemaError" else EnhancedCaptureStatus.UNAVAILABLE
            results.append(EnhancedCaptureItem(
                dataset, planned_symbol, status, endpoint, None, None, None,
                f"{type(error).__name__}: {str(error)[:200]}",
            ))
    return EnhancedCaptureReport(
        now.isoformat(), clean_tickers, clean_datasets, True, len(planned), maximum,
        usage.remaining_before_reserve, tuple(results),
    )
