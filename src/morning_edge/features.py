from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import log, sqrt
from statistics import fmean, pstdev
from typing import Iterable, Sequence

from .clock import ensure_aware


TRADING_DAYS = 252


@dataclass(frozen=True, slots=True)
class DailyBar:
    session_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    available_at: datetime

    def __post_init__(self) -> None:
        ensure_aware(self.available_at)
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC values must be positive")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("high is below another OHLC value")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("low is above another OHLC value")
        if self.volume < 0:
            raise ValueError("volume must be non-negative")


@dataclass(frozen=True, slots=True)
class TrendFeatures:
    close: float
    return_5d: float | None
    return_20d: float | None
    return_63d: float | None
    ema_20: float | None
    ema_50: float | None
    ema_200: float | None
    realized_vol_20: float | None
    atr_14_pct: float | None
    volume_ratio_20: float | None
    relative_return_20d: float | None
    observations: int


@dataclass(frozen=True, slots=True)
class FlowObservation:
    directional_premium: float
    gross_premium: float
    qualifying_prints: int
    single_leg_share: float
    opening_share: float
    next_day_oi_change: int | None
    qualifying_trade_volume: int

    def __post_init__(self) -> None:
        if self.gross_premium < 0:
            raise ValueError("gross_premium must be non-negative")
        if self.qualifying_prints < 0 or self.qualifying_trade_volume < 0:
            raise ValueError("flow counts must be non-negative")
        if not 0 <= self.single_leg_share <= 1:
            raise ValueError("single_leg_share must be between 0 and 1")
        if not 0 <= self.opening_share <= 1:
            raise ValueError("opening_share must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class FlowFeatures:
    directional_premium: float
    own_history_percentile: float | None
    own_history_zscore: float | None
    quality_multiplier: float
    oi_confirmation_ratio: float | None
    confirmed: bool


@dataclass(frozen=True, slots=True)
class VolatilityFeatures:
    iv: float
    realized_vol_20: float | None
    iv_rv_gap: float | None
    iv_percentile_90d: float | None
    front_back_slope: float | None
    put_skew_25d: float | None


def _ordered_available_bars(
    bars: Iterable[DailyBar], *, cutoff_at: datetime
) -> list[DailyBar]:
    cutoff = ensure_aware(cutoff_at)
    rows = [bar for bar in bars if bar.available_at <= cutoff]
    rows.sort(key=lambda bar: bar.session_date)
    if len({bar.session_date for bar in rows}) != len(rows):
        raise ValueError("duplicate session dates are not allowed")
    return rows


def _period_return(values: Sequence[float], sessions: int) -> float | None:
    if len(values) <= sessions:
        return None
    return values[-1] / values[-1 - sessions] - 1


def _ema(values: Sequence[float], window: int) -> float | None:
    if len(values) < window:
        return None
    weight = 2 / (window + 1)
    current = fmean(values[:window])
    for value in values[window:]:
        current = value * weight + current * (1 - weight)
    return current


def _realized_vol(values: Sequence[float], window: int) -> float | None:
    if len(values) <= window:
        return None
    returns = [log(values[index] / values[index - 1]) for index in range(len(values) - window, len(values))]
    return pstdev(returns) * sqrt(TRADING_DAYS)


def _atr_percent(rows: Sequence[DailyBar], window: int) -> float | None:
    if len(rows) <= window:
        return None
    true_ranges: list[float] = []
    for index in range(len(rows) - window, len(rows)):
        current = rows[index]
        previous_close = rows[index - 1].close
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous_close),
                abs(current.low - previous_close),
            )
        )
    return fmean(true_ranges) / rows[-1].close


def build_trend_features(
    bars: Iterable[DailyBar],
    *,
    cutoff_at: datetime,
    benchmark_bars: Iterable[DailyBar] | None = None,
) -> TrendFeatures:
    rows = _ordered_available_bars(bars, cutoff_at=cutoff_at)
    if not rows:
        raise ValueError("at least one available bar is required")
    closes = [bar.close for bar in rows]
    volume_ratio = None
    if len(rows) >= 21:
        baseline = fmean(bar.volume for bar in rows[-21:-1])
        volume_ratio = rows[-1].volume / baseline if baseline > 0 else None
    relative_return = None
    if benchmark_bars is not None:
        benchmark = _ordered_available_bars(benchmark_bars, cutoff_at=cutoff_at)
        benchmark_return = _period_return([bar.close for bar in benchmark], 20)
        stock_return = _period_return(closes, 20)
        if benchmark_return is not None and stock_return is not None:
            relative_return = stock_return - benchmark_return
    return TrendFeatures(
        close=closes[-1],
        return_5d=_period_return(closes, 5),
        return_20d=_period_return(closes, 20),
        return_63d=_period_return(closes, 63),
        ema_20=_ema(closes, 20),
        ema_50=_ema(closes, 50),
        ema_200=_ema(closes, 200),
        realized_vol_20=_realized_vol(closes, 20),
        atr_14_pct=_atr_percent(rows, 14),
        volume_ratio_20=volume_ratio,
        relative_return_20d=relative_return,
        observations=len(rows),
    )

def percentile_rank(value: float, history: Sequence[float]) -> float | None:
    if not history:
        return None
    less = sum(item < value for item in history)
    equal = sum(item == value for item in history)
    return (less + 0.5 * equal) / len(history)


def build_flow_features(
    current: FlowObservation,
    *,
    historical_directional_premium: Sequence[float],
) -> FlowFeatures:
    history = [float(value) for value in historical_directional_premium]
    absolute_history = [abs(value) for value in history]
    percentile = percentile_rank(abs(current.directional_premium), absolute_history)
    zscore = None
    if len(history) >= 5:
        center = fmean(history)
        deviation = pstdev(history)
        if deviation > 0:
            zscore = (current.directional_premium - center) / deviation
    quality = current.single_leg_share * current.opening_share
    if current.qualifying_prints < 2:
        quality *= 0.65
    oi_ratio = None
    confirmed = False
    if current.next_day_oi_change is not None and current.qualifying_trade_volume > 0:
        oi_ratio = max(0.0, current.next_day_oi_change / current.qualifying_trade_volume)
        confirmed = oi_ratio >= 0.5
    return FlowFeatures(
        directional_premium=current.directional_premium,
        own_history_percentile=percentile,
        own_history_zscore=zscore,
        quality_multiplier=max(0.0, min(1.0, quality)),
        oi_confirmation_ratio=oi_ratio,
        confirmed=confirmed,
    )


def build_volatility_features(
    *,
    iv: float,
    realized_vol_20: float | None,
    historical_iv: Sequence[float],
    front_iv: float | None = None,
    back_iv: float | None = None,
    put_iv_25d: float | None = None,
    call_iv_25d: float | None = None,
) -> VolatilityFeatures:
    if iv <= 0:
        raise ValueError("iv must be positive")
    if any(value <= 0 for value in historical_iv):
        raise ValueError("historical IV values must be positive")
    term_slope = None if front_iv is None or back_iv is None else back_iv - front_iv
    skew = None if put_iv_25d is None or call_iv_25d is None else put_iv_25d - call_iv_25d
    return VolatilityFeatures(
        iv=iv,
        realized_vol_20=realized_vol_20,
        iv_rv_gap=None if realized_vol_20 is None else iv - realized_vol_20,
        iv_percentile_90d=percentile_rank(iv, historical_iv[-90:]),
        front_back_slope=term_slope,
        put_skew_25d=skew,
    )
