from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from morning_edge.ledger import ForecastLedger as SqliteForecastLedger, StoredForecast
from morning_edge.models import Dataset, SnapshotEnvelope
from morning_edge.pipeline import AnalysisRequest, SourceSnapshot, run_analysis
from morning_edge.scoring import Action, Evidence, MorningInputs, PortfolioPosition, Provenance
from morning_edge.store import SnapshotStore


NOW = datetime(2026, 8, 20, 11, 0, tzinfo=timezone.utc)


def evidence(value: float, **changes: object) -> Evidence:
    return Evidence(
        value=value,
        as_of=changes.get("as_of", NOW),
        available_at=changes.get("available_at"),
        provenance=changes.get("provenance", Provenance.OBSERVED),
        source="fixture",
        quality=changes.get("quality", 1.0),
    )


def inferred(value: float, **changes: object) -> Evidence:
    return evidence(value, provenance=Provenance.INFERRED, **changes)


def modeled(value: float, **changes: object) -> Evidence:
    return evidence(value, provenance=Provenance.MODELED, **changes)


def inputs(**changes: object) -> MorningInputs:
    data = dict(
        ticker="QCOM", captured_at=NOW, price=evidence(160), trend=modeled(.8),
        flow=inferred(.8), oi_change=evidence(.7), gex=modeled(.6), iv_rank=modeled(40),
        bid_ask_spread_pct=evidence(2), catalyst=inferred(.5), event_risk=modeled(.1),
        execution_ready=True,
        calibration_ready=True,
    )
    data.update(changes)
    return MorningInputs(**data)


def source(snapshot_id: int = 1, **changes: object) -> SourceSnapshot:
    data = dict(
        snapshot_id=snapshot_id, as_of=NOW - timedelta(minutes=2),
        retrieved_at=NOW - timedelta(minutes=1), feature_payload={"provider_field": "opaque"}, symbol="QCOM",
    )
    data.update(changes)
    return SourceSnapshot(**data)


def request(**changes: object) -> AnalysisRequest:
    data = dict(
        inputs=inputs(), cutoff_at=NOW, generated_at=NOW + timedelta(seconds=1),
        horizon_sessions=20, sources=(source(),), feature_payload={"trend_feature": .8},
    )
    data.update(changes)
    return AnalysisRequest(**data)


class RecordingLedger:
    def __init__(self) -> None:
        self.records: list[object] = []

    def insert_forecast(self, record: object) -> str:
        self.records.append(record)
        return f"forecast-{len(self.records)}"


