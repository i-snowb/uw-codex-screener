from __future__ import annotations

import unittest

from morning_edge.enhanced_features import (
    summarize_dark_pool,
    summarize_greek_exposure,
    summarize_greek_flow,
    summarize_volatility,
)


class EnhancedFeatureTests(unittest.TestCase):
    def test_greek_exposure_keeps_positive_and_negative_topology_separate(self) -> None:
        result = summarize_greek_exposure([
            {"date": "2026-08-24", "strike": "95", "call_gex": "30", "put_gex": "-10", "call_vanna": "2", "put_vanna": "-1", "call_charm": "4", "put_charm": "-2"},
            {"date": "2026-08-24", "strike": "100", "call_gex": "5", "put_gex": "-45", "call_vanna": "1", "put_vanna": "-3", "call_charm": "2", "put_charm": "-5"},
            {"date": "2026-08-24", "strike": "110", "call_gex": "60", "put_gex": "-5", "call_vanna": "4", "put_vanna": "-1", "call_charm": "5", "put_charm": "-1"},
        ], spot=100.0)

        self.assertEqual(110.0, result["strongest_positive_gex_strike"])
        self.assertEqual(100.0, result["strongest_negative_gex_strike"])
        self.assertEqual("negative", result["near_spot_regime"])
        self.assertIn("not verified dealer inventory", result["caveat"])

    def test_greek_flow_reports_persistence_and_late_change_without_probability(self) -> None:
        rows = [
            {"timestamp": f"2026-08-24T13:3{i}:00Z", "dir_delta_flow": str(value), "dir_vega_flow": "5", "otm_dir_delta_flow": str(value / 2)}
            for i, value in enumerate((1, 2, -1, -4, -8))
        ]
        result = summarize_greek_flow(rows)

        self.assertEqual(-8.0, result["directional_delta_flow"])
        self.assertEqual(0.6, result["delta_sign_persistence"])
        self.assertNotIn("probability", result)

    def test_volatility_and_dark_pool_are_descriptive_not_directional(self) -> None:
        volatility = summarize_volatility(
            [
                {"dte": 30, "volatility": "0.40", "implied_move_perc": "0.08"},
                {"dte": 60, "volatility": "0.35", "implied_move_perc": "0.11"},
            ],
            [{"iv": "0.42", "rv": "0.30", "iv_rank": "55"}],
            [{"days": 30, "implied_move_perc": "0.09", "percentile": "0.70"}],
        )
        dark = summarize_dark_pool([
            {"price": "99", "dark_pool_volume": "100", "regular_volume": "50"},
            {"price": "101", "dark_pool_volume": "300", "regular_volume": "100"},
        ], spot=100.0)

        self.assertAlmostEqual(-0.05, volatility["term_slope_30d_to_60d"])
        self.assertAlmostEqual(0.12, volatility["iv_minus_rv"])
        self.assertEqual(101.0, dark["dominant_price"])
        self.assertAlmostEqual(0.01, dark["dominant_distance_pct"])


if __name__ == "__main__":
    unittest.main()
