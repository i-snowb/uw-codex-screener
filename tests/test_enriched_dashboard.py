import importlib.util
from datetime import date, timedelta
from pathlib import Path
import re
import stat
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts" / "build_enriched_morning_dashboard.py"
SPEC = importlib.util.spec_from_file_location("enriched_dashboard", SCRIPT)
assert SPEC and SPEC.loader
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)


def inline_script(fragment: str) -> str:
    match = re.search(r"<script>\s*([\s\S]*?)\s*</script>", fragment)
    if match is None:
        raise AssertionError("inline dashboard script is missing")
    return match.group(1)


def sample() -> dict:
    return {
        "mode": "HISTORICAL / SHADOW",
        "as_of": "2026-08-21T20:00:00Z",
        "watchlist": [{
            "ticker": "QCOM",
            "price": 160.75,
            "action": "BUY",
            "gates": {"data_ready": False, "calibrated": False, "execution_ready": False},
            "data_quality": {"complete": False, "chain_status": "historical close", "gex_status": "complete"},
            "technical": {"rsi14": 43.6, "bars": [{"date": "2026-08-20", "close": 160}, {"date": "2026-08-21", "close": 160.75, "ema20": 164.04}]},
            "analyst": {"claims": ["Close remains below EMA20."], "counterevidence": ["No fresh flow."], "unknowns": ["Premarket quote missing."]},
            "options": {"candidates": [{"type": "call", "strike": 165, "expiry": "2026-10-16", "bid": 4, "ask": 4.5}]},
        }],
    }


