from dataclasses import replace
from datetime import datetime, timezone
import unittest

from morning_edge.report import render_markdown
from morning_edge.scoring import Action, DataGate, Provenance, ScoreResult


NOW = datetime(2026, 8, 20, 11, 0, tzinfo=timezone.utc)


def result(ticker: str, action: Action, passed: bool) -> ScoreResult:
    return ScoreResult(
        ticker=ticker, scoring_version="1.0.0", as_of=NOW, action=action,
        setup_score=71, directional_probability=.63, confidence=76,
        execution_ready=passed,
        calibration_ready=passed,
        data_gate=DataGate(passed, ("Missing required flow data",) if not passed else (), .8, .9),
        reasons=("A reason",), component_scores={},
        provenance_summary={Provenance.OBSERVED: 5, Provenance.INFERRED: 1, Provenance.MODELED: 0},
    )


class ReportTests(unittest.TestCase):
    def test_report_orders_urgent_actions_and_explains_data_block(self) -> None:
        report = render_markdown([result("QCOM", Action.BUY, True), result("INTC", Action.NO_ACTION, False)], generated_at=NOW)
        self.assertLess(report.index("| QCOM | BUY"), report.index("| INTC | NO_ACTION"))
        self.assertIn("Scoring version: `1.0.0`", report)
        self.assertIn("Evidence provenance: observed 5, inferred 1, modeled 0.", report)
        self.assertIn("Do not open a new position", report)
        self.assertIn("PASS / READY / VALIDATED", report)
        self.assertIn("BLOCKED / NOT READY / SHADOW", report)

    def test_report_explains_non_executable_watchlist_snapshot(self) -> None:
        non_executable = replace(result("QCOM", Action.WATCH, True), execution_ready=False)
        report = render_markdown([non_executable], generated_at=NOW)
        self.assertIn("PASS / NOT READY", report)
        self.assertIn("cannot authorize a new option entry", report)

    def test_report_explains_shadow_mode(self) -> None:
        shadow = replace(
            result("QCOM", Action.WATCH, True),
            calibration_ready=False,
        )
        report = render_markdown([shadow], generated_at=NOW)
        self.assertIn("PASS / READY / SHADOW", report)
        self.assertIn("Keep this result in shadow mode", report)

    def test_report_is_newline_terminated(self) -> None:
        self.assertTrue(render_markdown([], generated_at=NOW).endswith("\n"))
