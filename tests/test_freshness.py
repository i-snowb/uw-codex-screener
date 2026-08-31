import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from morning_edge.freshness import dataset_freshness, latest_complete_session


ET = ZoneInfo("America/New_York")


class FreshnessTests(unittest.TestCase):
    def test_preopen_uses_prior_regular_session_as_complete(self):
        cutoff = datetime(2026, 8, 31, 6, 45, tzinfo=ET)
        self.assertEqual("2026-08-28", latest_complete_session(cutoff).isoformat())

    def test_labels_partial_current_prior_and_stale(self):
        result = dataset_freshness(
            cutoff_at=datetime(2026, 8, 31, 9, 45, tzinfo=ET),
            price_session="2026-08-28",
            dataset_dates={
                "flow": "2026-08-31",
                "oi": "2026-08-28",
                "old": "2026-08-26",
                "missing": None,
            },
        )
        self.assertEqual("CURRENT_COMPLETE", result["datasets"]["price"]["status"])
        self.assertEqual("INTRADAY_PARTIAL", result["datasets"]["flow"]["status"])
        self.assertEqual("CURRENT_COMPLETE", result["datasets"]["oi"]["status"])
        self.assertEqual("STALE", result["datasets"]["old"]["status"])
        self.assertEqual("UNAVAILABLE", result["datasets"]["missing"]["status"])
        self.assertEqual("DEGRADED", result["overall"])


if __name__ == "__main__":
    unittest.main()
