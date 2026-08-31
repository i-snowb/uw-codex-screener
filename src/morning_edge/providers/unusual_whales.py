"""Unusual Whales REST adapter for the Codex Screener data audit.

The endpoint constants track the official REST reference:
https://api.unusualwhales.com/docs and https://api.unusualwhales.com/api/openapi.
This is intentionally a read-only adapter. It does not trade, create alerts, or
depend on a browser session.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
import os
from typing import Any
from urllib.parse import quote

from .base import (
    JSONValue,
    AttemptBudget,
    JsonResponse,
    ProviderSchemaError,
    RawResponseHook,
    SafeGetClient,
    Transport,
    require_list,
    require_mapping,
)


OFFICIAL_DOCS_URL = "https://api.unusualwhales.com/docs"
OFFICIAL_OPENAPI_URL = "https://api.unusualwhales.com/api/openapi"
DEFAULT_BASE_URL = "https://api.unusualwhales.com"
API_KEY_ENVIRONMENT_VARIABLE = "UNUSUAL_WHALES_API_KEY"
API_HOST = "api.unusualwhales.com"
GEX_LEVEL_FIELDS = ("call_wall", "put_wall", "gamma_flip", "gamma_magnet")


def gex_levels_are_empty(data: object) -> bool:
    """Return true only for the provider's explicit no-levels GEX shape.

    A historical GEX response can be structurally present while every usable
    level is null.  That is an empty observation, not a zero-GEX observation
    and not usable dealer-exposure evidence.
    """

    return isinstance(data, Mapping) and all(data.get(field) is None for field in GEX_LEVEL_FIELDS)


def gex_unusable_level_names(data: object) -> tuple[str, ...]:
    """Name structurally present GEX levels that cannot support derivation.

    The caller separately rejects absent keys.  A null or blank named level is
    preserved as raw provider evidence but makes the complete level set unsafe
    for derived dealer-exposure features.
    """

    if not isinstance(data, Mapping):
        return ()
    return tuple(
        field
        for field in GEX_LEVEL_FIELDS
        if data.get(field) is None or (isinstance(data.get(field), str) and not data[field].strip())
    )


def _nonempty_value(row: Mapping[str, Any], field: str, *, endpoint: str, index: int) -> Any:
    """Return a required scalar-like value without silently accepting nulls."""

    value = row.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ProviderSchemaError(f"{endpoint}: row {index} missing a non-empty {field}")
    return value


def _one_of_fields(
    row: Mapping[str, Any],
    fields: tuple[str, ...],
    *,
    endpoint: str,
    index: int,
) -> tuple[str, Any]:
    """Return exactly one compatible provider field, rejecting ambiguous values."""

    present = [
        (field, value)
        for field in fields
        if (value := row.get(field)) is not None and not (isinstance(value, str) and not value.strip())
    ]
    if not present:
        raise ProviderSchemaError(f"{endpoint}: row {index} missing one of {list(fields)}")
    rendered = {str(value).strip() for _, value in present}
    if len(rendered) > 1:
        raise ProviderSchemaError(f"{endpoint}: row {index} has conflicting values for {list(fields)}")
    return present[0]


def _require_iso_date(value: Any, *, endpoint: str, field: str, index: int) -> None:
    if not isinstance(value, str):
        raise ProviderSchemaError(f"{endpoint}: row {index} {field} must be an ISO date string")
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ProviderSchemaError(f"{endpoint}: row {index} {field} must use YYYY-MM-DD") from error


def _require_utc_timestamp(value: Any, *, endpoint: str, field: str, index: int) -> None:
    if not isinstance(value, str):
        raise ProviderSchemaError(f"{endpoint}: row {index} {field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProviderSchemaError(f"{endpoint}: row {index} {field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ProviderSchemaError(f"{endpoint}: row {index} {field} must include a timezone")


def _ticker(value: str) -> str:
    ticker = value.strip().upper()
    if not ticker or not ticker.replace(".", "").replace("-", "").isalnum():
        raise ValueError("ticker must be a non-empty symbol containing letters, digits, '.' or '-'")
    return ticker


def _market_date(value: date | str | None) -> str | None:
    if value is None:
        return None
    rendered = value.isoformat() if isinstance(value, date) else value
    try:
        date.fromisoformat(rendered)
    except ValueError as error:
        raise ValueError("market date must use YYYY-MM-DD") from error
    return rendered


def _expiry_date(value: date | str) -> str:
    rendered = value.isoformat() if isinstance(value, date) else value
    try:
        date.fromisoformat(rendered)
    except ValueError as error:
        raise ValueError("expiry must use YYYY-MM-DD") from error
    return rendered


def _response_field(response: JsonResponse, *, endpoint: str, field: str = "data") -> JSONValue:
    if field == "data":
        return response.data
    payload = response.payload
    if not isinstance(payload, Mapping) or field not in payload:
        raise ProviderSchemaError(f"{endpoint}: expected a JSON object containing a {field!r} field")
    return payload[field]  # type: ignore[return-value]


def _require_object_collection(
    response: JsonResponse, *, endpoint: str, field: str = "data"
) -> None:
    """Accept documented object or object-list payloads while rejecting scalars."""

    data = _response_field(response, endpoint=endpoint, field=field)
    if isinstance(data, Mapping):
        return
    if isinstance(data, list) and all(isinstance(row, Mapping) for row in data):
        return
    raise ProviderSchemaError(f"{endpoint}: expected data to be an object or a list of objects")


@dataclass(frozen=True)
class EndpointResponse:
    """A validated endpoint response with provider and route provenance."""

    endpoint: str
    response: JsonResponse
    data_field: str = "data"

    @property
    def data(self) -> JSONValue:
        return _response_field(self.response, endpoint=self.endpoint, field=self.data_field)


class UnusualWhalesClient:
    """Read-only REST client authenticated with ``Authorization: Bearer ...``.

    Official docs specify Bearer auth for these endpoints. Token material comes
    from an explicit secret input or ``UNUSUAL_WHALES_API_KEY`` and is never
    returned, serialized, or included in exceptions.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 15.0,
        max_attempts: int = 3,
        raw_response_hook: RawResponseHook | None = None,
        request_budget: AttemptBudget | None = None,
        transport: Transport | None = None,
        sleep: Callable[[float], None] | None = None,
        random_float: Callable[[], float] | None = None,
    ) -> None:
        token = api_key.strip()
        if not token:
            raise ValueError("Unusual Whales API key is required")
        kwargs: dict[str, Any] = {
            "provider": "unusual_whales",
            "authorization": f"Bearer {token}",
            "base_url": base_url,
            "timeout_seconds": timeout_seconds,
            "max_attempts": max_attempts,
            "raw_response_hook": raw_response_hook,
            "attempt_budget": request_budget,
            "transport": transport,
            # The real HTTPS transport may only send the Bearer token to the
            # documented API host. Test transports may use a fake HTTPS origin.
            "allowed_hosts": frozenset({API_HOST}),
        }
        if sleep is not None:
            kwargs["sleep"] = sleep
        if random_float is not None:
            kwargs["random_float"] = random_float
        # A production client must receive the application's shared budget.
        # A relative default ledger would split accounting across working
        # directories and make the protected reserve unreliable. Injected
        # transports remain usable for deterministic offline tests.
        if request_budget is None and transport is None:
            raise ValueError("real UnusualWhalesClient requires an explicit shared request_budget")
        kwargs["attempt_budget"] = request_budget
        self._http = SafeGetClient(**kwargs)

    @classmethod
    def from_environment(cls, *, env: Mapping[str, str] | None = None, **kwargs: Any) -> "UnusualWhalesClient":
        environment = os.environ if env is None else env
        token = environment.get(API_KEY_ENVIRONMENT_VARIABLE, "").strip()
        if not token:
            raise ValueError(f"{API_KEY_ENVIRONMENT_VARIABLE} is not set")
        return cls(token, **kwargs)

    def option_chain(self, ticker: str, *, as_of: date | str | None = None, greeks: bool = True) -> EndpointResponse:
        """Get contract rows enriched with NBBO, IV, OI, volume, and Greeks.

        Official endpoint: ``/api/stock/{ticker}/option-chains``. The docs state
        that ``greeks=true`` changes the default symbol array into enriched rows.
        """

        endpoint = f"/api/stock/{_ticker(ticker)}/option-chains"
        response = self._http.get_json(endpoint, params={"date": _market_date(as_of), "greeks": str(greeks).lower()})
        rows = require_list(response.payload, endpoint=endpoint)
        if greeks:
            # The documented schema uses ``expiry``/``type``. The live payload
            # observed during the trial uses ``expires``/``option_type``. Keep
            # raw fields intact, but require one unambiguous alias on *every*
            # row so later normalization cannot accidentally mix contracts.
            for index, row in enumerate(rows):
                _nonempty_value(row, "option_symbol", endpoint=endpoint, index=index)
                _nonempty_value(row, "strike", endpoint=endpoint, index=index)
                expiry_field, expiry = _one_of_fields(
                    row, ("expiry", "expires"), endpoint=endpoint, index=index
                )
                _require_iso_date(expiry, endpoint=endpoint, field=expiry_field, index=index)
                _one_of_fields(row, ("type", "option_type"), endpoint=endpoint, index=index)
        return EndpointResponse(endpoint, response)

    def flow_alerts(
        self,
        ticker: str,
        *,
        unusual: bool = False,
        min_premium: float | None = None,
        max_dte: int | None = None,
        newer_than: str | int | None = None,
        older_than: str | int | None = None,
        limit: int = 100,
        page: int | None = None,
    ) -> EndpointResponse:
        """Get aggregated options-flow alerts for one ticker.

        Endpoint: ``/api/option-trades/flow-alerts``. ``unusual=true`` applies
        the provider's live-options-flow preset; it is deliberately opt-in so an
        audit can compare raw flow with the provider preset.
        """

        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if max_dte is not None and max_dte < 0:
            raise ValueError("max_dte must not be negative")
        endpoint = "/api/option-trades/flow-alerts"
        response = self._http.get_json(
            endpoint,
            params={
                "ticker_symbol": _ticker(ticker),
                "unusual": str(unusual).lower(),
                "min_premium": min_premium,
                "max_dte": max_dte,
                "newer_than": newer_than,
                "older_than": older_than,
                "limit": limit,
                "page": page,
            },
        )
        require_list(response.payload, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def option_trades(
        self,
        ticker: str,
        *,
        newer_than: str | int | None = None,
        older_than: str | int | None = None,
        limit: int = 500,
    ) -> EndpointResponse:
        """Get underlying option trades for timestamp-level flow reconstruction."""

        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        endpoint = "/api/option-trades"
        response = self._http.get_json(
            endpoint,
            params={
                "ticker_symbol": _ticker(ticker),
                "newer_than": newer_than,
                "older_than": older_than,
                "limit": limit,
            },
        )
        require_list(response.payload, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def oi_change(
        self,
        ticker: str,
        *,
        as_of: date | str | None = None,
        limit: int = 500,
        page: int | None = None,
        order: str = "desc",
    ) -> EndpointResponse:
        """Get contract-level open-interest changes and the two source dates."""

        if limit < 1 or order not in {"asc", "desc"}:
            raise ValueError("limit must be positive and order must be 'asc' or 'desc'")
        endpoint = f"/api/stock/{_ticker(ticker)}/oi-change"
        response = self._http.get_json(
            endpoint,
            params={"date": _market_date(as_of), "limit": limit, "page": page, "order": order},
        )
        require_list(response.payload, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def gex_levels(self, ticker: str, *, as_of: date | str | None = None) -> EndpointResponse:
        """Get the provider's call wall, put wall, gamma flip, and gamma magnet."""

        endpoint = f"/api/stock/{_ticker(ticker)}/gex-levels"
        requested_date = _market_date(as_of)
        response = self._http.get_json(endpoint, params={"date": requested_date})
        data = require_mapping(response.payload, endpoint=endpoint)
        expected = set(GEX_LEVEL_FIELDS)
        missing = expected - set(data)
        if missing:
            raise ProviderSchemaError(f"{endpoint}: missing GEX levels {sorted(missing)}")
        if gex_levels_are_empty(data):
            return EndpointResponse(endpoint, response)

        # A populated or partially populated level set must identify the
        # provider observation. A partial set is retained by backfill as raw
        # evidence but explicitly excluded from derived GEX; a malformed date
        # or time is still a schema failure because its session is unknown.
        provider_date = _nonempty_value(data, "date", endpoint=endpoint, index=0)
        _require_iso_date(provider_date, endpoint=endpoint, field="date", index=0)
        provider_time = _nonempty_value(data, "time", endpoint=endpoint, index=0)
        _require_utc_timestamp(provider_time, endpoint=endpoint, field="time", index=0)
        if requested_date is not None and provider_date != requested_date:
            raise ProviderSchemaError(
                f"{endpoint}: provider date {provider_date!r} does not match requested date {requested_date!r}"
            )
        return EndpointResponse(endpoint, response)

    def greek_exposure_by_strike(
        self, ticker: str, *, as_of: date | str | None = None
    ) -> EndpointResponse:
        """Get call/put gamma, vanna, charm, and delta exposure by strike."""

        endpoint = f"/api/stock/{_ticker(ticker)}/greek-exposure/strike"
        response = self._http.get_json(endpoint, params={"date": _market_date(as_of)})
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def greek_exposure_by_strike_expiry(
        self,
        ticker: str,
        *,
        expiry: date | str,
        as_of: date | str | None = None,
    ) -> EndpointResponse:
        """Get strike Greek exposure scoped to one explicit option expiry."""

        endpoint = f"/api/stock/{_ticker(ticker)}/greek-exposure/strike-expiry"
        response = self._http.get_json(
            endpoint,
            params={"date": _market_date(as_of), "expiry": _expiry_date(expiry)},
        )
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def greek_flow(self, ticker: str, *, as_of: date | str | None = None) -> EndpointResponse:
        """Get minute-level directional and total delta/vega option flow."""

        endpoint = f"/api/stock/{_ticker(ticker)}/greek-flow"
        response = self._http.get_json(endpoint, params={"date": _market_date(as_of)})
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def greek_flow_by_expiry(
        self,
        ticker: str,
        *,
        expiry: date | str,
        as_of: date | str | None = None,
    ) -> EndpointResponse:
        """Get minute-level Greek flow for one explicit option expiry."""

        endpoint = f"/api/stock/{_ticker(ticker)}/greek-flow/{_expiry_date(expiry)}"
        response = self._http.get_json(endpoint, params={"date": _market_date(as_of)})
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def iv_term_structure(self, ticker: str, *, as_of: date | str | None = None) -> EndpointResponse:
        """Get ATM IV and implied move across listed expirations."""

        endpoint = f"/api/stock/{_ticker(ticker)}/volatility/term-structure"
        response = self._http.get_json(endpoint, params={"date": _market_date(as_of)})
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def volatility_stats(self, ticker: str, *, as_of: date | str | None = None) -> EndpointResponse:
        """Get provider IV/RV levels, ranges, and IV rank."""

        endpoint = f"/api/stock/{_ticker(ticker)}/volatility/stats"
        response = self._http.get_json(endpoint, params={"date": _market_date(as_of)})
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def interpolated_iv(self, ticker: str, *, as_of: date | str | None = None) -> EndpointResponse:
        """Get fixed-horizon interpolated IV, implied move, and percentile."""

        endpoint = f"/api/stock/{_ticker(ticker)}/interpolated-iv"
        response = self._http.get_json(endpoint, params={"date": _market_date(as_of)})
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def stock_state(self, ticker: str) -> EndpointResponse:
        """Get the provider's latest stock-state/tape observation."""

        endpoint = f"/api/stock/{_ticker(ticker)}/stock-state"
        response = self._http.get_json(endpoint)
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def options_pulse(self, ticker: str, *, as_of: date | str | None = None) -> EndpointResponse:
        """Get the provider's ticker-level options pulse for one market date."""

        endpoint = f"/api/stock/{_ticker(ticker)}/options-pulse"
        response = self._http.get_json(endpoint, params={"date": _market_date(as_of)})
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def option_price_levels(
        self, ticker: str, *, as_of: date | str | None = None
    ) -> EndpointResponse:
        """Get call and put option volume aggregated by underlying price level."""

        endpoint = f"/api/stock/{_ticker(ticker)}/option/stock-price-levels"
        response = self._http.get_json(endpoint, params={"date": _market_date(as_of)})
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def stock_volume_price_levels(
        self, ticker: str, *, as_of: date | str | None = None
    ) -> EndpointResponse:
        """Get FINRA off-exchange and Nasdaq-operated lit volume by price."""

        endpoint = f"/api/stock/{_ticker(ticker)}/stock-volume-price-levels"
        response = self._http.get_json(endpoint, params={"date": _market_date(as_of)})
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def volatility_anomaly(
        self, ticker: str, *, as_of: date | str | None = None
    ) -> EndpointResponse:
        """Get the provider's volatility-anomaly diagnostics."""

        endpoint = f"/api/stock/{_ticker(ticker)}/volatility/anomaly"
        response = self._http.get_json(endpoint, params={"date": _market_date(as_of)})
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def volatility_character(
        self, ticker: str, *, as_of: date | str | None = None
    ) -> EndpointResponse:
        """Get the provider's volatility-character diagnostics."""

        endpoint = f"/api/stock/{_ticker(ticker)}/volatility/character"
        response = self._http.get_json(endpoint, params={"date": _market_date(as_of)})
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def variance_risk_premium(
        self, ticker: str, *, as_of: date | str | None = None
    ) -> EndpointResponse:
        """Get the provider's variance-risk-premium diagnostics."""

        endpoint = f"/api/stock/{_ticker(ticker)}/volatility/variance-risk-premium"
        response = self._http.get_json(endpoint, params={"date": _market_date(as_of)})
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def darkpool_price_levels(
        self, ticker: str, *, as_of: date | str | None = None
    ) -> EndpointResponse:
        """Get dark-pool and regular volume aggregated by execution price."""

        endpoint = f"/api/darkpool/{_ticker(ticker)}/price-levels"
        response = self._http.get_json(endpoint, params={"date": _market_date(as_of)})
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def market_tide(
        self,
        *,
        as_of: date | str | None = None,
        otm_only: bool = False,
        interval_5m: bool = True,
    ) -> EndpointResponse:
        """Get market-wide net call/put premium and option volume tide."""

        endpoint = "/api/market/market-tide"
        response = self._http.get_json(
            endpoint,
            params={
                "date": _market_date(as_of),
                "otm_only": str(otm_only).lower(),
                "interval_5m": str(interval_5m).lower(),
            },
        )
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def etf_tide(self, ticker: str, *, as_of: date | str | None = None) -> EndpointResponse:
        """Get five-minute net option premium and volume for an ETF."""

        endpoint = f"/api/market/{_ticker(ticker)}/etf-tide"
        response = self._http.get_json(endpoint, params={"date": _market_date(as_of)})
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def market_correlations(
        self,
        tickers: Sequence[str],
        *,
        interval: str = "1d",
        start_date: date | str,
        end_date: date | str,
    ) -> EndpointResponse:
        """Get pairwise market-data correlations for a bounded ticker set."""

        clean = tuple(dict.fromkeys(_ticker(item) for item in tickers))
        if len(clean) < 2 or len(clean) > 25:
            raise ValueError("market correlations require between 2 and 25 unique tickers")
        if interval not in {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}:
            raise ValueError("unsupported market-correlation interval")
        endpoint = "/api/market/correlations"
        response = self._http.get_json(endpoint, params={
            "tickers": ",".join(clean), "interval": interval,
            "start_date": _market_date(start_date), "end_date": _market_date(end_date),
        })
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def economic_calendar(self, *, as_of: date | str | None = None) -> EndpointResponse:
        """Get scheduled macroeconomic events for one market date."""

        endpoint = "/api/market/economic-calendar"
        response = self._http.get_json(endpoint, params={"date": _market_date(as_of)})
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def sector_tide(
        self,
        sector: str,
        *,
        as_of: date | str | None = None,
    ) -> EndpointResponse:
        """Get sector-wide net call/put premium and option volume tide."""

        cleaned = sector.strip()
        allowed = {
            "Basic Materials", "Communication Services", "Consumer Cyclical",
            "Consumer Defensive", "Energy", "Financial Services", "Healthcare",
            "Industrials", "Real Estate", "Technology", "Utilities",
        }
        if cleaned not in allowed:
            raise ValueError(f"sector must be one of {sorted(allowed)}")
        endpoint = f"/api/market/{quote(cleaned, safe='')}/sector-tide"
        response = self._http.get_json(endpoint, params={"date": _market_date(as_of)})
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def short_interest_float(self, ticker: str) -> EndpointResponse:
        """Get current short interest, float, days-to-cover, and borrow fields."""

        endpoint = f"/api/shorts/{_ticker(ticker)}/interest-float/v2"
        response = self._http.get_json(endpoint)
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def short_borrow(self, ticker: str) -> EndpointResponse:
        """Get borrow fee, rebate, and shares-available observations."""

        endpoint = f"/api/shorts/{_ticker(ticker)}/data"
        response = self._http.get_json(endpoint)
        _require_object_collection(response, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def short_volume_ratio(self, ticker: str) -> EndpointResponse:
        """Get historical short-volume and total-volume ratios."""

        endpoint = f"/api/shorts/{_ticker(ticker)}/volume-and-ratio"
        response = self._http.get_json(endpoint)
        # The live API currently uses the legacy top-level ``si`` envelope for
        # this route although the common API contract uses ``data``. Preserve
        # the raw payload and record the explicit route-local alias.
        _require_object_collection(response, endpoint=endpoint, field="si")
        return EndpointResponse(endpoint, response, data_field="si")

    def darkpool_trades(
        self,
        ticker: str,
        *,
        as_of: date | str | None = None,
        newer_than: str | int | None = None,
        older_than: str | int | None = None,
        min_premium: float | None = None,
        limit: int = 500,
        order: str = "desc",
        order_by: str = "executed_at",
    ) -> EndpointResponse:
        """Get ticker dark-pool prints, including tape and TRF execution times."""

        if not 1 <= limit <= 500 or order not in {"asc", "desc"}:
            raise ValueError("limit must be between 1 and 500 and order must be 'asc' or 'desc'")
        if order_by not in {"executed_at", "trf_executed_at", "premium", "size", "volume"}:
            raise ValueError("unsupported dark-pool order_by")
        endpoint = f"/api/darkpool/{_ticker(ticker)}"
        response = self._http.get_json(
            endpoint,
            params={
                "date": _market_date(as_of),
                "newer_than": newer_than,
                "older_than": older_than,
                "min_premium": min_premium,
                "limit": limit,
                "order": order,
                "order_by": order_by,
            },
        )
        require_list(response.payload, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def ohlc(
        self,
        ticker: str,
        *,
        candle_size: str = "1d",
        timeframe: str = "1Y",
        end_date: date | str | None = None,
        limit: int | None = None,
    ) -> EndpointResponse:
        """Get OHLC candles. Candle end times are documented as UTC timestamps."""

        allowed = {"1m", "5m", "10m", "15m", "30m", "1h", "4h", "1d", "1w"}
        if candle_size not in allowed:
            raise ValueError(f"candle_size must be one of {sorted(allowed)}")
        if limit is not None and not 1 <= limit <= 2500:
            raise ValueError("limit must be between 1 and 2500")
        endpoint = f"/api/stock/{_ticker(ticker)}/ohlc/{candle_size}"
        response = self._http.get_json(
            endpoint,
            params={"timeframe": timeframe, "end_date": _market_date(end_date), "limit": limit},
        )
        rows = require_list(response.payload, endpoint=endpoint)
        # Historical documentation names ``end_time``. The live daily payload
        # provides only ``date`` (a trading-session date). Do not invent a UTC
        # close timestamp from that date; retain its day-level semantics for the
        # normalizer and reject rows that provide neither field.
        for index, row in enumerate(rows):
            end_time = row.get("end_time")
            if end_time is not None and not (isinstance(end_time, str) and not end_time.strip()):
                _require_utc_timestamp(end_time, endpoint=endpoint, field="end_time", index=index)
                continue
            date_value = row.get("date")
            if date_value is None or (isinstance(date_value, str) and not date_value.strip()):
                raise ProviderSchemaError(f"{endpoint}: row {index} missing one of ['end_time', 'date']")
            _require_iso_date(date_value, endpoint=endpoint, field="date", index=index)
        return EndpointResponse(endpoint, response)

    def news_headlines(
        self,
        ticker: str,
        *,
        major_only: bool = False,
        limit: int = 100,
        page: int | None = None,
    ) -> EndpointResponse:
        """Get news headlines. ``created_at`` is the published/created timestamp."""

        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        endpoint = "/api/news/headlines"
        response = self._http.get_json(
            endpoint,
            params={"ticker": _ticker(ticker), "major_only": str(major_only).lower(), "limit": limit, "page": page},
        )
        require_list(response.payload, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def earnings_history(self, ticker: str) -> EndpointResponse:
        """Get historical ticker earnings, report date/time, and realized moves."""

        endpoint = f"/api/earnings/{_ticker(ticker)}"
        response = self._http.get_json(endpoint)
        require_list(response.payload, endpoint=endpoint)
        return EndpointResponse(endpoint, response)

    def paged_oi_change(
        self,
        ticker: str,
        *,
        as_of: date | str | None = None,
        limit: int = 500,
        max_pages: int = 20,
    ) -> Sequence[EndpointResponse]:
        """Bounded page traversal for OI changes; page numbering starts at zero."""

        endpoint = f"/api/stock/{_ticker(ticker)}/oi-change"
        pages = self._http.iter_pages(
            endpoint,
            params={"date": _market_date(as_of), "limit": limit, "order": "desc"},
            start_page=0,
            max_pages=max_pages,
        )
        return tuple(EndpointResponse(endpoint, page) for page in pages)
