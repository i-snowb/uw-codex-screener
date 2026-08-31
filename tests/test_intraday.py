from __future__ import annotations

from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from morning_edge.intraday import (
    IntradayTier,
    due_tiers,
    full_session_logical_request_estimate,
    intraday_condition,
    market_session,
    merge_frozen_daily_model,
)


ET = ZoneInfo("America/New_York")


class IntradayPolicyTests(unittest.TestCase):
    def test_regular_holiday_and_early_close_sessions(self) -> None:
        regular = market_session(datetime(2026, 8, 28, 10, tzinfo=ET))
        holiday = market_session(datetime(2026, 9, 7, 10, tzinfo=ET))
        early = market_session(datetime(2026, 11, 27, 12, tzinfo=ET))
        self.assertEqual("REGULAR", regular.status)
        self.assertTrue(regular.is_open_at(datetime(2026, 8, 28, 15, 59, tzinfo=ET)))
        self.assertEqual("CLOSED", holiday.status)
        self.assertFalse(holiday.is_open_at(datetime(2026, 9, 7, 10, tzinfo=ET)))
        self.assertEqual(13, early.closes_at.hour)
        self.assertFalse(early.is_open_at(datetime(2026, 11, 27, 13, tzinfo=ET)))

    def test_due_tiers_respect_independent_intervals(self) -> None:
        now = datetime(2026, 8, 28, 10, 30, tzinfo=ET)
        completed = {
            "fast": "2026-08-28T10:26:00-04:00",
            "medium": "2026-08-28T10:14:00-04:00",
            "slow": "2026-08-28T10:01:00-04:00",
        }
        self.assertEqual((IntradayTier.MEDIUM,), due_tiers(now, completed))

    def test_full_day_request_estimate_fits_basic_plan(self) -> None:
        self.assertEqual(8112, full_session_logical_request_estimate(14))
        self.assertLess(8112 * 3, 35_000)

    def test_condition_uses_price_and_persistent_greek_flow_only(self) -> None:
        baseline = {
            "price": {"value": 100},
            "technical": {"ema20": 98},
            "trade_thesis": {"direction": "BULLISH"},
        }
        enhanced = {
            "stock_state": {"price": 102},
            "greek_flow": {"directional_delta_flow": 25000, "delta_sign_persistence": 0.7},
            "greek_exposure": {"near_spot_regime": "positive"},
        }
        result = intraday_condition(
            baseline,
            enhanced,
            observed_at=datetime(2026, 8, 28, 10, 30, tzinfo=ET),
        )
        self.assertEqual("CONFIRMING", result["status"])
        self.assertEqual(2, result["directional_votes"])
        self.assertAlmostEqual(0.02, result["change_from_daily_anchor"])
        self.assertNotIn("GEX", " ".join(result["drivers"]))

    def test_merge_preserves_daily_forecast_and_agent_origin(self) -> None:
        baseline = {
            "watchlist": [{
                "ticker": "QCOM",
                "trade_rank": 1,
                "price": {"value": 100},
                "technical": {"ema20": 99},
                "trade_thesis": {"direction": "BEARISH"},
                "agent_enrichment": {"summary": "frozen"},
                "edge": {"forecast_v4": {"center_return_20d": -0.1}, "historical_analogs": {"sample_size": 7}},
            }],
        }
        live = {
            "watchlist": [{
                "ticker": "QCOM",
                "trade_rank": 9,
                "price": {"value": 100},
                "technical": {"ema20": 99},
                "trade_thesis": {"direction": "BULLISH"},
                "agent_enrichment": {"summary": "new"},
                "edge": {"forecast_v4": {"center_return_20d": 0.2}, "historical_analogs": {"sample_size": 20}},
            }],
        }
        enhanced = {"symbols": {"QCOM": {
            "stock_state": {"price": 97},
            "greek_flow": {"directional_delta_flow": -100, "delta_sign_persistence": 0.8},
        }}}
        merge_frozen_daily_model(
            live,
            baseline,
            enhanced,
            observed_at=datetime(2026, 8, 28, 10, 30, tzinfo=ET),
        )
        row = live["watchlist"][0]
        self.assertEqual(1, row["trade_rank"])
        self.assertEqual("frozen", row["agent_enrichment"]["summary"])
        self.assertEqual(-0.1, row["edge"]["forecast_v4"]["center_return_20d"])
        self.assertEqual("CONFIRMING", row["intraday_condition"]["status"])


if __name__ == "__main__":
    unittest.main()
