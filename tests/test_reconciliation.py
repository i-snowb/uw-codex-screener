"""Read-only reconciliation must explain gaps without relaxing eligibility."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from morning_edge.experiments import digest
from morning_edge.models import Dataset, SnapshotEnvelope, timestamp_from_text
from morning_edge.reconciliation import inspect_price_source
from morning_edge.research_audit import load_development_data
from morning_edge.store import SnapshotStore


def bar(day, close=100):
    return {"date": day, "market_time": "r", "open": close, "high": close+1,
            "low": close-1, "close": close, "volume": 1000}


def raw(rows, **metadata):
    payload = json.dumps({"data": rows})
    return {"id": 1, "provider": "test", "content_json": payload,
            "raw_payload_hash": hashlib.sha256(payload.encode()).hexdigest(),
            "as_of": "2026-09-10T12:00:00Z", "retrieved_at": "2026-09-10T12:00:00Z",
            "metadata_json": json.dumps(metadata)}


CUTOFF = timestamp_from_text("2026-09-10T12:57:00Z")


class ReconciliationTests(unittest.TestCase):
    def test_overlap_does_not_grant_scope_and_date_mismatch_is_visible(self):
        rows = [bar("2026-09-08"), bar("2026-09-09")]
        source = raw(rows, backfill_plan_id="plan", requested_market_date="2026-09-08")
        result, valid = inspect_price_source(source, {"2026-09-08": rows[0]}, CUTOFF)
        self.assertTrue(result["scope_excluded"])
        self.assertFalse(result["historical_scope_verified"])
        self.assertEqual(1, result["overlap_rows"])
        self.assertEqual(1, result["rows_after_requested_date"])
        self.assertEqual([], result["close_conflict_dates"])
        self.assertEqual(2, len(valid))

    def test_hash_corruption_and_future_source_have_no_candidates(self):
        source = raw([bar("2026-09-09")])
        source["content_json"] += " "
        result, valid = inspect_price_source(source, {}, CUTOFF)
        self.assertEqual(["PAYLOAD_HASH_MISMATCH"], result["source_issues"])
        self.assertEqual({}, valid)
        source = raw([bar("2026-09-09")])
        source["retrieved_at"] = "2026-09-10T13:00:00Z"
        result, valid = inspect_price_source(source, {}, CUTOFF)
        self.assertEqual(["SOURCE_AFTER_CUTOFF"], result["source_issues"])
        self.assertEqual({}, valid)

    def test_invalid_nonfinite_and_nonregular_rows_not_candidates(self):
        for bad in (None, True, 0, -1, float("inf"), float("nan")):
            result, valid = inspect_price_source(raw([{**bar("2026-09-09"), "close": bad}]), {}, CUTOFF)
            self.assertEqual({}, valid)
            self.assertEqual(1, result["row_issues"]["INVALID_REGULAR_ROW"])
        result, valid = inspect_price_source(raw([bar("2026-09-12"), {**bar("2026-09-09"), "market_time": "po"}]), {}, CUTOFF)
        self.assertEqual({}, valid)
        self.assertEqual({"NON_REGULAR_ROW": 1, "NON_SESSION": 1}, result["row_issues"])

    def test_duplicate_regular_dates_fail_closed_even_if_prices_agree(self):
        result, valid = inspect_price_source(raw([bar("2026-09-09"), bar("2026-09-09")]), {}, CUTOFF)
        self.assertEqual({}, valid)
        self.assertEqual(["2026-09-09"], result["duplicate_dates"])

    def test_field_revisions_are_separate_from_close_conflicts(self):
        source = raw([{**bar("2026-09-09"), "volume": 2000}])
        result, _ = inspect_price_source(source, {"2026-09-09": bar("2026-09-09")}, CUTOFF)
        self.assertEqual([], result["close_conflict_dates"])
        self.assertEqual({"volume": 1}, result["field_conflict_counts"])
        result, _ = inspect_price_source(raw([bar("2026-09-09", 110)]), {"2026-09-09": bar("2026-09-09")}, CUTOFF)
        self.assertEqual(["2026-09-09"], result["close_conflict_dates"])

    def test_partial_daily_bar_is_flagged_by_original_capture_time(self):
        source = raw([bar("2026-09-09")])
        source["as_of"] = source["retrieved_at"] = "2026-09-09T15:00:00Z"
        result, _ = inspect_price_source(source, {}, CUTOFF)
        self.assertEqual(["2026-09-09"], result["not_final_at_capture_dates"])
        source["retrieved_at"] = "2026-09-09T20:16:00Z"
        result, _ = inspect_price_source(source, {}, CUTOFF)
        self.assertEqual([], result["not_final_at_capture_dates"])

    def database_result(self, candidate_closes, *, future_only=False):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"test.sqlite"
            early = timestamp_from_text("2026-09-10T12:00:00Z")
            late = timestamp_from_text("2026-09-10T13:00:00Z")
            store = SnapshotStore(path)
            for ticker in ("AAA", "QQQ"):
                store.insert(SnapshotEnvelope("test", Dataset.OHLC, early, early,
                    {"data": [bar("2026-09-04"), bar("2026-09-09")]}, ticker))
            for close in candidate_closes:
                when = late if future_only else early
                store.insert(SnapshotEnvelope("test", Dataset.OHLC, when, when,
                    {"data": [bar("2026-09-04"), bar("2026-09-08", close), bar("2026-09-09")]}, "QQQ",
                    {"backfill_plan_id": "plan"}))
            store.insert(SnapshotEnvelope("test", Dataset.SECURITY_IDENTITY, early, early,
                {"data": {"symbol": "AAA"}}, "AAA", {"reference_kind": "stock_info"}))
            store.close()
            run = {"cutoff_at": "2026-09-10T12:57:00Z", "watchlist": [{"ticker": "AAA"}]}
            before = path.read_bytes()
            result = load_development_data(path, run, "QQQ", include_reconciliation=True)
            ordinary = load_development_data(path, run, "QQQ")
            repeated = load_development_data(path, run, "QQQ", include_reconciliation=True)
            self.assertEqual(before, path.read_bytes())
            self.assertEqual(result, repeated)
            stripped = copy.deepcopy(result)
            audit = stripped.pop("reconciliation")
            self.assertEqual(ordinary, stripped)
            self.assertEqual(digest(ordinary), audit["dataset_sha256"])
            self.assertEqual(0, audit["automatic_repairs"])
            self.assertFalse(audit["promotion_eligible"])
            return result

    def test_stored_candidate_explained_but_not_inserted_and_inventory_is_not_verification(self):
        data = self.database_result([100])
        aaa, qqq = data["reconciliation"]["tickers"]
        self.assertEqual("CAPTURED_NOT_RECONCILED", aaa["identity_status"])
        self.assertEqual("NOT_CAPTURED_FOR_SYMBOL", aaa["corporate_action_status"])
        self.assertEqual("NO_VALID_CUTOFF_SAFE_RAW_CANDIDATE", aaa["gaps"][0]["status"])
        self.assertEqual("RAW_CANDIDATE_REQUIRES_REVIEW", qqq["gaps"][0]["status"])
        candidate = qqq["gaps"][0]["candidates"][0]
        self.assertIn("HISTORICAL_SCOPE_UNVERIFIED", candidate["blockers"])
        self.assertFalse(candidate["eligible_for_automatic_repair"])
        self.assertEqual(2, len(data["series"]["QQQ"]))

    def test_disagreeing_gap_candidates_are_quarantined(self):
        data = self.database_result([100, 110])
        gap = data["reconciliation"]["tickers"][1]["gaps"][0]
        self.assertEqual("CONFLICTING_RAW_CANDIDATES", gap["status"])
        self.assertEqual(2, gap["distinct_payloads"])

    def test_future_candidate_cannot_fill_a_historical_gap(self):
        data = self.database_result([100], future_only=True)
        gap = data["reconciliation"]["tickers"][1]["gaps"][0]
        self.assertEqual("NO_VALID_CUTOFF_SAFE_RAW_CANDIDATE", gap["status"])
        self.assertEqual([], gap["candidates"])


if __name__ == "__main__":
    unittest.main()
