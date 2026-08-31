"""Fail-closed intraday schedule and selective capture policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
import math
from typing import Any, Mapping, MutableMapping, Sequence
from zoneinfo import ZoneInfo

from .clock import nyse_holidays
from .current_collection import CurrentDataset
from .enhanced_collection import EnhancedDataset, GLOBAL_DATASETS


EASTERN = ZoneInfo("America/New_York")


class IntradayTier(StrEnum):
    FAST = "fast"
    MEDIUM = "medium"
    SLOW = "slow"


@dataclass(frozen=True, slots=True)
class TierPolicy:
    interval: timedelta
    current: tuple[CurrentDataset, ...]
    enhanced: tuple[EnhancedDataset, ...]


POLICY: Mapping[IntradayTier, TierPolicy] = {
    IntradayTier.FAST: TierPolicy(
        timedelta(minutes=5),
        (CurrentDataset.FLOW_ALERTS, CurrentDataset.NEWS),
        (
            EnhancedDataset.STOCK_STATE,
            EnhancedDataset.GREEK_FLOW,
            EnhancedDataset.MARKET_TIDE,
            EnhancedDataset.SECTOR_TIDE_TECHNOLOGY,
            EnhancedDataset.SECTOR_TIDE_COMMUNICATION,
            EnhancedDataset.ETF_TIDE_QQQ,
            EnhancedDataset.ETF_TIDE_SMH,
            EnhancedDataset.ETF_TIDE_SOXX,
        ),
    ),
    IntradayTier.MEDIUM: TierPolicy(
        timedelta(minutes=15),
        (CurrentDataset.OPTION_CHAIN, CurrentDataset.DEALER_EXPOSURE),
        (
            EnhancedDataset.OPTION_PRICE_LEVELS,
            EnhancedDataset.GREEK_EXPOSURE_STRIKE,
            EnhancedDataset.IV_TERM_STRUCTURE,
            EnhancedDataset.INTERPOLATED_IV,
        ),
    ),
    IntradayTier.SLOW: TierPolicy(
        timedelta(minutes=30),
        (CurrentDataset.DARK_POOL,),
        (
            EnhancedDataset.DARK_POOL_LEVELS,
            EnhancedDataset.VOLATILITY_STATS,
            EnhancedDataset.VOLATILITY_ANOMALY,
            EnhancedDataset.VOLATILITY_CHARACTER,
            EnhancedDataset.VARIANCE_RISK_PREMIUM,
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class MarketSession:
    session_date: date
    opens_at: datetime
    closes_at: datetime
    status: str
    reason: str

    @property
    def is_regular_session(self) -> bool:
        return self.status != "CLOSED"

    def is_open_at(self, value: datetime) -> bool:
        observed = _eastern(value)
        return self.is_regular_session and self.opens_at <= observed < self.closes_at


def _eastern(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(EASTERN)


def _nth_weekday(year: int, month: int, weekday: int, ordinal: int) -> date:
    current = date(year, month, 1)
    offset = (weekday - current.weekday()) % 7
    return current + timedelta(days=offset + 7 * (ordinal - 1))


def _observed(value: date) -> date:
    if value.weekday() == 5:
        return value - timedelta(days=1)
    if value.weekday() == 6:
        return value + timedelta(days=1)
    return value


def _early_close(day: date) -> bool:
    thanksgiving = _nth_weekday(day.year, 11, 3, 4)
    if day == thanksgiving + timedelta(days=1):
        return True
    if day.month == 12 and day.day == 24 and day.weekday() < 5:
        return True
    independence_observed = _observed(date(day.year, 7, 4))
    return day == independence_observed - timedelta(days=1) and day.weekday() < 5


def market_session(value: datetime) -> MarketSession:
    observed = _eastern(value)
    day = observed.date()
    if day.weekday() >= 5:
        status, reason, close_time = "CLOSED", "weekend", time(16)
    elif day in nyse_holidays(day.year):
        status, reason, close_time = "CLOSED", "NYSE holiday", time(16)
    elif _early_close(day):
        status, reason, close_time = "EARLY_CLOSE", "scheduled 13:00 ET close", time(13)
    else:
        status, reason, close_time = "REGULAR", "scheduled regular session", time(16)
    return MarketSession(
        session_date=day,
        opens_at=datetime.combine(day, time(9, 30), EASTERN),
        closes_at=datetime.combine(day, close_time, EASTERN),
        status=status,
        reason=reason,
    )


def due_tiers(now: datetime, completed: Mapping[str, str]) -> tuple[IntradayTier, ...]:
    observed = _eastern(now)
    result: list[IntradayTier] = []
    for tier, policy in POLICY.items():
        raw = completed.get(tier.value)
        if not raw:
            result.append(tier)
            continue
        try:
            last = _eastern(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            result.append(tier)
            continue
        if last.date() != observed.date() or observed - last >= policy.interval:
            result.append(tier)
    return tuple(result)


def logical_request_count(tiers: tuple[IntradayTier, ...], ticker_count: int) -> int:
    if ticker_count < 1:
        raise ValueError("ticker_count must be positive")
    total = 0
    for tier in tiers:
        policy = POLICY[tier]
        total += len(policy.current) * ticker_count
        total += sum(item not in GLOBAL_DATASETS for item in policy.enhanced) * ticker_count
        total += sum(item in GLOBAL_DATASETS for item in policy.enhanced)
    return total


def full_session_logical_request_estimate(ticker_count: int) -> int:
    session_minutes = 390
    return sum(
        (session_minutes // int(policy.interval.total_seconds() // 60))
        * logical_request_count((tier,), ticker_count)
        for tier, policy in POLICY.items()
    )


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def intraday_condition(
    baseline: Mapping[str, Any],
    enhanced: Mapping[str, Any],
    *,
    observed_at: datetime,
) -> dict[str, Any]:
    """Classify fresh context against a frozen daily thesis.

    This is a deterministic consistency check, not a new forecast. Modeled GEX,
    aggregate dark-pool activity, and volatility level do not receive a
    directional vote because they do not establish dealer inventory, owner
    intent, or price direction.
    """

    observed = _eastern(observed_at)
    thesis = baseline.get("trade_thesis")
    technical = baseline.get("technical")
    price_value = baseline.get("price")
    thesis = thesis if isinstance(thesis, Mapping) else {}
    technical = technical if isinstance(technical, Mapping) else {}
    price_value = price_value.get("value") if isinstance(price_value, Mapping) else price_value
    direction = str(thesis.get("direction") or "NEUTRAL").upper()
    direction_sign = 1 if direction == "BULLISH" else -1 if direction == "BEARISH" else 0

    stock = enhanced.get("stock_state")
    flow = enhanced.get("greek_flow")
    stock = stock if isinstance(stock, Mapping) else {}
    flow = flow if isinstance(flow, Mapping) else {}
    live_price = _number(stock.get("price"))
    daily_price = _number(price_value)
    ema20 = _number(technical.get("ema20"))
    flow_delta = _number(flow.get("directional_delta_flow"))
    persistence = _number(flow.get("delta_sign_persistence"))

    drivers: list[str] = []
    conflicts: list[str] = []
    neutral: list[str] = []
    votes: list[int] = []

    if live_price is not None and ema20 is not None and direction_sign:
        price_sign = 1 if live_price > ema20 else -1 if live_price < ema20 else 0
        aligned = price_sign == direction_sign
        (drivers if aligned else conflicts).append(
            f"Price ${live_price:.2f} is {'above' if price_sign > 0 else 'below'} EMA20 ${ema20:.2f}."
        )
        votes.append(1 if aligned else -1)
    else:
        neutral.append("Price-to-EMA20 confirmation is unavailable.")

    if flow_delta is not None and direction_sign and persistence is not None and persistence >= 0.55:
        flow_sign = 1 if flow_delta > 0 else -1 if flow_delta < 0 else 0
        if flow_sign:
            aligned = flow_sign == direction_sign
            (drivers if aligned else conflicts).append(
                f"Greek delta flow is {'positive' if flow_sign > 0 else 'negative'} with {persistence:.0%} same-sign persistence."
            )
            votes.append(1 if aligned else -1)
        else:
            neutral.append("Greek delta flow is flat.")
    elif flow_delta is not None:
        neutral.append("Greek flow persistence is below the 55% confirmation threshold.")
    else:
        neutral.append("Greek delta flow is unavailable.")

    if live_price is None:
        status = "UNAVAILABLE"
    elif not votes or direction_sign == 0:
        status = "MIXED"
    elif sum(votes) >= 2:
        status = "CONFIRMING"
    elif sum(votes) <= -2:
        status = "WEAKENING"
    else:
        status = "MIXED"

    change = live_price / daily_price - 1 if live_price is not None and daily_price not in {None, 0.0} else None
    return {
        "schema_version": "codex_screener/intraday_condition/v1",
        "status": status,
        "thesis_direction": direction,
        "observed_at": observed.isoformat(),
        "observed_price": live_price,
        "daily_anchor_price": daily_price,
        "change_from_daily_anchor": change,
        "drivers": drivers,
        "conflicts": conflicts,
        "neutral_context": neutral,
        "directional_votes": sum(votes),
        "vote_count": len(votes),
        "boundary": "Deterministic intraday consistency check; not a new forecast or probability.",
    }


_FROZEN_RECORD_FIELDS = (
    "trade_rank",
    "research_rank",
    "trade_thesis",
    "agent_enrichment",
    "agent_enrichment_validated",
    "agent_analysis",
    "analyst",
)
_FROZEN_EDGE_FIELDS = ("forecast", "forecast_v4", "historical_analogs")


def merge_frozen_daily_model(
    live_run: MutableMapping[str, Any],
    baseline_run: Mapping[str, Any],
    enhanced_summary: Mapping[str, Any],
    *,
    observed_at: datetime,
) -> MutableMapping[str, Any]:
    """Attach live evidence while preserving the published model origin."""

    baseline_rows = baseline_run.get("watchlist")
    live_rows = live_run.get("watchlist")
    symbols = enhanced_summary.get("symbols")
    if not isinstance(baseline_rows, Sequence) or isinstance(baseline_rows, (str, bytes)):
        raise ValueError("baseline watchlist must be a sequence")
    if not isinstance(live_rows, Sequence) or isinstance(live_rows, (str, bytes)):
        raise ValueError("live watchlist must be a sequence")
    symbols = symbols if isinstance(symbols, Mapping) else {}
    baseline_by_ticker = {
        str(row.get("ticker") or "").upper(): row
        for row in baseline_rows
        if isinstance(row, Mapping) and row.get("ticker")
    }
    for live in live_rows:
        if not isinstance(live, MutableMapping):
            continue
        ticker = str(live.get("ticker") or "").upper()
        baseline = baseline_by_ticker.get(ticker)
        if not isinstance(baseline, Mapping):
            raise ValueError(f"baseline record is missing for {ticker}")
        for field in _FROZEN_RECORD_FIELDS:
            if field in baseline:
                live[field] = baseline[field]
        baseline_edge = baseline.get("edge")
        live_edge = live.get("edge")
        if isinstance(baseline_edge, Mapping) and isinstance(live_edge, MutableMapping):
            for field in _FROZEN_EDGE_FIELDS:
                if field in baseline_edge:
                    live_edge[field] = baseline_edge[field]
        enhanced = symbols.get(ticker)
        enhanced = enhanced if isinstance(enhanced, Mapping) else {}
        live["intraday_condition"] = intraday_condition(baseline, enhanced, observed_at=observed_at)
    return live_run
