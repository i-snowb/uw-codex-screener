from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import stat
import tempfile
import unittest

from morning_edge.current_collection import CurrentCaptureItem, CurrentCaptureReport, CurrentCaptureStatus, CurrentDataset
from morning_edge.daily import MORNING_RUN_SCHEMA_VERSION, _trade_thesis, build_morning_run, write_morning_run
from morning_edge.models import Dataset, SnapshotEnvelope, timestamp_text
from morning_edge.store import SnapshotStore


NOW = datetime(2026, 8, 24, 10, 45, tzinfo=UTC)


def report(snapshot_ids: dict[CurrentDataset, int], *, generated_at: datetime = NOW, fetched_at: datetime = NOW) -> CurrentCaptureReport:
    return CurrentCaptureReport(
        generated_at=generated_at.isoformat(), tickers=("QCOM",), datasets=tuple(snapshot_ids), preflight_passed=True,
        max_transport_attempts=8, remaining_transport_attempt_capacity_before_run=90,
        results=tuple(CurrentCaptureItem("QCOM", dataset, CurrentCaptureStatus.CAPTURED, "/test", value, fetched_at.isoformat(), 1) for dataset, value in snapshot_ids.items()),
    )


class DailyArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "edge.sqlite"
        self.store = SnapshotStore(self.database)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def _insert(self, dataset: Dataset, payload: object, *, as_of: datetime | None = None, retrieved_at: datetime = NOW) -> int:
        return self.store.insert(SnapshotEnvelope(
            provider="test", dataset=dataset, symbol="QCOM", as_of=as_of or NOW - timedelta(minutes=1),
            retrieved_at=retrieved_at, payload={"data": payload}, metadata={"requested_market_date": "2026-08-24"},
        )).id

    def test_builds_grounded_shadow_artifact_with_reference_contracts(self) -> None:
        ohlc = self._insert(Dataset.OHLC, [
            {"date": "2026-08-20", "market_time": "r", "open": 100, "high": 103, "low": 99, "close": 102, "volume": 1000},
            {"date": "2026-08-21", "market_time": "r", "open": 102, "high": 105, "low": 101, "close": 104, "volume": 1100},
        ])
        chain = self._insert(Dataset.OPTION_CHAIN, [
            {"option_symbol": "QCOM261218C00105000", "option_type": "call", "expiry": "2026-12-18", "strike": 105, "nbbo_bid": 4.0, "nbbo_ask": 4.2, "delta": .5, "gamma": .02, "theta": -.03, "vega": .2, "open_interest": 500, "volume": 5, "last_tape_time": "2026-08-24T10:44:00Z"},
            {"option_symbol": "QCOM261218P00105000", "option_type": "put", "expiry": "2026-12-18", "strike": 105, "nbbo_bid": 4.1, "nbbo_ask": 4.3, "delta": -.5, "gamma": .02, "theta": -.03, "vega": .2, "open_interest": 600, "volume": 6, "last_tape_time": "2026-08-24T10:44:00Z"},
        ])
        gex = self._insert(Dataset.DEALER_EXPOSURE, {"call_wall": 110, "put_wall": 95, "gamma_flip": 100, "gamma_magnet": 102}, as_of=NOW - timedelta(minutes=2))
        artifact = build_morning_run(database=self.database, capture_report=report({CurrentDataset.OHLC: ohlc, CurrentDataset.OPTION_CHAIN: chain, CurrentDataset.DEALER_EXPOSURE: gex}), positions=({"ticker": "QCOM", "contract": "QCOM261218C00105000", "quantity": 1},))

        self.assertEqual(MORNING_RUN_SCHEMA_VERSION, artifact["run_schema_version"])
        self.assertFalse(artifact["recommendations_enabled"])
        record = artifact["watchlist"][0]
        self.assertEqual("NO_RECOMMENDATION", record["action"])
        self.assertEqual({"data_ready": False, "calibrated": False, "execution_ready": False}, {key: record["decision"][key] for key in ("data_ready", "calibrated", "execution_ready")})
        self.assertEqual("NO_RECOMMENDATION", record["agent_analysis"]["suggested_action"])
        self.assertEqual(2, len(record["options"]["candidates"]))
        self.assertTrue(all(item["quote_fresh"] for item in record["options"]["candidates"]))
        self.assertTrue(all(item["status"] == "NOT_ELIGIBLE" for item in record["options"]["candidates"]))
        self.assertEqual(1, len(record["positions"]))
        self.assertIn("snapshot_id", record["evidence"]["source_refs"][0])
        self.assertEqual(["test"], record["provenance"]["provider"])
        self.assertTrue(record["data_quality"]["complete"])
        self.assertIn("Narrative", record["analyst"]["note"])
        self.assertEqual("CONDITIONAL_RESEARCH_ONLY", record["trade_thesis"]["status"])
        self.assertFalse(record["trade_thesis"]["conviction_is_probability"])
        self.assertEqual(1, record["trade_rank"])

        required = {"gates", "price", "return_1d_pct", "technical", "coverage_status", "analyst", "agent_analysis", "options", "data_quality", "provenance", "edge", "trade_thesis", "trade_rank"}
        self.assertTrue(required.issubset(record))
        self.assertEqual({"data_ready": False, "calibrated": False, "execution_ready": False}, record["gates"])
        self.assertTrue({"bars", "rsi14", "rv20_ann_pct", "ema20", "ema50", "drawdown_126d_pct"}.issubset(record["technical"]))
        self.assertTrue(all({"date", "close", "ema20", "ema50"}.issubset(item) for item in record["technical"]["bars"]))

    def test_absent_evidence_abstains_and_atomic_writer_is_owner_only(self) -> None:
        artifact = build_morning_run(database=self.database, capture_report=report({}))
        record = artifact["watchlist"][0]
        self.assertEqual("ABSTAIN", record["agent_analysis"]["status"])
        self.assertEqual([], record["options"]["candidates"])
        destination = write_morning_run(Path(self.directory.name) / "run.json", artifact)
        self.assertEqual(0o600, stat.S_IMODE(destination.stat().st_mode))
        self.assertEqual(artifact["run_id"], json.loads(destination.read_text())["run_id"])

    def test_default_cutoff_includes_snapshots_captured_after_collection_started(self) -> None:
        started = NOW - timedelta(minutes=5)
        fetched = NOW + timedelta(minutes=2)
        chain = self._insert(Dataset.OPTION_CHAIN, [{
            "option_symbol": "QCOM261218C00105000", "option_type": "call", "expiry": "2026-12-18", "strike": 105,
            "nbbo_bid": 4.0, "nbbo_ask": 4.2, "delta": .5, "gamma": .02, "theta": -.03, "vega": .2,
            "open_interest": 500, "last_tape_time": fetched.isoformat(),
        }], as_of=NOW, retrieved_at=fetched)
        artifact = build_morning_run(database=self.database, capture_report=report({CurrentDataset.OPTION_CHAIN: chain}, generated_at=started, fetched_at=fetched))
        record = artifact["watchlist"][0]
        self.assertEqual(timestamp_text(fetched), artifact["cutoff_at"])
        self.assertIn(chain, record["provenance"]["snapshot_ids"])
        self.assertIn(chain, [item["snapshot_id"] for item in artifact["capture_report"]["results"]])

    def test_wide_spread_contracts_are_not_candidates(self) -> None:
        chain = self._insert(Dataset.OPTION_CHAIN, [{
            "option_symbol": "QCOM261218C00105000", "option_type": "call", "expiry": "2026-12-18", "strike": 105,
            "nbbo_bid": 1.0, "nbbo_ask": 2.0, "delta": .5, "gamma": .02, "theta": -.03, "vega": .2,
            "open_interest": 500, "last_tape_time": NOW.isoformat(),
        }])
        artifact = build_morning_run(database=self.database, capture_report=report({CurrentDataset.OPTION_CHAIN: chain}))
        self.assertEqual([], artifact["watchlist"][0]["options"]["candidates"])

    def test_reference_universe_keeps_multiple_contracts_per_side_but_is_bounded(self) -> None:
        rows = []
        for index in range(9):
            rows.extend([
                {"option_symbol": f"QCOM261218C{100000 + index * 5000:08d}", "option_type": "call", "expiry": "2026-12-18", "strike": 100 + index * 5, "nbbo_bid": 4.0, "nbbo_ask": 4.2, "delta": .35 + index * .02, "gamma": .02, "theta": -.03, "vega": .2, "open_interest": 500 + index, "last_tape_time": NOW.isoformat()},
                {"option_symbol": f"QCOM261218P{100000 + index * 5000:08d}", "option_type": "put", "expiry": "2026-12-18", "strike": 100 + index * 5, "nbbo_bid": 4.0, "nbbo_ask": 4.2, "delta": -.35 - index * .02, "gamma": .02, "theta": -.03, "vega": .2, "open_interest": 500 + index, "last_tape_time": NOW.isoformat()},
            ])
        chain = self._insert(Dataset.OPTION_CHAIN, rows)
        artifact = build_morning_run(database=self.database, capture_report=report({CurrentDataset.OPTION_CHAIN: chain}))
        candidates = artifact["watchlist"][0]["options"]["candidates"]
        self.assertEqual(12, len(candidates))
        self.assertEqual(6, sum(row["option_type"] == "CALL" for row in candidates))
        self.assertEqual(6, sum(row["option_type"] == "PUT" for row in candidates))
        self.assertTrue(all(row["status"] == "NOT_ELIGIBLE" for row in candidates))

    def test_analog_frequency_below_half_reduces_directional_thesis_score(self) -> None:
        def edge(frequency: float) -> dict[str, object]:
            return {
                "dimensions": {
                    "directional_edge": 70,
                    "evidence_quality": 60,
                    "positioning_context": 60,
                    "tradeability": 60,
                    "catalyst_risk": 0,
                },
                "forecast": {
                    "direction": "BULLISH",
                    "status": "EXPERIMENTAL_UNCALIBRATED",
                    "directional_analog_frequency": frequency,
                    "center_return_20d": .03,
                    "low_return_20d": -.05,
                    "high_return_20d": .10,
                },
                "historical_analogs": {
                    "sample_size": 10,
                    "historical_disposition": "BULLISH",
                    "stability": {"status": "STABLE"},
                },
            }

        conflicting = _trade_thesis(technical={}, edge=edge(.40), contracts=[])
        supportive = _trade_thesis(technical={}, edge=edge(.60), contracts=[])

        self.assertEqual("BULLISH", conflicting["direction"])
        self.assertGreater(supportive["conviction_score"], conflicting["conviction_score"])


if __name__ == "__main__":
    unittest.main()
