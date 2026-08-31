from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from morning_edge.feature_mart import FeatureMart, FeatureRecord
from morning_edge.provider_contracts import Accumulation, contract_catalog, contract_for
from morning_edge.signal_registry import SignalStatus, registry, signal


UTC = timezone.utc


class ResearchControlTests(unittest.TestCase):
    def test_feature_records_and_replays_are_idempotent_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "research.sqlite"
            cutoff = datetime(2026, 8, 28, 11, tzinfo=UTC)
            record = FeatureRecord(
                ticker="qcom", effective_session=date(2026, 8, 27),
                cutoff_at=cutoff, available_at=cutoff - timedelta(minutes=1),
                feature_version="edge-v1", values={"trend.return_5d": 0.03},
                source_snapshot_ids=(4, 2, 4),
            )
            with FeatureMart(path) as mart:
                first = mart.insert(record)
                second = mart.insert(record)
                self.assertEqual(first, second)
                replay = mart.register_replay(
                    run_id="run-1", cutoff_at=cutoff, input_payload={"a": 1},
                    feature_record_ids=(first,), model_versions=("v3",),
                )
                self.assertEqual(replay, mart.register_replay(
                    run_id="run-1", cutoff_at=cutoff, input_payload={"a": 1},
                    feature_record_ids=(first,), model_versions=("v3",),
                ))
                self.assertEqual([first], mart.manifest(replay)["feature_record_ids"])
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    mart.connection.execute("UPDATE feature_records SET quality_status='BAD'")

    def test_feature_record_rejects_lookahead(self) -> None:
        cutoff = datetime(2026, 8, 28, 11, tzinfo=UTC)
        with self.assertRaisesRegex(ValueError, "available_at"):
            FeatureRecord(
                ticker="QCOM", effective_session=date(2026, 8, 27),
                cutoff_at=cutoff, available_at=cutoff + timedelta(seconds=1),
                feature_version="v1", values={"x": 1}, source_snapshot_ids=(1,),
            )

    def test_provider_contracts_make_accumulation_explicit(self) -> None:
        greek = contract_for("greek-flow", "GreekFlow", "dir_delta_flow")
        option_state = contract_for("option-states", "OptionState", "volume")
        self.assertEqual(Accumulation.PARTIAL_BUCKET, greek.accumulation)
        self.assertEqual(Accumulation.DAILY_CUMULATIVE, option_state.accumulation)
        self.assertIn("do not sum", option_state.safe_aggregation)
        self.assertEqual("CONTEXT_ONLY_UNTIL_CONTRACTED", contract_catalog()["unregistered_field_policy"])

    def test_signal_registry_keeps_unvalidated_signals_out_of_confidence(self) -> None:
        catalog = registry()
        self.assertEqual(0, catalog["validated_signal_count"])
        self.assertEqual(SignalStatus.CONTEXT_ONLY, signal("dark_pool.response_level").status)
        self.assertGreater(signal("dark_pool.response_level").collection_priority, 1)


if __name__ == "__main__":
    unittest.main()
