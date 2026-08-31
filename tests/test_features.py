from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from morning_edge.features import (
    DailyBar,
    FlowObservation,
    build_flow_features,
    build_trend_features,
    build_volatility_features,
)


ET = ZoneInfo("America/New_York")


def make_bars(count: int, *, offset: float = 0.0) -> list[DailyBar]:
    start = date(2025, 10, 1)
    rows: list[DailyBar] = []
    for index in range(count):
        close = 100 + offset + index * 0.2
        rows.append(
            DailyBar(
                session_date=start + timedelta(days=index),
                open=close - 0.2,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=1_000_000 + index * 1_000,
                available_at=datetime.combine(start + timedelta(days=index + 1), datetime.min.time(), ET),
            )
        )
    return rows


class FeatureTests(unittest.TestCase):
    def test_trend_features_use_only_available_bars(self) -> None:
        bars = make_bars(70)
        cutoff = bars[-2].available_at
        features = build_trend_features(bars, cutoff_at=cutoff)
        self.assertEqual(features.observations, 69)
        self.assertIsNotNone(features.return_63d)
        self.assertIsNotNone(features.realized_vol_20)
        self.assertGreater(features.close, 100)

    def test_duplicate_sessions_are_rejected(self) -> None:
        bars = make_bars(25)
        cutoff = bars[-1].available_at
        with self.assertRaisesRegex(ValueError, "duplicate"):
            build_trend_features([*bars, bars[-1]], cutoff_at=cutoff)

    def test_flow_confirmation_requires_next_day_oi(self) -> None:
        provisional = build_flow_features(
            FlowObservation(2_000_000, 2_500_000, 3, 0.9, 0.8, None, 1_000),
            historical_directional_premium=[-100_000, 120_000, 80_000, 50_000, 90_000],
        )
        confirmed = build_flow_features(
            FlowObservation(2_000_000, 2_500_000, 3, 0.9, 0.8, 600, 1_000),
            historical_directional_premium=[-100_000, 120_000, 80_000, 50_000, 90_000],
        )
        self.assertFalse(provisional.confirmed)
        self.assertTrue(confirmed.confirmed)
        self.assertGreater(confirmed.own_history_percentile or 0, 0.9)

    def test_volatility_keeps_direction_separate(self) -> None:
        features = build_volatility_features(
            iv=0.50,
            realized_vol_20=0.38,
            historical_iv=[0.30, 0.35, 0.42, 0.55],
            front_iv=0.54,
            back_iv=0.48,
            put_iv_25d=0.57,
            call_iv_25d=0.49,
        )
        self.assertAlmostEqual(features.iv_rv_gap or 0, 0.12)
        self.assertAlmostEqual(features.front_back_slope or 0, -0.06)
        self.assertAlmostEqual(features.put_skew_25d or 0, 0.08)


if __name__ == "__main__":
    unittest.main()
