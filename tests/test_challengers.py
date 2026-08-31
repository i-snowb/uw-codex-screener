from __future__ import annotations

from datetime import date, timedelta
import math
import unittest

from morning_edge.challengers import shadow_challengers


class ChallengerTests(unittest.TestCase):
    def test_suite_is_deterministic_and_shadow_only(self) -> None:
        start = date(2024, 1, 2)
        bars = [
            {"date": (start + timedelta(days=index)).isoformat(),
             "close": 100.0 * math.exp(0.0007 * index + 0.04 * math.sin(index / 13.0))}
            for index in range(360)
            if (start + timedelta(days=index)).weekday() < 5
        ]
        first = shadow_challengers(bars=bars)
        second = shadow_challengers(bars=bars)
        self.assertEqual(first, second)
        self.assertEqual("SHADOW_ONLY", first["status"])
        self.assertFalse(first["promotion_eligible"])
        self.assertGreaterEqual(len(first["models"]), 6)
        logistic = [row for row in first["models"] if row["model_version"].startswith("regularized-logistic")]
        self.assertTrue(logistic)
        self.assertTrue(all(row["raw_score_is_probability"] is False for row in logistic))
        self.assertTrue(all(row["path"][0]["date"] > bars[-1]["date"] for row in first["models"]))

    def test_insufficient_history_fails_closed(self) -> None:
        value = shadow_challengers(bars=[{"date": "2026-08-27", "close": 100}])
        self.assertEqual("INSUFFICIENT_HISTORY", value["status"])
        self.assertFalse(value["promotion_eligible"])
        self.assertEqual([], value["models"])


if __name__ == "__main__":
    unittest.main()