class PipelineTests(unittest.TestCase):
    def test_stores_exact_provenance_and_renders_deterministically(self) -> None:
        ledger = RecordingLedger()
        request_with_entry_rules = request(
            trigger_assumptions={"price_above": 161},
            invalidation_assumptions={"price_below": 157},
        )
        analysis = run_analysis(request_with_entry_rules, ledger=ledger)
        repeated = run_analysis(request_with_entry_rules)
        self.assertEqual(analysis.score.action, Action.BUY)
        self.assertEqual(analysis.ledger_receipt, "forecast-1")
        self.assertEqual(len(ledger.records), 1)
        self.assertEqual(analysis.ledger_record.action, "BUY")
        self.assertEqual(analysis.forecast_record.source_snapshot_ids, ("1",))
        self.assertEqual(analysis.forecast_record.feature_payload_digest, analysis.forecast_record.feature_payload_digest)
        self.assertIn("| QCOM | BUY", analysis.report_markdown)
        self.assertIn("Scoring version: `1.2.0`", analysis.report_markdown)
        self.assertEqual(analysis.report_markdown, repeated.report_markdown)
        self.assertEqual(analysis.forecast_record, repeated.forecast_record)

    def test_data_gate_failure_preserves_position_exit(self) -> None:
        bad = inputs(
            price=evidence(160, as_of=NOW - timedelta(minutes=6)),
            position=PortfolioPosition(contracts=1, unrealized_return_pct=0, days_to_expiry=2),
        )
        analysis = run_analysis(request(inputs=bad))
        self.assertFalse(analysis.score.data_gate.passed)
        self.assertIs(analysis.score.action, Action.EXIT)
        self.assertFalse(any("Pipeline fail-closed" in reason for reason in analysis.score.reasons))

    def test_data_gate_failure_converts_position_watch_to_no_action(self) -> None:
        bad = inputs(
            price=evidence(160, as_of=NOW - timedelta(minutes=6)),
            position=PortfolioPosition(contracts=1, unrealized_return_pct=0, days_to_expiry=30),
        )
        analysis = run_analysis(request(inputs=bad))
        self.assertIs(analysis.score.action, Action.NO_ACTION)
        self.assertTrue(any("Pipeline fail-closed" in reason for reason in analysis.score.reasons))

    def test_requires_source_ids_and_rejects_duplicate_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one"):
            request(sources=())
        with self.assertRaisesRegex(ValueError, "unique"):
            request(sources=(source(1), source(1)))

    def test_rejects_source_after_cutoff_and_ticker_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "later than the analysis cutoff"):
            request(sources=(source(retrieved_at=NOW + timedelta(seconds=1)),))
        with self.assertRaisesRegex(ValueError, "must match"):
            request(sources=(source(symbol="INTC"),))

    def test_rejects_input_cutoff_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "must equal"):
            request(inputs=inputs(captured_at=NOW - timedelta(minutes=1)))

    def test_rejects_invalid_generated_at_and_horizon(self) -> None:
        with self.assertRaisesRegex(ValueError, "generated_at"):
            request(generated_at=NOW - timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, "horizon_sessions"):
            request(horizon_sessions=0)

    def test_assumption_and_feature_mutation_after_construction_do_not_change_record(self) -> None:
        features = {"x": [1]}
        assumptions = {"strike": 170}
        exact_source = source(feature_payload={"chain": ["first"]})
        analysis_request = request(
            feature_payload=features,
            option_assumptions=assumptions,
            sources=(exact_source,),
        )
        before = run_analysis(analysis_request).forecast_record
        features["x"].append(2)
        assumptions["strike"] = 175
        exact_source.feature_payload["chain"].append("later")
        after = run_analysis(analysis_request).forecast_record
        self.assertEqual(before.feature_payload_digest, after.feature_payload_digest)
        self.assertEqual(before.option_assumptions_digest, after.option_assumptions_digest)
        self.assertEqual(before.source_feature_digests, after.source_feature_digests)

    def test_ledger_failure_is_not_hidden(self) -> None:
        class BrokenLedger:
            def insert_forecast(self, record: object) -> object:
                raise OSError("disk full")

        with self.assertRaisesRegex(OSError, "disk full"):
            run_analysis(request(), ledger=BrokenLedger())

    def test_ledger_requires_integer_snapshot_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "integer source snapshot IDs"):
            run_analysis(request(sources=(source(snapshot_id="provider-1"),)), ledger=RecordingLedger())

    def test_buy_requires_explicit_trigger_and_invalidation(self) -> None:
        analysis = run_analysis(request())
        self.assertIs(analysis.score.action, Action.WATCH)
        self.assertTrue(any("BUY downgraded" in reason for reason in analysis.score.reasons))
        ready = run_analysis(request(
            trigger_assumptions={"price_above": 161},
            invalidation_assumptions={"price_below": 157},
        ))
        self.assertIs(ready.score.action, Action.BUY)

    def test_persists_a_real_ledger_forecast_with_snapshot_lineage(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "morning-edge.sqlite"
            with SnapshotStore(database) as snapshots:
                stored = snapshots.insert(SnapshotEnvelope(
                    provider="fixture",
                    dataset=Dataset.OPTION_FLOW,
                    symbol="QCOM",
                    as_of=NOW - timedelta(minutes=2),
                    retrieved_at=NOW - timedelta(minutes=1),
                    payload={"unchanged": "provider response"},
                ))
            with SqliteForecastLedger(database) as ledger:
                analysis = run_analysis(request(sources=(source(snapshot_id=stored.id),)), ledger=ledger)
                self.assertIsInstance(analysis.ledger_receipt, StoredForecast)
                self.assertEqual(analysis.ledger_receipt.record.source_snapshot_ids, (stored.id,))
                self.assertEqual(analysis.ledger_receipt.record.feature_payload, {"trend_feature": .8})
