from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path

from morning_edge.evaluation import build_report, discover_runs, evaluate_registered, register_run
from morning_edge.models import Dataset, SnapshotEnvelope
from morning_edge.store import SnapshotStore


UTC = timezone.utc


def run(*, run_id: str, cutoff: str, price_date: str, price: float, source_id: int, option_bid: float) -> dict:
    return {
        "run_id": run_id,
        "cutoff_at": cutoff,
        "generated_at": cutoff,
        "watchlist": [{
            "ticker": "QCOM",
            "action": "NO_RECOMMENDATION",
            "price": {"as_of": price_date, "value": price},
            "provenance": {"snapshot_ids": [source_id]},
            "technical": {
                "bars": [{"date": price_date, "close": price}],
                "return_5d": 0.03,
                "return_20d": -0.02,
            },
            "edge": {
                "feature_version": "edge-test-v1",
                "dimensions": {"directional_edge": 60},
                "forecast": {
                    "model_version": "analog-test-v1",
                    "status": "EXPERIMENTAL_UNCALIBRATED",
                    "directional_analog_frequency": 0.6,
                    "path": [
                        {"session": 1, "date": "2026-08-24", "center_return": 0.02, "low_return": -0.03, "high_return": 0.06},
                        {"session": 5, "date": "2026-08-28", "center_return": 0.04, "low_return": -0.08, "high_return": 0.12},
                        {"session": 10, "date": "2026-09-04", "center_return": 0.05, "low_return": -0.12, "high_return": 0.18},
                        {"session": 20, "date": "2026-09-18", "center_return": 0.08, "low_return": -0.20, "high_return": 0.30},
                    ],
                },
            },
            "trade_thesis": {
                "direction": "BULLISH", "conviction_score": 70,
                "trigger_reference": "acceptance above level", "invalidation_reference": "close below level",
                "option_reference": {
                    "contract": "QCOM261016C00170000", "type": "CALL", "strike": 170,
                    "expiry": "2026-10-16", "ask": 5.0, "bid": 4.8,
                },
            },
            "options": {"candidates": [{"contract": "QCOM261016C00170000", "bid": option_bid, "ask": option_bid + .2}]},
        }],
    }


