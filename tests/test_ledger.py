from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
import tempfile
import unittest
from pathlib import Path

from morning_edge.ledger import ForecastLedger, ForecastRecord, OutcomeRecord
from morning_edge.models import Dataset, SnapshotEnvelope
from morning_edge.store import SnapshotStore


UTC = timezone.utc
AS_OF = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
RETRIEVED = datetime(2026, 8, 20, 10, 5, tzinfo=UTC)
CUTOFF = datetime(2026, 8, 20, 10, 10, tzinfo=UTC)


class ForecastLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "morning-edge.sqlite3"
        self.snapshots = SnapshotStore(self.database)
        self.source = self.snapshots.insert(
            SnapshotEnvelope(
                provider="unusual_whales",
                dataset=Dataset.OPTION_CHAIN,
                symbol="QCOM",
                as_of=AS_OF,
                retrieved_at=RETRIEVED,
                payload={"contracts": [{"strike": 165, "iv": 0.31}]},
            )
        )
        self.ledger = ForecastLedger(self.database)

    def tearDown(self) -> None:
        self.ledger.close()
        self.snapshots.close()
        self.temporary_directory.cleanup()

    def forecast(self, **overrides: object) -> ForecastRecord:
        values: dict[str, object] = {
            "ticker": "QCOM",
            "cutoff_at": CUTOFF,
            "generated_at": CUTOFF + timedelta(seconds=5),
            "horizon_sessions": 20,
            "scoring_version": "1.0.0",
            "model_version": "forecast-1.0.0",
            "action": "WATCH",
            "setup_score": 72,
            "directional_probability": 0.56,
            "confidence": 0.68,
            "source_snapshot_ids": (self.source.id,),
            "trigger": "Close above 165 with positive flow",
            "invalidation": "Daily close below 155",
            "feature_payload": {"trend": 0.4, "flow": 0.3},
            "option_metadata": {"expiry": "2026-10-16", "strike": 165},
            "friction_metadata": {"spread_pct": 3.1},
        }
        values.update(overrides)
        return ForecastRecord(**values)  # type: ignore[arg-type]

    def test_forecast_is_idempotent_and_keeps_full_lineage(self) -> None:
        first = self.ledger.insert_forecast(self.forecast())
        second = self.ledger.insert_forecast(self.forecast())

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.record.feature_hash, first.record.feature_hash)
        loaded = self.ledger.get_forecast(first.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.record.source_snapshot_ids, (self.source.id,))
        self.assertEqual(loaded.record.option_metadata["strike"], 165)

    def test_rejects_future_or_unknown_source_snapshots(self) -> None:
        future = self.snapshots.insert(
            SnapshotEnvelope(
                provider="unusual_whales",
                dataset=Dataset.OPTION_FLOW,
                symbol="QCOM",
                as_of=CUTOFF + timedelta(minutes=1),
                retrieved_at=CUTOFF + timedelta(minutes=2),
                payload={"premium": 100000},
            )
        )
        with self.assertRaisesRegex(ValueError, "unavailable at cutoff"):
            self.ledger.insert_forecast(self.forecast(source_snapshot_ids=(future.id,)))
        with self.assertRaisesRegex(ValueError, "do not exist"):
            self.ledger.insert_forecast(self.forecast(source_snapshot_ids=(9999,)))

    def test_outcome_requires_post_cutoff_observation_and_is_idempotent(self) -> None:
        forecast = self.ledger.insert_forecast(self.forecast())
        with self.assertRaisesRegex(ValueError, "after forecast generation"):
            self.ledger.insert_outcome(OutcomeRecord(forecast.id, CUTOFF, underlying_return_pct=1.0))
        with self.assertRaisesRegex(ValueError, "after forecast generation"):
            self.ledger.insert_outcome(
                OutcomeRecord(forecast.id, CUTOFF + timedelta(seconds=1), underlying_return_pct=1.0)
            )

        first = self.ledger.insert_outcome(
            OutcomeRecord(
                forecast.id,
                CUTOFF + timedelta(days=20),
                underlying_return_pct=5.2,
                option_return_pct=21.3,
                max_adverse_excursion_pct=-6.1,
                realized_volatility=0.29,
                metadata={"close_source": "ohlc"},
            )
        )
        second = self.ledger.insert_outcome(first.record)
        self.assertEqual(first.id, second.id)

    def test_sqlite_triggers_prevent_mutation_and_lookahead(self) -> None:
        forecast = self.ledger.insert_forecast(self.forecast())
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.ledger.connection.execute("UPDATE forecasts SET action = 'BUY' WHERE id = ?", (forecast.id,))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "after forecast generation"):
            self.ledger.connection.execute(
                """
                INSERT INTO outcomes (
                    idempotency_key, forecast_id, observed_at, metadata_json, inserted_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ("a" * 64, forecast.id, "2026-08-20T10:10:00.000000Z", "{}", "2026-08-20T10:11:00.000000Z"),
            )

    def test_record_validation_requires_utc_aware_timestamps_and_feature_evidence(self) -> None:
        values = dict(
            ticker="QCOM",
            cutoff_at=CUTOFF.replace(tzinfo=None),
            generated_at=CUTOFF,
            horizon_sessions=20,
            scoring_version="1",
            model_version="1",
            action="WATCH",
            setup_score=50,
            directional_probability=0.5,
            confidence=0.5,
            source_snapshot_ids=(self.source.id,),
            trigger="above 165",
            invalidation="below 155",
            feature_hash="b" * 64,
        )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            ForecastRecord(**values)
        values["cutoff_at"] = CUTOFF
        values.pop("feature_hash")
        with self.assertRaisesRegex(ValueError, "feature_payload or feature_hash"):
            ForecastRecord(**values)


if __name__ == "__main__":
    unittest.main()
