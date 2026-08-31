from datetime import datetime, timedelta, timezone
import unittest

from morning_edge.agent_analysis import (
    ANALYSIS_SCHEMA_VERSION, AgentAnalysis, AgentAnalysisValidationError, AnalysisCritic,
    AnalysisStatus, AssertionType, Claim, Confidence, Contradiction, DeterministicAnalystBackend,
    Direction, EvidenceBundle, EvidenceFeature, EvidenceSource, FeatureStatus, Scenario,
    ScenarioName, SuggestedAction,
)


NOW = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
HASH = "a" * 64


def bundle(**changes: object) -> EvidenceBundle:
    values = {
        "analysis_id": "qcom-2026-08-24", "ticker": "QCOM", "cutoff_at": NOW,
        "feature_version": "features-v1",
        "sources": (EvidenceSource("ohlc-1", 1, "ohlc", NOW - timedelta(days=1), NOW - timedelta(hours=1), HASH),),
        "features": (EvidenceFeature("trend.return_20d", 0.05, "ratio", "modeled", "trend-v1", ("ohlc-1",), NOW - timedelta(hours=1), 0.9),),
    }
    values.update(changes)
    return EvidenceBundle(**values)


def scenarios(feature_id: str = "trend.return_20d", *, probability: float | None = None, calibration_id: str | None = None) -> tuple[Scenario, ...]:
    return (
        Scenario(ScenarioName.BULL, ("Trend remains constructive.",), Direction.BULLISH, 20, (feature_id,), ("Trend reverses.",), probability, calibration_id),
        Scenario(ScenarioName.BASE, ("Trend remains mixed.",), Direction.NEUTRAL, 20, (feature_id,), ("Volatility regime changes.",), probability, calibration_id),
        Scenario(ScenarioName.BEAR, ("Trend reverses.",), Direction.BEARISH, 20, (feature_id,), ("Trend recovers.",), probability, calibration_id),
    )


def analysis(**changes: object) -> AgentAnalysis:
    values = {
        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION, "analysis_id": "qcom-2026-08-24",
        "status": AnalysisStatus.COMPLETE, "evidence_summary": "Trend evidence is available.",
        "claims": (Claim("claim-1", "trend", Direction.BULLISH, 20, "Trend is constructive.", AssertionType.INFERENCE, ("trend.return_20d",), (), ("ohlc-1",), "No countervailing feature is in this minimal bundle.", "One feature is insufficient for a trade."),),
        "scenarios": scenarios(),
        "confidence": Confidence(35, 100, 70, 20, 90, 0, 0, 80, ("Not calibrated.",)),
        "suggested_action": SuggestedAction.WATCH,
    }
    values.update(changes)
    return AgentAnalysis(**values)


class AgentAnalysisTests(unittest.TestCase):
    def test_fallback_is_evidence_bound_and_non_actionable(self) -> None:
        result = DeterministicAnalystBackend().analyze(bundle())
        self.assertEqual(result.status, AnalysisStatus.COMPLETE)
        self.assertEqual(result.suggested_action, SuggestedAction.NO_RECOMMENDATION)
        self.assertEqual(result.claims[0].source_refs, ("ohlc-1",))
        self.assertEqual({item.name for item in result.scenarios}, set(ScenarioName))

    def test_fallback_blocks_missing_required_feature(self) -> None:
        item = EvidenceFeature("flow.confirmed", None, "ratio", "inferred", "flow-v1", ("ohlc-1",), NOW - timedelta(hours=1), 0.0, FeatureStatus.MISSING)
        result = DeterministicAnalystBackend().analyze(bundle(features=(item,), required_feature_ids=("flow.confirmed",)))
        self.assertEqual(result.status, AnalysisStatus.BLOCKED)
        self.assertEqual(result.suggested_action, SuggestedAction.NO_RECOMMENDATION)
        self.assertEqual(len(result.unknowns), 1)

    def test_critic_rejects_unknown_feature_and_source_refs(self) -> None:
        bad_claim = Claim("claim-1", "trend", Direction.BULLISH, 20, "Trend is constructive.", AssertionType.INFERENCE, ("not-a-feature",), (), ("not-a-source",), "Counterevidence reviewed.", "Limited data.")
        with self.assertRaisesRegex(AgentAnalysisValidationError, "unknown features"):
            AnalysisCritic.validate(bundle(), analysis(claims=(bad_claim,)))

    def test_critic_rejects_uncalibrated_probability(self) -> None:
        with self.assertRaisesRegex(AgentAnalysisValidationError, "unsupported probability"):
            AnalysisCritic.validate(bundle(), analysis(scenarios=scenarios(probability=0.6, calibration_id="cal-1")))

    def test_critic_accepts_calibrated_probability_only_with_registered_calibration(self) -> None:
        accepted_bundle = bundle(calibration_ready=True, calibration_ids=("cal-1",))
        AnalysisCritic.validate(accepted_bundle, analysis(scenarios=scenarios(probability=0.6, calibration_id="cal-1")))

    def test_critic_rejects_buy_without_deterministic_authorization(self) -> None:
        with self.assertRaisesRegex(AgentAnalysisValidationError, "BUY language"):
            AnalysisCritic.validate(bundle(), analysis(suggested_action=SuggestedAction.BUY))
        with self.assertRaisesRegex(AgentAnalysisValidationError, "BUY language"):
            AnalysisCritic.validate(bundle(), analysis(evidence_summary="Buy immediately."))

    def test_critic_rejects_complete_with_unknowns_and_blocked_without_unknowns(self) -> None:
        unknown = ("flow", "No accepted flow response.", "Flow interpretation", "Collect it.")
        from morning_edge.agent_analysis import Unknown
        with self.assertRaisesRegex(AgentAnalysisValidationError, "COMPLETE"):
            AnalysisCritic.validate(bundle(), analysis(unknowns=(Unknown(*unknown),)))
        with self.assertRaisesRegex(AgentAnalysisValidationError, "BLOCKED"):
            AnalysisCritic.validate(bundle(), analysis(status=AnalysisStatus.BLOCKED))

    def test_from_dict_rejects_unknown_top_level_fields(self) -> None:
        payload = analysis().to_dict()
        payload["invented"] = True
        with self.assertRaisesRegex(AgentAnalysisValidationError, "unknown fields"):
            AgentAnalysis.from_dict(payload)

    def test_bundle_rejects_source_or_feature_after_cutoff(self) -> None:
        future_source = EvidenceSource("next", 2, "ohlc", NOW, NOW + timedelta(seconds=1), HASH)
        with self.assertRaisesRegex(ValueError, "later than cutoff"):
            bundle(sources=(future_source,))
        future_feature = EvidenceFeature("future", 1, "x", "modeled", "x-v1", ("ohlc-1",), NOW + timedelta(seconds=1), 1.0)
        with self.assertRaisesRegex(ValueError, "feature available_at"):
            bundle(features=(future_feature,))


if __name__ == "__main__":
    unittest.main()