class EvaluationHarnessTests(unittest.TestCase):
    def test_discovery_accepts_one_date_stamped_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "2026-08-25"
            older = root / "2026-08-24"
            current.mkdir()
            older.mkdir()
            current_run = current / "morning-run-enriched.json"
            current_run.write_text("{}\n", encoding="utf-8")
            (older / "morning-run-enriched.json").write_text("{}\n", encoding="utf-8")

            self.assertEqual([current_run], discover_runs(current))
            self.assertEqual(2, len(discover_runs(root)))

    def test_registration_is_idempotent_and_scoring_is_walk_forward(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "edge.sqlite"
            with SnapshotStore(database) as store:
                source = store.insert(SnapshotEnvelope(
                    provider="test", dataset=Dataset.OHLC, symbol="QCOM",
                    as_of=datetime(2026, 8, 21, 20, tzinfo=UTC),
                    retrieved_at=datetime(2026, 8, 24, 11, tzinfo=UTC), payload={"data": []},
                ))
            origin = run(run_id="origin", cutoff="2026-08-24T12:00:00Z", price_date="2026-08-21", price=100, source_id=source.id, option_bid=4.8)
            future = run(run_id="future", cutoff="2026-08-25T12:00:00Z", price_date="2026-08-24", price=103, source_id=source.id, option_bid=6.0)
            first = register_run(database, origin)
            second = register_run(database, origin)
            self.assertEqual(4, first["unique_forecasts"])
            self.assertEqual(4, second["unique_forecasts"])

            scored = evaluate_registered(database, [origin, future])
            self.assertEqual(1, scored["evaluated"])
            self.assertEqual(3, scored["pending"])
            report = build_report(database)
            one_day = report["horizons"]["1"]
            self.assertEqual(1, one_day["evaluated"])
            self.assertEqual(1.0, one_day["direction_accuracy"])
            self.assertAlmostEqual(20.0, one_day["median_option_return_pct"])
            self.assertEqual(1, one_day["prospective_evaluated"])
            self.assertEqual(1.0, one_day["prospective_direction_accuracy"])
            self.assertEqual(1, one_day["distinct_origin_sessions"])
            self.assertEqual(1, one_day["origin_dependence"]["distinct_origin_sessions"])
            self.assertEqual(1, one_day["independent_origin_metrics"]["origin_sessions"])
            self.assertEqual(1.0, one_day["independent_origin_metrics"]["equal_weight_accuracy"])
            self.assertEqual(1.0, one_day["prospective_classification"]["bullish_recall"])
            self.assertEqual(1.0, one_day["baselines"]["always_bullish"]["accuracy"])
            self.assertEqual(1.0, one_day["baselines"]["five_session_momentum"]["accuracy"])
            self.assertEqual(0.0, one_day["baselines"]["twenty_session_momentum"]["accuracy"])
            self.assertAlmostEqual(9.0, one_day["median_interval_score"])
            self.assertIsNotNone(one_day["direction_accuracy_wilson_95"])
            self.assertFalse(one_day["sample_gate_passed"])
            self.assertFalse(report["calibrated"])
            self.assertEqual("BLOCKED_UNCALIBRATED_INPUT", report["probability_scoring"]["status"])
            self.assertEqual(4, report["direction_breakdown"]["BULLISH"]["registered"])
            self.assertEqual(1.0, report["direction_breakdown"]["BULLISH"]["direction_accuracy"])
            self.assertEqual(4, report["ticker_breakdown"]["QCOM"]["registered"])
            self.assertEqual(4, report["conviction_breakdown"]["65–79"]["registered"])

            again = evaluate_registered(database, [origin, future])
            self.assertEqual(0, again["evaluated"])

    def test_registration_mode_change_does_not_duplicate_one_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "edge.sqlite"
            with SnapshotStore(database) as store:
                source = store.insert(SnapshotEnvelope(
                    provider="test", dataset=Dataset.OHLC, symbol="QCOM",
                    as_of=datetime(2026, 8, 21, 20, tzinfo=UTC),
                    retrieved_at=datetime(2026, 8, 24, 11, tzinfo=UTC), payload={"data": []},
                ))
            origin = run(
                run_id="origin", cutoff="2026-08-24T12:00:00Z",
                price_date="2026-08-21", price=100, source_id=source.id, option_bid=4.8,
            )

            prospective = register_run(database, origin, registration_mode="PROSPECTIVE")
            replayed = register_run(database, origin, registration_mode="RETROSPECTIVE_ARTIFACT_SEED")
            report = build_report(database)

            self.assertEqual(4, prospective["unique_forecasts"])
            self.assertEqual(4, replayed["unique_forecasts"])
            self.assertEqual(4, report["ledger_forecasts"])
            self.assertEqual(4, report["registered_forecasts"])
            self.assertTrue(all(row["registration_mode"] == "PROSPECTIVE" for row in report["rows"]))

    def test_missing_later_contract_is_not_scored_as_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "edge.sqlite"
            with SnapshotStore(database) as store:
                source = store.insert(SnapshotEnvelope(
                    provider="test", dataset=Dataset.OHLC, symbol="QCOM",
                    as_of=datetime(2026, 8, 21, 20, tzinfo=UTC),
                    retrieved_at=datetime(2026, 8, 24, 11, tzinfo=UTC), payload={"data": []},
                ))
            origin = run(run_id="origin", cutoff="2026-08-24T12:00:00Z", price_date="2026-08-21", price=100, source_id=source.id, option_bid=4.8)
            future = run(run_id="future", cutoff="2026-08-25T12:00:00Z", price_date="2026-08-24", price=97, source_id=source.id, option_bid=6.0)
            future["watchlist"][0]["options"]["candidates"] = []
            register_run(database, origin)
            evaluate_registered(database, [origin, future])
            report = build_report(database)
            self.assertEqual(0, report["horizons"]["1"]["option_evaluated"])
            self.assertIsNone(report["rows"][0]["option_return_pct"])

    def test_direction_only_thesis_is_registered_without_numeric_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "edge.sqlite"
            with SnapshotStore(database) as store:
                source = store.insert(SnapshotEnvelope(
                    provider="test", dataset=Dataset.OHLC, symbol="QCOM",
                    as_of=datetime(2026, 8, 21, 20, tzinfo=UTC),
                    retrieved_at=datetime(2026, 8, 24, 11, tzinfo=UTC), payload={"data": []},
                ))
            origin = run(run_id="origin", cutoff="2026-08-24T12:00:00Z", price_date="2026-08-21", price=100, source_id=source.id, option_bid=4.8)
            origin["watchlist"][0]["edge"]["forecast"]["path"] = []
            origin["watchlist"][0]["edge"]["forecast"]["status"] = "INSUFFICIENT_HISTORY"
            result = register_run(database, origin)
            self.assertEqual(4, result["unique_forecasts"])
            report = build_report(database)
            self.assertEqual(4, report["registered_forecasts"])
            self.assertEqual(4, report["direction_breakdown"]["BULLISH"]["pending"])
            self.assertTrue(all(row["conviction_score"] == 70 for row in report["rows"]))

    def test_shadow_v4_is_tracked_without_changing_active_thesis_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "edge.sqlite"
            with SnapshotStore(database) as store:
                source = store.insert(SnapshotEnvelope(
                    provider="test", dataset=Dataset.OHLC, symbol="QCOM",
                    as_of=datetime(2026, 8, 21, 20, tzinfo=UTC),
                    retrieved_at=datetime(2026, 8, 24, 11, tzinfo=UTC), payload={"data": []},
                ))
            origin = run(run_id="origin", cutoff="2026-08-24T12:00:00Z", price_date="2026-08-21", price=100, source_id=source.id, option_bid=4.8)
            origin["watchlist"][0]["edge"]["forecast_v4"] = {
                "model_version": "volatility-scaled-analog-ensemble-v4",
                "status": "SHADOW_EXPERIMENTAL_UNCALIBRATED",
                "direction": "BEARISH",
                "directional_analog_frequency": .55,
                "path": [
                    {"session": horizon, "date": "2026-09-18", "center_return": -.01 * horizon,
                     "low_return": -.03 * horizon, "high_return": .02 * horizon}
                    for horizon in (1, 5, 10, 20)
                ],
            }
            result = register_run(database, origin)
            report = build_report(database)
            self.assertEqual(8, result["unique_forecasts"])
            self.assertEqual(4, report["registered_forecasts"])
            self.assertEqual(8, report["tracked_model_forecasts"])
            self.assertEqual(4, report["model_breakdown"]["analog-test-v1"]["registered"])
            self.assertEqual(4, report["model_breakdown"]["volatility-scaled-analog-ensemble-v4"]["registered"])
            shadow = [row for row in report["rows"] if row["model_role"] == "SHADOW_V4"]
            self.assertEqual(4, len(shadow))
            self.assertTrue(all(not row["option_available"] for row in shadow))


if __name__ == "__main__":
    unittest.main()
