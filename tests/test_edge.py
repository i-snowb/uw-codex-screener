from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from morning_edge.edge import EdgeAnalyzer, option_mechanics
from morning_edge.models import Dataset, SnapshotEnvelope
from morning_edge.store import SnapshotStore


CUTOFF = datetime(2026, 8, 24, 12, 30, tzinfo=UTC)


class EdgeAnalyzerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "edge.sqlite"
        self.store = SnapshotStore(self.database)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def insert(self, dataset: Dataset, payload: object, market_date: date, *, symbol: str = "QCOM") -> int:
        observed = datetime.combine(market_date, datetime.min.time(), UTC)
        return self.store.insert(SnapshotEnvelope(
            provider="test", dataset=dataset, symbol=symbol, as_of=observed,
            retrieved_at=min(CUTOFF, observed + timedelta(hours=1)), payload={"data": payload},
            metadata={"requested_market_date": market_date.isoformat()},
        )).id

    @staticmethod
    def chain(day: date, spot: float, call_oi: int, put_oi: int) -> list[dict[str, object]]:
        expiry = (day + timedelta(days=60)).isoformat()
        return [
            {"option_symbol": f"QCOM{day:%y%m%d}C", "option_type": "call", "expires": expiry,
             "strike": spot, "implied_volatility": .42, "delta": .51, "gamma": .02,
             "theta": -.03, "vega": .2, "nbbo_bid": 4, "nbbo_ask": 4.2,
             "open_interest": call_oi, "volume": 40},
            {"option_symbol": f"QCOM{day:%y%m%d}P", "option_type": "put", "expires": expiry,
             "strike": spot, "implied_volatility": .46, "delta": -.49, "gamma": .02,
             "theta": -.03, "vega": .2, "nbbo_bid": 4.1, "nbbo_ask": 4.3,
             "open_interest": put_oi, "volume": 35},
        ]

    def test_derives_rich_cutoff_safe_sections(self) -> None:
        start = date(2026, 5, 1)
        bars = []
        chain_days = []
        for index in range(80):
            day = start + timedelta(days=index)
            close = 100 + index * .4
            bars.append({"date": day.isoformat(), "market_time": "r", "open": close - .2,
                         "high": close + 1, "low": close - 1, "close": close, "volume": 1_000_000})
            if index >= 20 and index % 3 == 0:
                chain_days.append(day)
                self.insert(Dataset.OPTION_CHAIN, self.chain(day, close, 1000 + index, 900 + index), day)
                self.insert(Dataset.DEALER_EXPOSURE, {"call_wall": close + 10, "put_wall": close - 10,
                    "gamma_flip": close - 1, "gamma_magnet": close + 1, "nearby_flips": [close - 2]}, day)
        self.insert(Dataset.OHLC, bars, chain_days[-1])
        latest = chain_days[-1]
        self.insert(Dataset.OPTION_FLOW, [{"type": "call", "total_premium": 1_000_000,
            "total_ask_side_prem": 800_000, "total_bid_side_prem": 100_000,
            "all_opening_trades": True, "has_singleleg": True, "has_multileg": False,
            "has_sweep": True, "iv_start": .40, "iv_end": .43, "volume": 1000,
            "volume_oi_ratio": 1.2}], latest)
        self.insert(Dataset.DARK_POOL, [{"tracking_id": "a", "price": 130, "premium": 2_000_000,
            "nbbo_bid": 129.99, "nbbo_ask": 130.01}], latest)
        self.insert(Dataset.EARNINGS, [{"report_date": "2026-04-01", "expected_move_perc": .05,
            "post_earnings_move_1d": .07, "long_straddle_1d": .10}], latest)
        self.insert(Dataset.NEWS, [{"headline": "Product", "sentiment": "positive", "is_major": True,
            "source": "wire", "tags": ["product"]}], latest)

        technical = {
            "latest_regular_close": bars[-1]["close"], "realized_vol_20": .30,
            "return_5d": .02, "return_20d": .08, "return_63d": .20,
            "ema_20": 128, "ema_50": 122, "ema_200": None,
            "return_1d_pct": .3,
            "bars": [{"date": item["date"], "close": item["close"]} for item in bars],
        }
        with EdgeAnalyzer(self.database) as analyzer:
            result = analyzer.analyze("QCOM", CUTOFF, technical=technical)

        self.assertEqual("RESEARCH_ONLY", result["option_surface"]["status"])
        self.assertGreater(result["option_surface"]["history_sessions"], 8)
        self.assertEqual("DERIVED_FROM_CONSECUTIVE_CHAINS", result["open_interest"]["status"])
        self.assertGreater(result["flow_conviction"]["directional_premium"], 0)
        self.assertEqual("ABOVE_FLIP", result["gex_topology"]["spot_regime"])
        self.assertAlmostEqual(130, result["dark_pool"]["dominant_price_level"], delta=.2)
        self.assertEqual(1, result["news_signal"]["major_count"])
        self.assertFalse(result["calibration"]["ready"])
        self.assertFalse(result["dimensions"]["calibrated_probability_available"])
        json.dumps(result)

    def test_premarket_flow_uses_provider_row_date_not_retrieval_date(self) -> None:
        cutoff = datetime(2026, 8, 25, 11, 35, tzinfo=UTC)
        captured_at = cutoff - timedelta(minutes=1)
        self.store.insert(SnapshotEnvelope(
            provider="test", dataset=Dataset.OPTION_FLOW, symbol="QCOM",
            as_of=captured_at, retrieved_at=captured_at,
            payload={"data": [{
                "created_at": "2026-08-24T19:55:00Z",
                "type": "call", "total_premium": 1_000_000,
                "total_ask_side_prem": 800_000, "total_bid_side_prem": 100_000,
                "all_opening_trades": True, "has_singleleg": True,
                "has_multileg": False, "volume": 100,
            }]},
            metadata={"requested_market_date": "2026-08-25"},
        ))

        with EdgeAnalyzer(self.database) as analyzer:
            result = analyzer.flow_conviction("QCOM", cutoff)

        self.assertEqual("2026-08-24", result["market_date"])
        self.assertEqual(1, result["alert_count"])
        self.assertGreater(result["directional_premium"], 0)

    def test_gex_level_changes_do_not_cross_provider_method_boundary(self) -> None:
        self.insert(Dataset.DEALER_EXPOSURE, {
            "call_wall": 180, "put_wall": 150, "gamma_flip": 165, "gamma_magnet": 170,
        }, date(2026, 8, 21))
        self.insert(Dataset.DEALER_EXPOSURE, {
            "call_wall": 181, "put_wall": 151, "gamma_flip": 166, "gamma_magnet": 171,
            "nearby_flips": [166, 173], "source": "vol", "time": "2026-08-23T19:59:00Z",
        }, date(2026, 8, 23))
        self.insert(Dataset.DEALER_EXPOSURE, {
            "call_wall": 183, "put_wall": 152, "gamma_flip": 167, "gamma_magnet": 172,
            "nearby_flips": [167, 174], "source": "vol", "time": "2026-08-24T19:59:00Z",
        }, date(2026, 8, 24))

        with EdgeAnalyzer(self.database) as analyzer:
            result = analyzer.gex_topology("QCOM", CUTOFF, spot=168)

        self.assertEqual("spot_directionalized_volume_v2", result["method_version"])
        self.assertEqual(3, result["history_sessions"])
        self.assertEqual(2, result["comparable_history_sessions"])
        self.assertEqual("COMPARABLE", result["comparison_status"])
        self.assertEqual(1, result["flip_change"])
        self.assertEqual(2, result["call_wall_change"])
        self.assertEqual([167, 174], result["nearby_flips"])

    def test_option_mechanics_labels_risk_neutral_probability(self) -> None:
        result = option_mechanics({"option_type": "call", "strike": 105, "ask": 4,
            "implied_volatility": .40, "expiry": "2026-12-18"}, spot=100, cutoff_at=CUTOFF)
        self.assertEqual("MODEL_REFERENCE_ONLY", result["status"])
        self.assertFalse(result["physical_probability_available"])
        self.assertEqual(109, result["breakeven"])
        self.assertEqual(7, len(result["matrix"]))
        self.assertEqual(5, len(result["matrix"][0]["points"]))

    def test_forecast_is_explicitly_uncalibrated_and_has_empirical_band(self) -> None:
        bars = [{"date": (date(2026, 1, 2) + timedelta(days=index)).isoformat(), "close": 100 + index}
                for index in range(40)]
        analogs = {
            "status": "DESCRIPTIVE_NOT_CALIBRATED", "sample_size": 8,
            "horizons": {
                "1": {"sample_size": 8, "median_return": .002, "p10_return": -.01, "p90_return": .012, "up_rate": .625},
                "5": {"sample_size": 8, "median_return": .01, "p10_return": -.035, "p90_return": .05, "up_rate": .625},
                "20": {"sample_size": 8, "median_return": .04, "p10_return": -.08, "p90_return": .15, "up_rate": .625},
            },
        }
        result = EdgeAnalyzer.forecast_distribution(
            technical={"latest_regular_close": 139, "return_5d": .03, "return_20d": .08, "bars": bars},
            analogs=analogs,
        )
        self.assertEqual("EXPERIMENTAL_UNCALIBRATED", result["status"])
        self.assertFalse(result["calibrated"])
        self.assertEqual(20, len(result["path"]))
        self.assertLess(result["low_return_20d"], result["center_return_20d"])
        self.assertGreater(result["high_return_20d"], result["center_return_20d"])

    def test_v4_scales_analog_paths_and_exposes_one_week_quantiles(self) -> None:
        bars = [{"date": (date(2026, 1, 2) + timedelta(days=index)).isoformat(), "close": 100 + index}
                for index in range(40)]
        analogs = {
            "status": "DESCRIPTIVE_NOT_CALIBRATED", "sample_size": 7,
            "horizons": {"20": {"up_rate": 4 / 7}},
            "analogs": [
                {
                    "state_realized_vol_20": .18 + index * .02,
                    "forward_path": [((session + 1) / 20) * (-.08 + index * .03) for session in range(20)],
                }
                for index in range(7)
            ],
        }
        result = EdgeAnalyzer.forecast_distribution_v4(
            technical={"latest_regular_close": 139, "realized_vol_20": .42,
                       "return_5d": .03, "return_20d": .08, "bars": bars},
            analogs=analogs,
            surface={"front_iv": .50},
        )
        self.assertEqual("SHADOW_EXPERIMENTAL_UNCALIBRATED", result["status"])
        self.assertFalse(result["promotion_eligible"])
        self.assertEqual(20, len(result["path"]))
        self.assertEqual(result["path"][4]["center_return"], result["center_return_5d"])
        self.assertLessEqual(result["p10_return_5d"], result["p25_return_5d"])
        self.assertLessEqual(result["p25_return_5d"], result["p50_return_5d"])
        self.assertLessEqual(result["p50_return_5d"], result["p75_return_5d"])
        self.assertLessEqual(result["p75_return_5d"], result["p90_return_5d"])
        self.assertGreater(result["volatility_scaling"]["target_annualized_volatility"], .42)

    def test_analogs_include_full_path_excursions_and_base_rate(self) -> None:
        start = date(2025, 8, 1)
        bars = []
        for index in range(280):
            cycle = ((index % 40) - 20) * 0.18
            close = 100 + index * 0.09 + cycle
            bars.append({"date": (start + timedelta(days=index)).isoformat(), "close": close})
        with EdgeAnalyzer(self.database) as analyzer:
            result = analyzer.historical_analogs(bars=bars, surface={}, gex={})
        self.assertEqual("DESCRIPTIVE_NOT_CALIBRATED", result["status"])
        self.assertEqual(20, len(result["path_distribution"]))
        self.assertEqual(result["sample_size"], result["excursion_summary"]["sample_size"])
        self.assertGreater(result["baseline_comparison"]["sample_size"], 0)
        self.assertIn(result["historical_disposition"], {"BULLISH", "BEARISH", "MIXED"})
        self.assertEqual(20, len(result["analogs"][0]["forward_path"]))
        stability = result["stability"]
        self.assertEqual(result["sample_size"], stability["leave_one_out_runs"])
        self.assertGreater(stability["effective_sample_size"], 0)
        self.assertLessEqual(stability["effective_sample_size"], result["sample_size"])
        self.assertGreater(stability["maximum_distance_weight_share"], 0)
        self.assertLessEqual(stability["maximum_distance_weight_share"], 1)
        self.assertIn(stability["status"], {"STABLE", "SENSITIVE", "INSUFFICIENT_SAMPLE"})

    def test_analogs_use_long_price_history_and_gate_short_derivative_history(self) -> None:
        start = date(2024, 1, 2)
        bars = []
        for index in range(620):
            cycle = ((index % 45) - 22) * 0.22
            close = 100 + index * 0.04 + cycle
            bars.append({"date": (start + timedelta(days=index)).isoformat(), "close": close})
        recent_dates = [row["date"] for row in bars[-70:]]
        surface = {
            "front_iv": 0.34,
            "history": [
                {"date": day, "front_iv": 0.30 + (index % 7) * 0.01}
                for index, day in enumerate(recent_dates)
            ],
        }
        gex = {
            "gamma_flip": 120,
            "history": [
                {"date": day, "gamma_flip": 112 + (index % 9)}
                for index, day in enumerate(recent_dates)
            ],
        }
        with EdgeAnalyzer(self.database) as analyzer:
            result = analyzer.historical_analogs(bars=bars, surface=surface, gex=gex)
        self.assertEqual("DESCRIPTIVE_NOT_CALIBRATED", result["status"])
        self.assertEqual(504, result["lookback_sessions"])
        quality = result["match_quality"]
        self.assertIn("realized_vol_20", quality["current_features"])
        self.assertIn("drawdown_63d", quality["current_features"])
        self.assertIn("front_iv", quality["all_current_features"])
        self.assertNotIn("front_iv", quality["current_features"])
        self.assertEqual("STAGED_CONTEXT_ONLY", quality["derivative_match"]["status"])
        self.assertLess(quality["derivative_match"]["independent_candidate_count"], 5)


if __name__ == "__main__":
    unittest.main()