class EnrichedDashboardTests(unittest.TestCase):
    def test_two_year_price_history_and_ohlcv_are_preserved(self) -> None:
        start = date(2024, 1, 1)
        bars = [
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "open": 100 + index,
                "high": 102 + index,
                "low": 99 + index,
                "close": 101 + index,
                "volume": 1_000_000 + index,
                "ema20": 100.5 + index,
                "ema50": 99.5 + index,
            }
            for index in range(530)
        ]
        parsed = dashboard._bars(bars)
        self.assertEqual(520, len(parsed))
        self.assertEqual((start + timedelta(days=10)).isoformat(), parsed[0]["d"])
        self.assertEqual(
            {"d", "o", "h", "l", "c", "v", "e20", "e50"},
            set(parsed[-1]),
        )

    def test_fail_closed_action_and_option_candidate(self) -> None:
        entry = dashboard.normalize_run(sample())["entries"][0]
        self.assertEqual("NO_RECOMMENDATION", entry["action"])
        self.assertEqual("NOT_ELIGIBLE", entry["options"][0]["status"])
        self.assertIn("model calibration is not validated", entry["gateReasons"])

    def test_fragment_is_self_contained_and_escapes_data(self) -> None:
        run = sample()
        run["watchlist"][0]["analyst"]["claims"] = ["<script>bad()</script>"]
        fragment = dashboard.build_fragment(run)
        self.assertIn('id="codex-screener"', fragment)
        self.assertIn('id="me-consensus-pulse"', fragment)
        script = inline_script(fragment)
        self.assertIn("renderConsensusPulse", script)
        self.assertIn("Engine consensus", script)
        self.assertIn("cs-consensus-draw", fragment)
        self.assertNotIn("me-consensus-scan", fragment)
        self.assertIn("infinite!important", fragment)
        self.assertIn("chartView==='FORECAST')range='1M'", script)
        self.assertIn("chartView='FORECAST';range='1M'", script)
        self.assertNotIn("<h1>Codex Screener", fragment)
        self.assertNotIn("data:text/javascript", fragment)
        self.assertNotIn("<script>bad()</script>", fragment)
        self.assertIn(r"\u003cscript\u003ebad()\u003c/script\u003e", script)
        self.assertIn("NO_RECOMMENDATION", script)
        self.assertIn("State outcomes", script)
        self.assertIn("drawOutcomeChart", script)
        self.assertIn('id="me-levels"', fragment)
        self.assertIn('id="me-derivatives"', fragment)
        self.assertIn('id="me-intel"', fragment)
        self.assertIn("renderLevels", script)
        self.assertIn("renderDerivatives", script)
        self.assertIn("renderIntel", script)
        self.assertIn("Price catalyst", script)
        self.assertIn("Option flow", script)
        self.assertIn("drawFlowMini", script)
        self.assertIn("prior-20 top decile", script)
        self.assertIn("Added provider signals", script)
        self.assertIn("Stress-test", script)
        self.assertIn("V4 volatility fan", script)
        self.assertIn("'1Y':252,'2Y':504,'ALL':null", script)
        self.assertIn("drawHistoryChart", script)
        self.assertIn('id="me-study"', fragment)
        self.assertIn("chartStudy='NONE'", script)
        self.assertIn("chartIndicators", script)
        self.assertIn("Long trend", script)
        self.assertIn("Volatility", script)
        self.assertIn("me-sma200", fragment)
        self.assertIn("me-bb-band", fragment)
        self.assertIn("hasVolume?[['VOLUME','Volume']]:[]", script)
        self.assertIn("me-close-area", fragment)
        self.assertIn("Volume", script)
        self.assertIn("V3 baseline", script)
        self.assertIn("IV envelope", script)
        self.assertIn("V4 shadow forecast", script)
        self.assertIn("Gamma topology", script)
        self.assertIn("Greek flow + OI", script)
        self.assertIn("Option reference", script)
        self.assertIn("Dark-pool shelf", script)
        self.assertIn("Native Unusual Whales evidence", script)
        self.assertIn("Short crowding", script)
        self.assertIn("me-deriv-bullish", fragment)
        self.assertIn("me-deriv-bearish", fragment)
        self.assertIn("me-deriv-cautious", fragment)
        self.assertIn("Without consecutive-chain OI confirmation, the card stays cautious.", script)
        self.assertIn("me-option-call", fragment)
        self.assertIn("me-option-put", fragment)
        self.assertIn("Mid / spread", script)
        self.assertIn("Risk-neutral B/E", script)
        self.assertIn("What changed and why it matters", script)
        self.assertIn("Why it matters:", script)
        self.assertIn("Confirmation:", script)
        self.assertIn("Model accountability", script)
        self.assertIn("renderEvaluation", script)
        self.assertIn("evaluationHorizon", script)
        self.assertIn("Model accountability", script)
        self.assertIn("frozen forecasts", script)
        self.assertIn("prospective resolved", script)
        self.assertIn("Evidence alignment", script)
        self.assertIn("evidence quality", script)
        self.assertIn("GEX same-method", script)
        self.assertIn("Opportunity map", fragment)
        self.assertIn("Daily score change", fragment)
        self.assertIn("Price decision ladder", fragment)
        self.assertIn("Forecast tracking", fragment)
        self.assertIn("drawOpportunityMap", script)
        self.assertIn("renderScoreChange", script)
        self.assertIn("drawDecisionLadder", script)
        self.assertIn("renderTracking", script)
        self.assertIn("MARKET CONTEXT", fragment)
        self.assertIn("WATCHLIST DECISIONS", fragment)
        self.assertIn("SELECTED STOCK", fragment)
        self.assertIn("MODEL RESULTS", fragment)
        self.assertIn("renderSystemStatus", script)
        self.assertIn("renderWatchControls", script)
        self.assertIn("renderWatchAlerts", script)
        self.assertIn("renderStockTimeline", script)
        self.assertIn("renderPlatformTracking", script)
        self.assertIn("removeGlobalDetailFromStock", script)
        self.assertLess(fragment.index("MARKET CONTEXT"), fragment.index("WATCHLIST DECISIONS"))
        self.assertLess(fragment.index("WATCHLIST DECISIONS"), fragment.index("SELECTED STOCK"))
        self.assertLess(fragment.index("SELECTED STOCK"), fragment.index("MODEL RESULTS"))
        self.assertIn("showForecastSummary", script)
        self.assertIn("V4 volatility-scaled paths", script)
        self.assertIn("Forecast unavailable", script)
        self.assertIn("T12:00:00Z", script)
        self.assertIn("Stress-test with Codex", fragment)
        self.assertIn('id="me-codex-dialog"', fragment)
        self.assertIn('id="me-codex-prompt"', fragment)
        self.assertIn("requestCodexStressTest", script)
        self.assertIn("copyCodexPrompt", script)
        self.assertIn("Prompt copied. Paste it into the current Codex task.", script)
        self.assertNotIn("if(window.openai?.sendFollowUpMessage)await window.openai.sendFollowUpMessage", script)
        self.assertIn("\\u003cscript", script)
        self.assertNotIn("fetch(", fragment)
        self.assertIn("Decision view", fragment)
        self.assertIn("Research detail", fragment)
        self.assertIn("me-mobile-nav", fragment)
        self.assertIn("Shadow models", fragment)
        self.assertNotIn("MutationObserver", script)
        self.assertNotIn("ResizeObserver", script)
        self.assertIn("overflow:auto", fragment)
        self.assertIn("contain:layout paint style", fragment)
        self.assertEqual(1, fragment.count("<script>"))
        self.assertEqual(1, fragment.count("</script>"))
        self.assertLess(len(fragment.encode()), 1_000_000)

    def test_previous_run_is_reduced_to_score_comparison_fields(self) -> None:
        run = sample()
        prior = sample()
        prior["as_of"] = "2026-08-20T20:00:00Z"
        prior["watchlist"][0]["trade_thesis"] = {
            "direction": "BEARISH", "conviction_score": 42,
        }
        prior["watchlist"][0]["edge"] = {"dimensions": {"directional_edge": 38}}
        run["previous_run"] = prior
        previous = dashboard.normalize_run(run)["entries"][0]["previous"]
        self.assertEqual("2026-08-20T20:00:00Z", previous["asOf"])
        self.assertEqual("BEARISH", previous["direction"])
        self.assertEqual(42, previous["conviction"])
        self.assertEqual(38, previous["dimensions"]["directional"])

    def test_daily_flow_history_is_bounded_and_chart_ready(self) -> None:
        run = sample()
        run["watchlist"][0]["edge"] = {
            "flow_conviction": {
                "history_sessions": 80,
                "directional_zscore": 2.4,
                "directional_percentile": 0.96,
                "history": [
                    {
                        "market_date": f"2026-06-{(index % 28) + 1:02d}",
                        "directional_premium": float(index * 1000),
                        "directional_share": 0.2,
                        "alert_count": index,
                    }
                    for index in range(80)
                ],
            }
        }
        entry = dashboard.normalize_run(run)["entries"][0]
        flow = entry["edge"]["flow"]
        self.assertEqual(65, len(flow["history"]))
        self.assertEqual(15000.0, flow["history"][0]["premium"])
        self.assertEqual(2.4, flow["zscore"])
        fragment = dashboard.build_fragment(run)
        self.assertIn('id="me-flow-mini"', fragment)
        self.assertIn("select a bar for premium and rank", fragment)

    def test_morning_run_v1_price_cutoff_and_provider_shape(self) -> None:
        run = sample()
        run.pop("as_of")
        run["cutoff_at"] = "2026-08-24T12:24:10Z"
        run["watchlist"][0]["price"] = {"value": 160.75, "as_of": "2026-08-21"}
        run["watchlist"][0]["coverage_status"] = "COMPLETE_CAPTURE_NOT_EXECUTION_VALIDATED"
        run["watchlist"][0]["provenance"] = {"provider": ["unusual_whales"]}
        normalized = dashboard.normalize_run(run)
        entry = normalized["entries"][0]
        self.assertEqual(160.75, entry["price"])
        self.assertEqual("2026-08-24T12:24:10Z", normalized["asOf"])
        self.assertEqual("unusual_whales", entry["provenance"]["provider"])
        self.assertEqual("COMPLETE_CAPTURE_NOT_EXECUTION_VALIDATED", entry["technical"]["coverage"])

    def test_validated_agent_enrichment_is_visible_and_ranked(self) -> None:
        run = sample()
        first = run["watchlist"][0]
        first["research_rank"] = 2
        first["agent_enrichment_validated"] = True
        first["agent_enrichment"] = {
            "posture": "MIXED",
            "research_priority": 74,
            "evidence_confidence": 63,
            "day_outlook": "Prior-session evidence remains mixed.",
            "summary": "The validated synthesis stays conditional.",
            "evidence_points": [{"statement": "Price remains below EMA20."}],
            "counterevidence": ["One-day price action was near flat."],
            "unknowns": ["Current executable quotes are unavailable."],
            "scenarios": [{
                "name": "BASE",
                "conditions": ["Price stays in range."],
                "outcome": "The range framing persists.",
                "invalidation": ["A verified break develops."],
            }],
            "option_context": "The listed contracts are non-actionable references.",
        }
        second = dict(first)
        second["ticker"] = "ARM"
        second["research_rank"] = 1
        run["watchlist"].append(second)
        normalized = dashboard.normalize_run(run)
        self.assertEqual(["ARM", "QCOM"], [item["ticker"] for item in normalized["entries"]])
        entry = normalized["entries"][1]
        self.assertTrue(entry["analysis"]["validated"])
        self.assertEqual(74, entry["analysis"]["priority"])
        self.assertEqual(["Price remains below EMA20."], entry["claims"])
        self.assertEqual("BASE", entry["analysis"]["scenarios"][0]["name"])

    def test_cli_writes_fragment_from_json_only(self) -> None:
        import json
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "run.json"
            destination = Path(temp) / "private" / "dashboard.html"
            source.write_text(json.dumps(sample()), encoding="utf-8")
            self.assertEqual(0, dashboard.main(["--input", str(source), "--output", str(destination)]))
            self.assertIn("Codex Screener", destination.read_text(encoding="utf-8"))
            self.assertEqual(0o600, stat.S_IMODE(destination.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(destination.parent.stat().st_mode))

    def test_enhanced_sidecar_is_normalized_and_rendered(self) -> None:
        import json
        enhanced = {
            "generated_at": "2026-08-24T13:00:00Z",
            "contexts": {
                "market_tide": {"provider_date": "2026-08-24", "call_minus_put_premium": 1250000},
                "sector_tide_technology": {"provider_date": "2026-08-24", "call_minus_put_premium": -500000},
            },
            "symbols": {
                "QCOM": {
                    "sources": {"greek_exposure": 1, "greek_flow": 2, "volatility": 3},
                    "greek_exposure": {
                        "provider_date": "2026-08-24",
                        "near_spot_regime": "positive",
                        "near_spot_net_gex_5pct": 1200000,
                        "gex_concentration": 0.42,
                        "strongest_positive_gex_strike": 165,
                        "strongest_negative_gex_strike": 155,
                    },
                    "greek_flow": {
                        "final_timestamp": "2026-08-24T12:55:00Z",
                        "directional_delta_flow": 450000,
                        "directional_vega_flow": 12000,
                        "otm_delta_share": 0.61,
                    },
                    "volatility": {"provider_date": "2026-08-24", "iv": 0.33, "realized_volatility": 0.27},
                    "dark_pool_levels": {"dominant_price": 159.5, "dark_share_at_reported_levels": 0.47},
                    "short_crowding": {"short_interest_float": 0.024, "days_to_cover": 1.7},
                }
            },
        }
        normalized_run = sample()
        normalized_run["enhanced_summary"] = enhanced
        normalized = dashboard.normalize_run(normalized_run)
        self.assertTrue(normalized["entries"][0]["whale"]["available"])
        self.assertEqual("POSITIVE", normalized["entries"][0]["whale"]["gex"]["regime"])
        self.assertEqual(1250000, normalized["contexts"]["market"]["callMinusPut"])

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "run.json"
            sidecar = Path(temp) / "enhanced.json"
            destination = Path(temp) / "dashboard.html"
            source.write_text(json.dumps(sample()), encoding="utf-8")
            sidecar.write_text(json.dumps(enhanced), encoding="utf-8")
            self.assertEqual(0, dashboard.main([
                "--input", str(source),
                "--enhanced-input", str(sidecar),
                "--output", str(destination),
            ]))
            fragment = destination.read_text(encoding="utf-8")
            script = inline_script(fragment)
            self.assertIn('"regime":"POSITIVE"', script)
            self.assertIn("Native Unusual Whales evidence", script)
        self.assertIn("history varies by feature", script)
        self.assertIn('"alerts":null', script)
        self.assertIn('"historySessions":null', script)

    def test_evaluation_summary_is_bounded_and_visible(self) -> None:
        run = sample()
        run["model_evaluation"] = {
            "status": "INSUFFICIENT_OUT_OF_SAMPLE_HISTORY",
            "calibrated": False,
            "registered_forecasts": 8,
            "evaluated_forecasts": 1,
            "pending_forecasts": 7,
            "paper_option_method": "stored ask to later stored bid",
            "horizons": {
                "1": {
                    "registered": 2, "evaluated": 1, "pending": 1,
                    "direction_evaluable": 1, "direction_accuracy": 0.0,
                    "majority_direction_baseline": 1.0,
                    "accuracy_lift_vs_majority": -1.0,
                    "median_absolute_center_error_pct_points": 3.2,
                    "range_coverage": 1.0, "option_evaluated": 1,
                    "median_option_return_pct": -12.0,
                    "distinct_origin_sessions": 1, "sample_gate_passed": False,
                }
            },
            "rows": [{
                "ticker": "QCOM", "origin_session": "2026-08-20",
                "target_session": "2026-08-21", "horizon_sessions": 1,
                "direction": "BULLISH", "status": "EVALUATED",
                "underlying_return_pct": -1.0, "direction_correct": False,
                "absolute_center_error_pct_points": 3.2,
                "range_covered": True, "option_return_pct": -12.0,
                "option_available": True,
            }],
        }
        normalized = dashboard.normalize_run(run)
        evaluation = normalized["evaluation"]
        self.assertTrue(evaluation["available"])
        self.assertEqual(1, evaluation["evaluated"])
        self.assertEqual(0.0, evaluation["horizons"]["1"]["accuracy"])
        self.assertEqual("QCOM", evaluation["recentRows"][0]["ticker"])


if __name__ == "__main__":
    unittest.main()
