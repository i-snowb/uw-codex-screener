from __future__ import annotations

from datetime import UTC, datetime, timedelta
import multiprocessing
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest

from morning_edge.providers.base import SafeGetClient
from morning_edge.providers.budget import ProviderRequestBudgetExceeded, WeeklyRequestBudget


def _reserve_in_process(path: str, clock_text: str, start: multiprocessing.synchronize.Event) -> None:
    start.wait(10)
    with WeeklyRequestBudget(
        path, weekly_cap=5, protected_reserve=2,
        clock=lambda: datetime.fromisoformat(clock_text),
    ) as budget:
        budget.reserve_attempt()


class SequenceTransport:
    def __init__(self, responses: list[tuple[int, dict[str, str], bytes]]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, _url: str, _headers: dict[str, str], _timeout: float) -> tuple[int, dict[str, str], bytes]:
        self.calls += 1
        return self.responses.pop(0)


class WeeklyRequestBudgetTests(unittest.TestCase):
    def test_custom_rolling_window_supports_daily_api_policy(self) -> None:
        path = Path(self.directory.name) / "daily.sqlite"
        with WeeklyRequestBudget(
            path,
            weekly_cap=40_000,
            protected_reserve=5_000,
            rolling_window=timedelta(days=1),
            clock=lambda: self.now,
        ) as budget:
            usage = budget.usage()
        self.assertEqual(40_000, usage.public_dict()["window_cap"])
        self.assertTrue(usage.public_dict()["window_rule"].startswith("Trailing 24-hour"))

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "usage.sqlite"
        self.now = datetime(2026, 8, 24, 12, tzinfo=UTC)  # Monday UTC

    def tearDown(self) -> None:
        self.directory.cleanup()

    def budget(self, *, now: datetime | None = None) -> WeeklyRequestBudget:
        return WeeklyRequestBudget(
            self.path,
            weekly_cap=5,
            protected_reserve=2,
            clock=lambda: now or self.now,
        )

    def test_blocks_before_consuming_protected_reserve_and_persists_attempts(self) -> None:
        with self.budget() as budget:
            for _ in range(3):
                budget.reserve_attempt()
            usage = budget.usage()
            self.assertEqual(3, usage.attempted_requests)
            self.assertEqual(0, usage.remaining_before_reserve)
            with self.assertRaises(ProviderRequestBudgetExceeded):
                budget.reserve_attempt()

        with self.budget() as reopened:
            self.assertEqual(3, reopened.usage().attempted_requests)

    def test_counts_every_retry_attempt_and_prevents_transport_call_after_block(self) -> None:
        transport = SequenceTransport([
            (500, {}, b'{"data":[]}'),
            (200, {}, b'{"data":[]}'),
            (200, {}, b'{"data":[]}'),
        ])
        with self.budget() as budget:
            client = SafeGetClient(
                provider="unusual_whales",
                authorization="Bearer local-test-token",
                base_url="https://example.test",
                transport=transport,
                attempt_budget=budget,
                sleep=lambda _delay: None,
                random_float=lambda: 0.5,
            )
            client.get_json("/retry")
            self.assertEqual(2, transport.calls)
            self.assertEqual(2, budget.usage().attempted_requests)

            # Third request consumes the final usable slot. The next request is
            # rejected before its transport is invoked.
            client.get_json("/last-usable")
            self.assertEqual(3, transport.calls)
            with self.assertRaises(ProviderRequestBudgetExceeded):
                client.get_json("/blocked")
            self.assertEqual(3, transport.calls)

    def test_trailing_seven_day_window_expires_without_calendar_reset(self) -> None:
        with self.budget(now=datetime(2026, 8, 24, 12, tzinfo=UTC)) as original:
            original.reserve_attempt()
        with self.budget(now=datetime(2026, 8, 31, 11, 59, tzinfo=UTC)) as retained:
            self.assertEqual(1, retained.usage().attempted_requests)
        with self.budget(now=datetime(2026, 8, 31, 12, 0, 1, tzinfo=UTC)) as expired:
            usage = expired.usage()
            self.assertEqual(0, usage.attempted_requests)
            self.assertTrue(usage.public_dict()["window_rule"].startswith("Trailing 7 x 24-hour"))

    def test_baseline_adjustment_is_idempotent_and_counts_against_reserve(self) -> None:
        with self.budget() as budget:
            first = budget.add_baseline_adjustment(
                adjustment_id="prior-live-capture",
                attempted_requests=2,
                evidence_id="snapshot-raw-manual-v1",
                effective_at=self.now - timedelta(minutes=1),
            )
            replay = budget.add_baseline_adjustment(
                adjustment_id="prior-live-capture",
                attempted_requests=2,
                evidence_id="snapshot-raw-manual-v1",
                effective_at=self.now - timedelta(minutes=1),
            )
            self.assertEqual(2, first.adjustment_attempts)
            self.assertEqual(first, replay)
            budget.reserve_attempt()
            with self.assertRaises(ProviderRequestBudgetExceeded):
                budget.reserve_attempt()
            with self.assertRaisesRegex(ValueError, "different immutable"):
                budget.add_baseline_adjustment(
                    adjustment_id="prior-live-capture",
                    attempted_requests=1,
                    evidence_id="snapshot-raw-manual-v1",
                    effective_at=self.now - timedelta(minutes=1),
                )
            with self.assertRaisesRegex(ValueError, "future"):
                budget.add_baseline_adjustment(
                    adjustment_id="future-evidence",
                    attempted_requests=1,
                    evidence_id="snapshot-raw-manual-v1",
                    effective_at=self.now + timedelta(seconds=1),
                )

    def test_ledger_contains_no_authorization_or_request_url(self) -> None:
        with self.budget() as budget:
            budget.reserve_attempt()
        text = self.path.read_bytes()
        self.assertNotIn(b"Bearer", text)
        self.assertNotIn(b"https://", text)
        with sqlite3.connect(self.path) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(provider_request_attempts)")
            }
        self.assertEqual({"id", "provider", "window_start_utc", "attempted_at_utc"}, columns)
        with sqlite3.connect(self.path) as connection:
            adjustment_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(provider_request_adjustments)")
            }
        self.assertEqual(
            {"adjustment_id", "provider", "attempted_requests", "effective_at_utc", "evidence_id", "inserted_at_utc"},
            adjustment_columns,
        )

    def test_owner_private_database_directory_and_sidecars(self) -> None:
        with self.budget() as budget:
            budget.reserve_attempt()
            budget.usage()
            paths = [self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")]
            for candidate in paths:
                if candidate.exists():
                    self.assertEqual(0o600, stat.S_IMODE(candidate.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(self.path.parent.stat().st_mode))

    def test_concurrent_processes_cannot_cross_reserve(self) -> None:
        context = multiprocessing.get_context("spawn")
        for index in range(3):
            path = self.path.with_name(f"concurrent-{index}.sqlite")
            start = context.Event()
            processes = [
                context.Process(
                    target=_reserve_in_process,
                    args=(str(path), self.now.isoformat(), start),
                )
                for _ in range(3)
            ]
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(10)
                self.assertFalse(process.is_alive())
            self.assertTrue(all(process.exitcode == 0 for process in processes))
            with WeeklyRequestBudget(
                path, weekly_cap=5, protected_reserve=2, clock=lambda: self.now,
            ) as budget:
                self.assertEqual(3, budget.usage().attempted_requests)


if __name__ == "__main__":
    unittest.main()
