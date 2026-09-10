"""Offline regressions for dashboard review fixes; no providers or ledgers."""
import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_dashboard_bundle as bundle
from test_enriched_dashboard import sample


def run_at(cutoff):
    run = sample()
    run.update(cutoff_at=cutoff, run_id=cutoff, generated_at=cutoff)
    run["watchlist"][0]["price"] = {"value": 160.75, "as_of": "2026-09-08"}
    run["watchlist"][0]["edge"] = {
        "feature_version": "features-test-v1",
        "forecast": {"model_version": "model-test-v1"},
    }
    return run


class PriorPublicationTests(unittest.TestCase):
    def test_latest_prior_session_is_hash_verified_compact_and_version_safe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = run_at("2026-09-08T12:00:00Z")
            prior = run_at("2026-09-09T12:00:00Z")
            revised = run_at("2026-09-09T20:30:00Z")
            current = run_at("2026-09-10T12:00:00Z")
            for run in (old, prior, revised, current):
                bundle.archive_daily_data(run=run, app_root=root)
            attached = bundle.attach_previous_publication(current, root)
            previous = attached["previous_run"]
            self.assertEqual(revised["cutoff_at"], previous["asOf"])
            self.assertEqual(64, len(previous["sha256"]))
            self.assertNotIn("bars", previous["entries"][0])
            self.assertNotIn("previous_run", current)
            normalized = bundle.dashboard.normalize_run(attached)
            p = normalized["entries"][0]["previous"]
            self.assertTrue(p["comparable"])
            self.assertEqual(previous["sha256"], p["sha256"])
            self.assertEqual(revised["run_id"], p["runId"])

    def test_corrupt_or_outside_archive_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = bundle.archive_daily_data(run=run_at("2026-09-09T12:00:00Z"), app_root=root)
            reference = Path(manifest["files"]["daily_data"]["path"])
            reference.write_bytes(reference.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "integrity"):
                bundle.attach_previous_publication(run_at("2026-09-10T12:00:00Z"), root)
            manifest["files"]["daily_data"]["path"] = str(root / "outside.json")
            (root / "data/2026-09-09/manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "inside dashboard data"):
                bundle.attach_previous_publication(run_at("2026-09-10T12:00:00Z"), root)

    def test_same_day_future_and_non_session_archives_are_not_prior(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for cutoff in ("2026-09-06T12:00:00Z", "2026-09-07T12:00:00Z",
                           "2026-09-08T10:00:00Z", "2026-09-09T12:00:00Z"):
                bundle.archive_daily_data(run=run_at(cutoff), app_root=root)
            result = bundle.attach_previous_publication(run_at("2026-09-08T12:00:00Z"), root)
            self.assertNotIn("previous_run", result)

    def test_explicit_prior_is_preserved(self):
        run = run_at("2026-09-10T12:00:00Z")
        run["previous_run"] = run_at("2026-09-09T12:00:00Z")
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(run, bundle.attach_previous_publication(run, Path(temp)))

    def test_changed_unknown_or_future_versions_do_not_get_deltas(self):
        current = run_at("2026-09-10T12:00:00Z")
        prior = run_at("2026-09-09T12:00:00Z")
        current["previous_run"] = prior
        self.assertTrue(bundle.dashboard.normalize_run(current)["entries"][0]["previous"]["comparable"])
        for key in ("feature_version", "forecast"):
            changed = copy.deepcopy(current)
            changed["previous_run"]["watchlist"][0]["edge"].pop(key)
            self.assertFalse(bundle.dashboard.normalize_run(changed)["entries"][0]["previous"]["comparable"])
        for cutoff in ("2026-09-10T12:00:00Z", "2026-09-11T12:00:00Z", "bad", "2026-09-09T12:00:00"):
            changed = copy.deepcopy(current)
            changed["previous_run"]["cutoff_at"] = cutoff
            self.assertFalse(bundle.dashboard.normalize_run(changed)["entries"][0]["previous"]["comparable"])
        changed = copy.deepcopy(current)
        changed["previous_run"]["watchlist"][0]["edge"]["forecast"]["model_version"] = "other-model"
        self.assertFalse(bundle.dashboard.normalize_run(changed)["entries"][0]["previous"]["comparable"])


class DashboardReviewTests(unittest.TestCase):
    def test_missing_or_invalid_enhanced_timestamp_is_null_or_valid_fallback(self):
        for stamp in (None, "", "—", "bad", "2026-09-10T12:00:00"):
            run = run_at("2026-09-10T12:00:00Z")
            run["enhanced_summary"] = {"generated_at": stamp}
            self.assertIsNone(bundle.dashboard.normalize_run(run)["enhancedAt"])
            run["enhanced_cutoff_at"] = run["cutoff_at"]
            self.assertEqual(run["cutoff_at"], bundle.dashboard.normalize_run(run)["enhancedAt"])

    def test_focus_selector_targets_root_and_summary_has_primary_slot(self):
        markup, css, script = bundle._split_fragment(bundle.dashboard.build_fragment(sample()))
        for name in ("me-performance", "me-stock-record", "me-details"):
            self.assertIn("#codex-screener.me-focus-mode ." + name, css)
        self.assertNotIn("#codex-screener .me-focus-mode", css)
        self.assertIn("byId('me-outlook').innerHTML=renderAgentSynthesis(e)", script)
        self.assertNotIn("Strongest aligned:", script)

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_calendar_coverage_agent_summary_and_comparison_behavior(self):
        script = bundle._split_fragment(bundle.dashboard.build_fragment(sample()))[2]
        names = ("macroEventRead", "flowEvidence", "signalAgreement", "signalMap",
                 "renderAgentSynthesis", "forecastReferencePrice", "renderScoreChange")
        helpers = "\n".join(line for line in script.splitlines()
                            if any(line.startswith("function " + name + "(") for name in names))
        harness = r"""
const assert=require('node:assert/strict'),n=Number.isFinite;
const esc=x=>String(x).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
const tone=()=>'',ret=x=>(x*100).toFixed(2)+'%',trend=()=> 'Below EMA20 and EMA50';
const newsImpact=()=>({direction:'MIXED'}),timeLabel=String,host={innerHTML:''},byId=()=>host;
const cutoff='2026-09-10T12:00:00Z',event={event:'Upcoming',time:'2026-09-10T14:00:00Z'};
assert.equal(macroEventRead({events:[event]},'—',cutoff).label,'Upcoming');
assert.match(macroEventRead({events:[event]},'bad','bad').label,/cutoff unavailable/);
assert.match(macroEventRead({events:[{time:'bad',event:'Invalid'}]},cutoff).label,/invalid event times/);
const e={price:200,forecastOrigin:{price:100},thesis:{direction:'BULLISH'},edge:{
 flow:{comparisonStatus:'INSUFFICIENT_COMPARABLE_COVERAGE',quality:0,directionalPremium:10,
 history:[{date:'2026-09-08',comparable:true,premium:1},{date:'2026-09-09',comparable:false,premium:2}]},
 analogs:{disposition:'BULLISH'},gex:{regime:'UNKNOWN'},
 forecast:{direction:'BEARISH',path:[{session:1,center:101},{session:5,center:97},{session:20,center:95}]}},
 analysis:{validated:true,posture:'BEARISH_TREND',summary:'<img src=x> Counterevidence remains.'}};
assert.equal(flowEvidence(e).recentComplete,1);
assert.equal(flowEvidence(e).recentTotal,2);
let result=signalAgreement(e);
assert.equal(result.provisional,1);
assert.equal(result.aligned,1);
assert.equal(result.signals.find(row=>row.label==='Option flow').state,'provisional');
assert.match(signalMap(result),/1 provisional/);
e.edge.flow.comparisonStatus='COMPARABLE';
assert.equal(flowEvidence(e).eligible,false);
e.edge.flow.quality=0.5;
assert.equal(flowEvidence(e).eligible,false);
e.edge.flow.coverageStatus='COMPLETE_SESSION';
assert.equal(signalAgreement(e).aligned,2);
const summary=renderAgentSynthesis(e);
assert.match(summary,/Agent interpretation · BEARISH_TREND/);
assert.match(summary,/&lt;img src=x&gt;/);
assert.match(summary,/1.00%/);
assert.match(summary,/-3.00%/);
assert.match(summary,/-5.00%/);
assert.match(summary,/Active V3 model centers · BEARISH/);
e.analysis.validated=false;
assert.match(renderAgentSynthesis(e),/No validated agent interpretation/);
e.previous={comparable:false,reason:'Version changed <unsafe>'};
renderScoreChange(e);
assert.match(host.innerHTML,/cannot be compared/);
assert.match(host.innerHTML,/&lt;unsafe&gt;/);
"""
        result = subprocess.run(["node", "-e", helpers + harness], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
