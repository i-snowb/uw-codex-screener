from __future__ import annotations

from datetime import datetime, timezone
import unittest

from morning_edge.edge import option_mechanics
from morning_edge.option_research import shadow_option_research


class OptionResearchTests(unittest.TestCase):
    def test_scenario_rows_remain_non_executable(self) -> None:
        cutoff = datetime(2026, 8, 28, 11, tzinfo=timezone.utc)
        contract = {
            "contract": "QCOM261016C00170000", "option_type": "CALL", "expiry": "2026-10-16",
            "dte": 49, "strike": 170.0, "bid": 4.8, "ask": 5.0,
            "spread_pct_of_mid": 0.04, "delta": 0.46, "open_interest": 1800,
            "implied_volatility": 0.42, "quote_fresh": False,
        }
        contract["mechanics"] = option_mechanics(contract, spot=165.0, cutoff_at=cutoff)
        result = shadow_option_research(
            direction="BULLISH", spot=165.0, cutoff_at=cutoff, contracts=[contract],
            forecast_v4={"p10_return_20d": -0.12, "center_return_20d": 0.04, "p90_return_20d": 0.18},
        )
        self.assertEqual("SHADOW_ONLY", result["status"])
        self.assertFalse(result["promotion_eligible"])
        self.assertEqual("NOT_ELIGIBLE", result["rows"][0]["status"])
        self.assertFalse(result["rows"][0]["fit_score_is_probability"])
        self.assertEqual({"p10", "center", "p90"}, set(result["rows"][0]["scenarios"]))


if __name__ == "__main__":
    unittest.main()
