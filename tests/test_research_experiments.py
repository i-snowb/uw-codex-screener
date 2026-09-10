"""Leakage, cohort, determinism and read-only regressions for offline research."""
import copy
from datetime import date
import json
from math import exp, sin
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from morning_edge.experiments import (
    ExperimentPlan, MARKET_FEATURES, PRICE_FEATURES, digest, fit_ridge,
    make_examples, paired_metrics, run_experiment, sessions,
)
from morning_edge.research_audit import audit_inputs, frozen_cohorts, load_development_data, read_only
from morning_edge.evaluation import EVALUATION_VERSION
from morning_edge.models import Dataset, SnapshotEnvelope, timestamp_from_text
from morning_edge.store import SnapshotStore


def series():
    dates = sessions(date(2023, 1, 3), date(2025, 6, 30))
    return {ticker: [{"date": day, "close": 100*exp(.0003*i+.03*sin(i/9+offset))}
                     for i, day in enumerate(dates)]
            for ticker, offset in (("QQQ", 0), ("AAA", 1), ("BBB", 2))}


def examples():
    return make_examples(series(), "QQQ", (1, 5, 20))[0]


class ExperimentTests(unittest.TestCase):
    def test_plan_rejects_invalid_values(self):
        for kwargs in ({"horizons": (0,)}, {"horizons": (True,)}, {"horizons": (1, 1)},
                       {"holdout_origins": 0}, {"test_origins": 0}, {"ridge_penalty": float("nan")},
                       {"ridge_penalty": 0}):
            with self.assertRaises(ValueError):
                ExperimentPlan(**kwargs)

    def test_missing_sessions_are_not_compressed_into_labels(self):
        data = series()
        missing = data["AAA"][150]["date"]
        data["AAA"].pop(150)
        rows, excluded = make_examples(data, "QQQ", (1, 5))
        self.assertGreater(excluded["missing_feature_window"], 0)
        self.assertGreater(excluded["missing_target_window"], 0)
        self.assertFalse(any(row["ticker"] == "AAA" and row["origin"] < missing <= row["target"] for row in rows))
        for row in rows:
            self.assertEqual(row["horizon"]+1, len(sessions(date.fromisoformat(row["origin"]), date.fromisoformat(row["target"]))))

    def test_invalid_or_duplicate_bars_fail_closed(self):
        for invalid in (True, None, 0, -1, float("inf")):
            data = series()
            data["AAA"][30]["close"] = invalid
            with self.assertRaises(ValueError):
                make_examples(data, "QQQ", (1,))
        data = series()
        data["AAA"].append(dict(data["AAA"][30]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            make_examples(data, "QQQ", (1,))

    def test_features_do_not_read_future_prices(self):
        data = series()
        old, _ = make_examples(data, "QQQ", (1,))
        boundary = data["AAA"][300]["date"]
        for row in data["AAA"][301:]:
            row["close"] *= 2
        new, _ = make_examples(data, "QQQ", (1,))
        self.assertEqual([(r["origin"], r["features"]) for r in old if r["origin"] <= boundary],
                         [(r["origin"], r["features"]) for r in new if r["origin"] <= boundary])

    def test_deterministic_date_folds_purge_overlapping_labels(self):
        rows = examples()
        plan = ExperimentPlan()
        first = run_experiment(rows, plan, source_digest="fixed")
        second = run_experiment(list(reversed(rows)), plan, source_digest="fixed")
        self.assertEqual(first, second)
        self.assertFalse(first["promotion_eligible"])
        self.assertGreater(len(first["folds"]), 0)
        holdout = first["holdout"]["start"]
        for fold in first["folds"]:
            self.assertLess(fold["latest_training_target"], fold["test_start"])
        for row in first["predictions"]:
            self.assertLess(row["origin"], holdout)
            self.assertLess(row["target"], holdout)
        self.assertEqual("SEALED_NOT_SCORED", first["holdout"]["status"])

    def test_future_and_holdout_labels_cannot_change_earlier_fits(self):
        rows = examples()
        first = run_experiment(rows, ExperimentPlan(), source_digest="fixed")
        fold = first["folds"][0]
        changed = copy.deepcopy(rows)
        for row in changed:
            if row["origin"] >= fold["test_start"]:
                row["absolute_return"] += 100
                row["excess_return"] -= 100
        second = run_experiment(changed, ExperimentPlan(), source_digest="fixed")
        self.assertEqual(first["folds"][0], second["folds"][0])
        before = [r["predictions"] for r in first["predictions"] if r["fold"] == fold["id"]]
        after = [r["predictions"] for r in second["predictions"] if r["fold"] == fold["id"]]
        self.assertEqual(before, after)
        held = copy.deepcopy(rows)
        for row in held:
            if row["target"] >= first["holdout"]["start"]:
                row["absolute_return"] += 100
                row["excess_return"] -= 100
        self.assertEqual(first, run_experiment(held, ExperimentPlan(), source_digest="fixed"))

    def test_scaling_is_training_only_and_origin_weighted(self):
        rows = examples()[:4]
        model = fit_ridge(rows, PRICE_FEATURES, "absolute_return", 1)
        dates = sorted({row["origin"] for row in rows})
        expected = sum(sum(row["features"]["return_5"] for row in rows if row["origin"] == day)
                       /sum(row["origin"] == day for row in rows) for day in dates)/len(dates)
        self.assertAlmostEqual(expected, model["means"][0])
        self.assertEqual(digest(rows), model["training_digest"])
        self.assertEqual(len(PRICE_FEATURES)+1, len(model["coefficients"]))

    def test_no_relative_signal_reduces_to_price_model(self):
        rows = examples()[:100]
        for row in rows:
            row["features"]["excess_5"] = row["features"]["excess_20"] = 0
        price = fit_ridge(rows, PRICE_FEATURES, "absolute_return", 1)
        market = fit_ridge(rows, MARKET_FEATURES, "absolute_return", 1)
        for a, b in zip(price["coefficients"], market["coefficients"]):
            self.assertAlmostEqual(a, b)
        self.assertEqual([0, 0], market["coefficients"][-2:])

    def test_pairing_does_not_compare_different_rows_or_count_abstentions(self):
        def row(day, ticker, actual, a, b):
            return {"origin": day, "ticker": ticker, "target": "2026-09-10", "horizon": 1,
                    "actual": actual, "predictions": {"a": a, "b": b}}
        rows = [row("2026-09-08", "AAA", .1, .2, .05),
                row("2026-09-08", "BBB", -.1, -.2, .05),
                row("2026-09-09", "AAA", .1, -.2, .05),
                row("2026-09-09", "BBB", .1, None, .05),
                row("2026-09-09", "CCC", .1, 0, .05)]
        result = paired_metrics(rows, "a", "b")
        self.assertEqual(4, result["matched_rows"])
        self.assertEqual(3, result["direction_rows"])
        self.assertEqual(.6, result["paired_direction_coverage"])
        self.assertEqual(4, result["candidate_prediction_rows"])
        self.assertEqual(5, result["baseline_prediction_rows"])
        self.assertEqual(1, result["matched_coverage_of_candidate"])
        self.assertEqual(.75, result["direction_coverage_of_matched"])
        self.assertEqual(.5, result["candidate_accuracy"])
        self.assertEqual(.75, result["baseline_accuracy"])
        self.assertEqual(-.25, result["accuracy_lift"])
        with self.assertRaisesRegex(ValueError, "duplicate paired"):
            paired_metrics(rows+[rows[0]], "a", "b")

    def test_weekend_endpoint_is_not_an_exact_target_session(self):
        row = examples()[0]
        row.update(origin="2026-09-10", target="2026-09-12", horizon=1)
        with self.assertRaisesRegex(ValueError, "exact future"):
            run_experiment([row], ExperimentPlan(), source_digest="fixed")

    def test_small_data_abstains_and_bad_target_rejected(self):
        rows = examples()[:10]
        result = run_experiment(rows, ExperimentPlan(), source_digest="fixed")
        self.assertEqual("INSUFFICIENT_HISTORY", result["status"])
        self.assertEqual([], result["predictions"])
        rows[0]["target"] = rows[0]["origin"]
        with self.assertRaises(ValueError):
            run_experiment(rows, ExperimentPlan(), source_digest="fixed")


class FrozenCohortTests(unittest.TestCase):
    def database(self, path):
        c = sqlite3.connect(path)
        c.executescript("""
        CREATE TABLE forecasts(id INTEGER PRIMARY KEY,ticker TEXT,cutoff_at TEXT,
        horizon_sessions INTEGER,model_version TEXT,scoring_version TEXT,
        metadata_json TEXT,idempotency_key TEXT,feature_hash TEXT);
        CREATE TABLE outcomes(id INTEGER PRIMARY KEY,forecast_id INTEGER,
        underlying_return_pct REAL,metadata_json TEXT);
        """)
        return c

    def add(self, c, ident, model="analog-path-ensemble-v3", ticker="AAA",
            cutoff="2026-09-09T12:00:00Z", direction="BULLISH", mode="PROSPECTIVE",
            target="2026-09-09", outcome=True):
        metadata = {"evaluation_version": EVALUATION_VERSION, "registration_mode": mode,
                    "origin_session": "2026-09-08", "published_target_date": target,
                    "direction_label": direction, "target_center_return": .01,
                    "baseline_directions": {"twenty_session_momentum": "BEARISH"}}
        c.execute("INSERT INTO forecasts VALUES (?,?,?,?,?,?,?,?,?)",
                  (ident, ticker, cutoff, 1, model, "features-v1", json.dumps(metadata), str(ident), "hash"))
        if outcome:
            c.execute("INSERT INTO outcomes VALUES (?,?,?,?)",
                      (ident, ident, 2, json.dumps({"target_session": target})))
        c.commit()

    def test_neutral_excluded_on_both_sides_and_seed_excluded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"test.sqlite"
            c = self.database(path)
            self.add(c, 1)
            self.add(c, 2, ticker="BBB", direction="NEUTRAL")
            self.add(c, 3, ticker="CCC", mode="ARTIFACT_SEED")
            c.close()
            before = path.read_bytes()
            result = frozen_cohorts(path)
            comparison = next(r for r in result["comparisons"] if r["baseline"] == "always_bullish")
            self.assertEqual(1, comparison["direction_rows"])
            self.assertEqual(1, comparison["candidate_accuracy"])
            self.assertEqual(1, comparison["baseline_accuracy"])
            self.assertEqual(before, path.read_bytes())

    def test_earliest_pending_forecast_not_replaced_by_later_resolved(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"test.sqlite"
            c = self.database(path)
            self.add(c, 1, outcome=False)
            self.add(c, 2, cutoff="2026-09-09T13:00:00Z")
            c.close()
            result = frozen_cohorts(path)
            self.assertEqual(0, result["selected_forecasts"])
            self.assertEqual(1, result["rejected_counts"]["later_same_origin_forecast"])

    def test_model_pair_requires_identical_decision_cutoffs(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"test.sqlite"
            c = self.database(path)
            self.add(c, 1)
            self.add(c, 2, model="other", cutoff="2026-09-09T13:00:00Z")
            c.close()
            result = frozen_cohorts(path)
            pair = next(r for r in result["comparisons"] if r["baseline"] == "analog-path-ensemble-v3")
            self.assertEqual(0, pair["matched_rows"])

    def test_invalid_targets_rejected_and_readonly_connection_cannot_write(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"test.sqlite"
            c = self.database(path)
            self.add(c, 1, target="2026-09-10")
            c.close()
            self.assertEqual(1, frozen_cohorts(path)["rejected_counts"]["target_mismatch"])
            c = read_only(path)
            with self.assertRaises(sqlite3.OperationalError):
                c.execute("DELETE FROM forecasts")
            c.close()

    def test_cli_rejects_unacknowledged_development_before_reading_inputs(self):
        script = Path(__file__).resolve().parents[1]/"scripts/run_research_experiments.py"
        result = subprocess.run([sys.executable, str(script), "--input", "absent", "--database",
                                 "absent", "--output", "absent", "--run-development"],
                                capture_output=True, text=True)
        self.assertEqual(2, result.returncode)
        self.assertIn("requires --accept-retrospective-limitations", result.stderr)


class IntegrityAuditTests(unittest.TestCase):
    def test_loader_preserves_cutoff_and_hashes_without_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"test.sqlite"
            early = timestamp_from_text("2026-09-10T12:00:00Z")
            late = timestamp_from_text("2026-09-10T13:00:00Z")
            payload = {"data": [{"date": "2026-09-09", "market_time": "r", "open": 99,
                                 "high": 101, "low": 98, "close": 100, "volume": 1000}]}
            store = SnapshotStore(path)
            for ticker in ("AAA", "QQQ"):
                store.insert(SnapshotEnvelope("test", Dataset.OHLC, early, early, payload, ticker))
            store.insert(SnapshotEnvelope("test", Dataset.OHLC, late, late,
                                          {"data": [{**payload["data"][0], "close": 99}]}, "AAA"))
            store.close()
            run = {"cutoff_at": "2026-09-10T12:57:00Z", "watchlist": [{"ticker": "AAA"}]}
            before = path.read_bytes()
            data = load_development_data(path, run, "QQQ")
            self.assertEqual(100, data["series"]["AAA"][0]["close"])
            self.assertEqual(2, len(data["sources"]))
            self.assertEqual(before, path.read_bytes())
            c = sqlite3.connect(path)
            c.execute("DROP TRIGGER raw_payloads_no_update")
            c.execute("UPDATE raw_payloads SET content_json=content_json || ' '")
            c.commit()
            c.close()
            with self.assertRaisesRegex(ValueError, "integrity failed"):
                load_development_data(path, run, "QQQ")

    def test_empty_price_history_is_invalid(self):
        audit = audit_inputs({"cutoff_at": "2026-09-10T12:00:00Z", "watchlist": []},
                             {"series": {"QQQ": []}})
        self.assertEqual("INVALID_INPUT", audit["status"])

    def test_missing_late_duplicate_and_jump_flags_do_not_repair_prices(self):
        rows = [{"date": "2026-09-08", "close": 100, "available_at": "2026-09-09T00:00:00Z"},
                {"date": "2026-09-10", "close": 200, "available_at": "2026-09-11T00:00:00Z"}]
        data = {"series": {"AAA": rows}}
        run = {"cutoff_at": "2026-09-10T12:00:00Z", "watchlist": []}
        before = copy.deepcopy(data)
        audit = audit_inputs(run, data)
        self.assertEqual("INVALID_INPUT", audit["status"])
        self.assertEqual(["2026-09-09"], audit["prices"][0]["missing_internal_sessions"])
        self.assertEqual(1, len(audit["prices"][0]["large_adjacent_moves"]))
        self.assertEqual("UNRECONCILED", audit["prices"][0]["adjustment_basis"])
        self.assertEqual(before, data)


if __name__ == "__main__":
    unittest.main()
