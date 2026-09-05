from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from morning_edge.cli import live_morning_run, main
from morning_edge.config import Settings
from morning_edge.current_collection import CurrentCaptureItem, CurrentCaptureReport, CurrentCaptureStatus, CurrentDataset, collect_current
from morning_edge.edge import EdgeAnalyzer
from morning_edge.evaluation import evaluate_registered, register_run
from morning_edge.ledger import ForecastLedger
from morning_edge.models import Dataset, SnapshotEnvelope
from morning_edge.providers.base import ProviderAuthenticationError, ProviderNetworkError
from morning_edge.providers.budget import WeeklyRequestBudget
from morning_edge.store import SnapshotStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_dashboard_bundle as bundle
import build_research_control_plane as control
from test_evaluation import run


NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)


class CollectionFailureTests(unittest.TestCase):
    def test_shared_cutoff_and_enhanced_failure_gate(self) -> None:
        report = CurrentCaptureReport(NOW.isoformat(), ("QCOM",), (CurrentDataset.OPTION_CHAIN,), True, 3, 33000, (
            CurrentCaptureItem("QCOM", CurrentDataset.OPTION_CHAIN, CurrentCaptureStatus.CAPTURED,
                "/chain", 1, NOW.isoformat(), 1),
        ))
        later = NOW + timedelta(seconds=30)
        enhanced = Mock(preflight_passed=True, generated_at=later.isoformat(), results=(Mock(
            snapshot_id=2, fetched_at=later.isoformat(), status=CurrentCaptureStatus.CAPTURED),))
        enhanced.to_dict.return_value = {"results": []}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings.from_env({"MORNING_EDGE_PROVIDER": "unusual_whales",
                "UNUSUAL_WHALES_API_KEY": "local-test-secret",
                "MORNING_EDGE_PROVIDER_USAGE_DATABASE": str(root / "usage.sqlite")})
            args = dict(tickers=("QCOM",), datasets=(), audit_accepted=True, database_path=root / "source.sqlite", output_path=root / "run.json")
            artifact = {"run_id": "test", "cutoff_at": later.isoformat(), "watchlist": []}
            with patch("morning_edge.cli.collect_current", return_value=report), patch("morning_edge.cli.collect_enhanced", return_value=enhanced), patch("morning_edge.cli.build_morning_run", return_value=artifact) as analyze, patch("morning_edge.cli.build_enhanced_summary", return_value={}) as summarize:
                result = live_morning_run(settings, **args)
                self.assertEqual("morning_run_complete", result["status"])
                self.assertEqual(later, analyze.call_args.kwargs["cutoff_at"])
                self.assertEqual(later, summarize.call_args.kwargs["cutoff_at"])
                enhanced.results[0].status = CurrentCaptureStatus.UNAVAILABLE
                analyze.reset_mock()
                result = live_morning_run(settings, **args)
                self.assertEqual("collection_failed", result["status"])
                analyze.assert_not_called()
            with patch("morning_edge.cli.live_morning_run", return_value=result), patch("morning_edge.cli._json"):
                self.assertEqual(2, main(["morning-run", "--live", "--audit-accepted", "--output", str(root / "failure.json")]))

    def test_shared_transport_and_auth_failures_bound_request_count(self) -> None:
        for error, expected in ((ProviderNetworkError("network unavailable"), 3), (ProviderAuthenticationError("unauthorized"), 1)):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                client = Mock()
                client.option_chain.side_effect = error
                with SnapshotStore(root / "source.sqlite") as store, WeeklyRequestBudget(root / "usage.sqlite", weekly_cap=100, protected_reserve=0) as budget:
                    report = collect_current(client=client, snapshots=store, request_budget=budget,
                        tickers=[f"T{index}" for index in range(8)], datasets=[CurrentDataset.OPTION_CHAIN], generated_at=NOW)
                    self.assertEqual(expected, client.option_chain.call_count)
                    self.assertEqual(8, len(report.results))
                    self.assertEqual(0, store.count())
                    self.assertIn("circuit open", report.results[-1].reason)

    def test_failed_capture_does_not_analyze_or_replace_successful_output(self) -> None:
        report = CurrentCaptureReport(NOW.isoformat(), ("QCOM",), (CurrentDataset.OPTION_CHAIN,), True, 3, 33000, (
            CurrentCaptureItem("QCOM", CurrentDataset.OPTION_CHAIN, CurrentCaptureStatus.UNAVAILABLE,
                "/api/stock/QCOM/option-chains", None, None, None, "network unavailable"),
        ))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "run.json"
            output.write_text("last successful publication")
            settings = Settings.from_env({"MORNING_EDGE_PROVIDER": "unusual_whales",
                "UNUSUAL_WHALES_API_KEY": "local-test-secret",
                "MORNING_EDGE_PROVIDER_USAGE_DATABASE": str(root / "usage.sqlite")})
            with patch("morning_edge.cli.collect_current", return_value=report), patch("morning_edge.cli.collect_enhanced") as enhanced, patch("morning_edge.cli.build_morning_run") as analyze:
                result = live_morning_run(settings, tickers=("QCOM",), datasets=(), audit_accepted=True,
                    database_path=root / "source.sqlite", output_path=output)
            self.assertEqual("collection_failed", result["status"])
            self.assertFalse(result["analysis_started"])
            self.assertIsNone(result["artifact_path"])
            self.assertEqual("last successful publication", output.read_text())
            self.assertTrue(Path(result["diagnostic_path"]).is_file())
            enhanced.assert_not_called()
            analyze.assert_not_called()


