import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts" / "enrich_morning_run.py"
SPEC = importlib.util.spec_from_file_location("enrich_morning_run", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def source() -> dict:
    return {
        "run_schema_version": "morning_run/v1",
        "watchlist": [{
            "ticker": "QCOM",
            "action": "NO_RECOMMENDATION",
            "technical": {"ema20": 164.0},
            "evidence": {"news": {"latest_headlines": [{"headline": "Test headline"}]}},
            "provenance": {"snapshot_ids": [10, 11]},
        }],
    }


def record() -> dict:
    return {
        "ticker": "QCOM",
        "action": "NO_RECOMMENDATION",
        "posture": "MIXED",
        "research_priority": 70,
        "evidence_confidence": 60,
        "day_outlook": "Prior-session evidence is mixed and execution inputs are stale.",
        "summary": "Price is below the short trend reference, with limited directional confirmation.",
        "evidence_points": [{
            "statement": "The close remains below EMA20.",
            "field_refs": ["technical.ema20", "evidence.news.latest_headlines[0]"],
            "source_snapshot_ids": [10],
        }, {
            "statement": "The action gate remains closed.",
            "field_refs": ["action"],
            "source_snapshot_ids": [11],
        }],
        "counterevidence": ["One-day return is near flat.", "Monday option observations are unavailable."],
        "scenarios": [
            {"name": "BULL", "conditions": ["Trend recovers."], "outcome": "Momentum improves conditionally.", "invalidation": ["Trend recovery fails."]},
            {"name": "BASE", "conditions": ["Trend stays mixed."], "outcome": "Range behavior remains plausible.", "invalidation": ["A decisive move develops."]},
            {"name": "BEAR", "conditions": ["Weakness persists."], "outcome": "Downside pressure remains possible.", "invalidation": ["Price reclaims trend references."]},
        ],
        "option_context": "The displayed contract is a non-actionable stale reference.",
        "unknowns": ["Monday executable option quotes are unavailable."],
    }


class EnrichmentTests(unittest.TestCase):
    def test_validates_and_ranks_without_enabling_recommendations(self) -> None:
        result = module.enrich(source(), [{"schema": module.ENRICHMENT_SCHEMA, "records": [record()]}], input_digest="a" * 64)
        item = result["watchlist"][0]
        self.assertEqual(1, item["research_rank"])
        self.assertTrue(item["agent_enrichment_validated"])
        self.assertFalse(result["agentic_analysis"]["recommendations_enabled"])

    def test_rejects_unknown_source_or_action_language(self) -> None:
        bad = record()
        bad["evidence_points"][0]["source_snapshot_ids"] = [99]
        with self.assertRaisesRegex(ValueError, "outside the current capture"):
            module.enrich(source(), [{"schema": module.ENRICHMENT_SCHEMA, "records": [bad]}], input_digest="b" * 64)
        bad = record()
        bad["summary"] = "This is a buy setup."
        with self.assertRaisesRegex(ValueError, "trade-action language"):
            module.enrich(source(), [{"schema": module.ENRICHMENT_SCHEMA, "records": [bad]}], input_digest="b" * 64)

    def test_normalizes_named_scenario_map_and_single_text_conditions(self) -> None:
        item = record()
        item["scenarios"] = {
            scenario["name"]: {
                **scenario,
                "conditions": scenario["conditions"][0],
                "invalidation": scenario["invalidation"][0],
            }
            for scenario in item["scenarios"]
        }
        result = module.enrich(
            source(),
            [{"schema": module.ENRICHMENT_SCHEMA, "records": [item]}],
            input_digest="c" * 64,
        )
        scenarios = result["watchlist"][0]["agent_enrichment"]["scenarios"]
        self.assertEqual(["BULL", "BASE", "BEAR"], [scenario["name"] for scenario in scenarios])
        self.assertIsInstance(scenarios[0]["conditions"], list)
        self.assertIsInstance(scenarios[0]["invalidation"], list)


if __name__ == "__main__":
    unittest.main()
