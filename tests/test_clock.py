from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from morning_edge.clock import (
    NEW_YORK, RunKind, age_seconds, is_fresh, is_nyse_session,
    next_nyse_session, scheduled_windows,
)


class ClockTests(unittest.TestCase):
    def test_default_windows_are_explicit_eastern_times(self) -> None:
        preopen, open_refresh = scheduled_windows(date(2026, 8, 20))
        self.assertEqual(preopen.kind, RunKind.PREOPEN)
        self.assertEqual((preopen.scheduled_at.hour, preopen.scheduled_at.minute), (6, 55))
        self.assertEqual((open_refresh.scheduled_at.hour, open_refresh.scheduled_at.minute), (9, 40))
        self.assertEqual(preopen.scheduled_at.tzinfo, NEW_YORK)

    def test_weekend_moves_to_monday(self) -> None:
        preopen, _ = scheduled_windows(date(2026, 8, 22))
        self.assertEqual(preopen.scheduled_at.date(), date(2026, 8, 24))

    def test_exchange_holiday_moves_to_next_session(self) -> None:
        preopen, _ = scheduled_windows(date(2026, 12, 25))
        self.assertEqual(preopen.scheduled_at.date(), date(2026, 12, 28))
        self.assertFalse(is_nyse_session(date(2026, 4, 3)))  # Good Friday
        self.assertEqual(next_nyse_session(date(2026, 4, 3)), date(2026, 4, 6))

    def test_juneteenth_is_not_applied_before_exchange_adoption(self) -> None:
        self.assertTrue(is_nyse_session(date(2021, 6, 18)))
        self.assertFalse(is_nyse_session(date(2026, 6, 19)))

    def test_age_normalizes_timezones(self) -> None:
        observed = datetime(2026, 8, 20, 6, 50, tzinfo=NEW_YORK)
        cutoff = datetime(2026, 8, 20, 10, 55, tzinfo=ZoneInfo("UTC"))
        self.assertEqual(age_seconds(observed_at=observed, cutoff_at=cutoff), 300)
        self.assertTrue(is_fresh(observed_at=observed, cutoff_at=cutoff, maximum_age=timedelta(minutes=5)))

    def test_future_observation_is_rejected(self) -> None:
        cutoff = datetime(2026, 8, 20, 6, 55, tzinfo=NEW_YORK)
        with self.assertRaisesRegex(ValueError, "newer than"):
            age_seconds(observed_at=cutoff + timedelta(seconds=1), cutoff_at=cutoff)

    def test_naive_datetime_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone"):
            age_seconds(
                observed_at=datetime(2026, 8, 20, 6, 50),
                cutoff_at=datetime(2026, 8, 20, 6, 55, tzinfo=NEW_YORK),
            )


if __name__ == "__main__":
    unittest.main()