class FlowRegressionTests(unittest.TestCase):
    def test_pages_are_merged_deduplicated_and_clipped_to_requested_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "source.sqlite"
            ids = []
            def alert(identity: str, day: str, premium: int) -> dict:
                return {"id": identity, "created_at": day + "T19:00:00Z", "type": "call",
                    "total_premium": premium, "total_ask_side_prem": premium,
                    "has_singleleg": True, "all_opening_trades": True}
            with SnapshotStore(database) as store:
                for page, rows in enumerate((
                    [alert("a", "2026-08-21", 100), alert("b", "2026-08-21", 200)],
                    [alert("b", "2026-08-21", 200), alert("c", "2026-08-21", 300), alert("old", "2026-08-20", 9000)],
                )):
                    ids.append(store.insert(SnapshotEnvelope(provider="test", dataset=Dataset.OPTION_FLOW, symbol="QCOM",
                        as_of=NOW - timedelta(minutes=2), retrieved_at=NOW - timedelta(minutes=1), payload={"data": rows},
                        metadata={"requested_market_date": "2026-08-21", "pagination_family": "flow_alert_cursor",
                            "backfill_plan_id": "plan", "backfill_item_key": "item", "pagination_page": page})).id)
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE backfill_events(id INTEGER PRIMARY KEY, plan_id TEXT, item_key TEXT, state TEXT, details_json TEXT, recorded_at TEXT)")
                connection.execute("INSERT INTO backfill_events VALUES(1,'plan','item','collected',?,?)",
                    (json.dumps({"pages_captured": 2}), NOW.isoformat()))
            with EdgeAnalyzer(database) as analyzer:
                flow = analyzer.flow_conviction("QCOM", NOW)
                self.assertEqual(3, flow["alert_count"])
                self.assertEqual(600, flow["directional_premium"])
                self.assertEqual(ids, flow["source_snapshot_ids"])
                self.assertEqual("COMPLETE_SESSION", flow["coverage_status"])
                before_completion = analyzer.flow_conviction("QCOM", NOW - timedelta(seconds=30))
                self.assertEqual("PARTIAL_OR_UNVERIFIED", before_completion["coverage_status"])
                self.assertIsNone(before_completion["directional_percentile"])

    def test_unverified_empty_is_missing_and_zero_quality_remains_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "source.sqlite"
            with SnapshotStore(database) as store:
                store.insert(SnapshotEnvelope(provider="test", dataset=Dataset.OPTION_FLOW, symbol="QCOM",
                    as_of=NOW, retrieved_at=NOW, payload={"data": []}))
            with EdgeAnalyzer(database) as analyzer:
                flow = analyzer.flow_conviction("QCOM", NOW)
                self.assertIsNone(flow["directional_premium"])
                self.assertEqual("UNAVAILABLE", flow["status"])
            dimensions = EdgeAnalyzer.edge_dimensions(technical={}, surface={}, flow={"directional_share": 1, "quality_multiplier": 0}, gex={}, analogs={}, earnings={}, news={}, as_of_date=NOW.date())
            self.assertEqual(50, dimensions["positioning_context"])


