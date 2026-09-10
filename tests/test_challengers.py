from __future__ import annotations

from datetime import date, timedelta
import math
import unittest

from morning_edge.challengers import _clean_bars, shadow_challengers
from morning_edge.clock import is_nyse_session
from morning_edge.models import timestamp_from_text


class ChallengerTests(unittest.TestCase):
    def test_suite_is_deterministic_and_shadow_only(self) -> None:
        start = date(2024, 1, 2)
        bars = [
            {"date": (start + timedelta(days=index)).isoformat(),
             "close": 100.0 * math.exp(0.0007 * index + 0.04 * math.sin(index / 13.0))}
            for index in range(360)
            if is_nyse_session(start + timedelta(days=index))
        ]
        first = shadow_challengers(bars=bars)
        second = shadow_challengers(bars=bars)
        self.assertEqual(first, second)
        self.assertEqual(first, shadow_challengers(bars=list(reversed(bars))))
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

    def test_invalid_rows_and_gaps_fail_closed(self):
        good = [{"date": "2026-09-04", "close": 100}, {"date": "2026-09-08", "close": 101}]
        cases = [(good+[good[0]], "DUPLICATE_SESSION"),
                 ([good[0], {"date": "2026-09-09", "close": 101}], "MISSING_INTERNAL_SESSION"),
                 ([{"date": "2026-09-07", "close": 100}], "NON_TRADING_SESSION"),
                 ([{"date": "2026-09-08T00:00:00Z", "close": 100}], "INVALID_SESSION_DATE")]
        cases += [([{**good[0], "close": value}], "INVALID_CLOSE")
                  for value in (None, True, 0, -1, float("nan"), float("inf"))]
        for bars, reason in cases:
            with self.subTest(reason=reason, bars=bars):
                result = shadow_challengers(bars=bars)
                self.assertEqual("INVALID_HISTORY", result["status"])
                self.assertEqual(reason, result["reason"])
                self.assertEqual([], result["models"])
        self.assertEqual([100, 101], _clean_bars(list(reversed(good)))[1])

    def test_cutoff_rejects_incomplete_and_future_sessions(self):
        for day in ("2026-09-10", "2026-09-11"):
            result = shadow_challengers(bars=[{"date": day, "close": 100}],
                cutoff_at=timestamp_from_text("2026-09-10T12:00:00Z"))
            self.assertEqual("FUTURE_OR_INCOMPLETE_SESSION", result["reason"])


if __name__ == "__main__":
    unittest.main()