class EvaluationRegressionTests(unittest.TestCase):
    def test_missing_target_does_not_slide_and_direction_is_horizon_specific(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "source.sqlite"
            with SnapshotStore(database) as store:
                source = store.insert(SnapshotEnvelope(provider="test", dataset=Dataset.OHLC, symbol="QCOM",
                    as_of=NOW - timedelta(days=3), retrieved_at=NOW - timedelta(minutes=1), payload={"data": []}))
            origin = run(run_id="origin", cutoff=NOW.isoformat(), price_date="2026-08-21", price=100, source_id=source.id, option_bid=4.8)
            origin["watchlist"][0]["edge"]["forecast"]["path"][0]["center_return"] = -0.02
            register_run(database, origin)
            with self.assertRaisesRegex(ValueError, "cannot be registered as prospective"):
                register_run(database, {**origin, "mode": "RETROSPECTIVE_REPROCESSING"})
            future = run(run_id="later", cutoff="2026-08-26T12:00:00Z", price_date="2026-08-25", price=105, source_id=source.id, option_bid=4.8)
            self.assertEqual({"evaluated": 0, "pending": 4}, evaluate_registered(database, [origin, future]))
            with ForecastLedger(database) as ledger:
                rows = ledger.connection.execute("SELECT id FROM forecasts ORDER BY horizon_sessions").fetchall()
                first = ledger.get_forecast(rows[0][0]).record
                last = ledger.get_forecast(rows[-1][0]).record
                self.assertEqual("BEARISH", first.metadata["direction_label"])
                self.assertEqual("BULLISH", last.metadata["direction_label"])
                self.assertEqual("horizon-center-sign-v1", first.metadata["direction_semantics"])
            exact = run(run_id="exact", cutoff="2026-08-25T12:00:00Z", price_date="2026-08-24", price=98, source_id=source.id, option_bid=4.8)
            self.assertEqual(1, evaluate_registered(database, [origin, exact, future])["evaluated"])


class ProvenanceRegressionTests(unittest.TestCase):
    def test_renderer_rejects_enhanced_evidence_after_cutoff(self) -> None:
        with self.assertRaisesRegex(ValueError, "after the run cutoff"):
            bundle.dashboard.normalize_run({"watchlist": [], "cutoff_at": NOW.isoformat(),
                "enhanced_summary": {"capture_report": {"results": [{"fetched_at": (NOW + timedelta(seconds=1)).isoformat()}]}}})

    def test_availability_uses_all_derived_sources_and_rejects_future_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "source.sqlite"
            with SnapshotStore(database) as store:
                ids = [store.insert(SnapshotEnvelope(provider="test", dataset=Dataset.OHLC, symbol="QCOM",
                    as_of=NOW - timedelta(days=1), retrieved_at=NOW + timedelta(seconds=offset), payload={"data": []})).id for offset in (-30, 30)]
            entry = {"provenance": {"snapshot_ids": [ids[0]]}, "edge": {"source_snapshot_ids": ids}}
            self.assertEqual(set(ids), control._source_ids(entry))
            self.assertEqual(NOW - timedelta(seconds=30), control._source_availability(database, ids[:1], NOW))
            with self.assertRaisesRegex(ValueError, "not available"):
                control._source_availability(database, ids, NOW)
            with self.assertRaisesRegex(ValueError, "missing source"):
                control._source_availability(database, [999], NOW)
            self.assertIn("surface.front_iv", control._feature_values({}))
            measured = control._agent_accountability({"watchlist": [{"ticker": "QCOM", "agent_enrichment_validated": True,
                "agent_enrichment": {"summary": "Actual analyst output", "action": "NO_RECOMMENDATION",
                    "evidence_points": [{"field_refs": ["price.value"], "source_snapshot_ids": [1]}]}}]})
            self.assertEqual("agent_enrichment", measured["rows"][0]["measured_object"])
            self.assertEqual(1, measured["rows"][0]["claim_reference_coverage"])


@unittest.skipUnless(shutil.which("node"), "Node.js required for dashboard behavioral tests")
class DashboardRefreshRegressionTests(unittest.TestCase):
    def test_replay_manifest_polling_same_version_and_inflight_race(self) -> None:
        script = bundle._externalize_data(bundle._split_fragment(bundle.dashboard.build_fragment({"watchlist": []}))[2])
        refresh = re.search(r"async function refreshLatest\(\)\{.*?\n\}", script, re.S).group()
        replay = re.search(r"function renderReplay\(\)\{.*?\n\}", script, re.S).group()
        harness = r"""
const assert=require('node:assert/strict'),{createHash,webcrypto}=require('node:crypto');
const crypto=webcrypto;
let DATA={entries:[{ticker:'QCOM'}],dataVersion:'unchanged',publications:{entries:[]}},selected=0;
let replayActive=false,replaySelection='',navigationEpoch=0,appliedDigest=null,refreshFailures=0,refreshInFlight=false;
const document={hidden:false},select={value:'',innerHTML:'',hidden:true},byId=()=>select,esc=String;
let calls=[],payload=JSON.stringify({entries:[{ticker:'QCOM',value:2}],dataVersion:'unchanged'}),hold=null;
const renderAvailability=()=>{},showRefreshError=()=>{refreshFailures++};
const applyPublication=next=>{DATA={...next,publications:DATA.publications}};
const publicationLoader=async url=>({entries:[{ticker:'QCOM',value:url?'loaded':0}],dataVersion:url});
async function fetch(url){
  calls.push(url);
  if(url.includes('live-status'))return {ok:true,json:async()=>({sha256:createHash('sha256').update(payload).digest('hex')})};
  if(hold)await hold;
  return {ok:true,text:async()=>payload};
}
""" + refresh + "\n" + replay + r"""
(async()=>{
  replayActive=true;await refreshLatest();assert.equal(calls.length,0);
  replayActive=false;await refreshLatest();assert.equal(calls.length,2);assert.equal(DATA.entries[0].value,2);
  refreshFailures=2;await refreshLatest();assert.equal(calls.length,3);assert.equal(refreshFailures,0);
  assert.ok(calls[2].includes('live-status'));
  payload=JSON.stringify({entries:[{ticker:'QCOM',value:3}],dataVersion:'unchanged'});
  let release;hold=new Promise(resolve=>{release=resolve});
  const inflight=refreshLatest();await new Promise(resolve=>setImmediate(resolve));
  replayActive=true;navigationEpoch++;release();await inflight;
  assert.equal(DATA.entries[0].value,2);assert.equal(refreshInFlight,false);
  hold=null;renderReplay();select.value='./archive.json';await select.onchange();
  assert.equal(replayActive,true);assert.equal(replaySelection,'./archive.json');
  const count=calls.length;await refreshLatest();assert.equal(calls.length,count);
  select.value='';await select.onchange();assert.equal(replayActive,false);assert.equal(replaySelection,'');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
