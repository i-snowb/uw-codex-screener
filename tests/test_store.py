from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from morning_edge.models import Dataset, SnapshotEnvelope
from morning_edge.store import SnapshotStore


UTC = timezone.utc
AS_OF = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
RETRIEVED = datetime(2026, 8, 20, 10, 5, tzinfo=UTC)


def quote(*, price: float = 162.55, retrieved_at: datetime = RETRIEVED) -> SnapshotEnvelope:
    return SnapshotEnvelope(
        provider="unusual_whales",
        dataset=Dataset.EQUITY_QUOTE,
        symbol="qcom",
        as_of=AS_OF,
        retrieved_at=retrieved_at,
        payload={"last": price, "bid": price - 0.01},
        metadata={"endpoint": "/stock/QCOM/quote"},
    )


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "snapshots.sqlite3"
        self.store = SnapshotStore(self.database_path)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def test_idempotent_insert_and_raw_payload_deduplication(self) -> None:
        first = self.store.insert(quote())
        second = self.store.insert(quote())
        later_read = self.store.insert(quote(retrieved_at=RETRIEVED + timedelta(minutes=1)))

        self.assertEqual(first.id, second.id)
        self.assertNotEqual(first.id, later_read.id)
        self.assertEqual(self.store.count(), 2)
        payload_count = self.store.connection.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0]
        self.assertEqual(payload_count, 1)

    def test_stored_timestamps_are_explicit_utc_and_keep_distinct_semantics(self) -> None:
        eastern = timezone(timedelta(hours=-4))
        stored = self.store.insert(
            SnapshotEnvelope(
                provider="provider_a",
                dataset="option_chain",
                symbol="ARM",
                as_of=datetime(2026, 8, 20, 9, 30, tzinfo=eastern),
                retrieved_at=datetime(2026, 8, 20, 13, 35, tzinfo=UTC),
                payload={"contracts": []},
            )
        )

        self.assertEqual(stored.envelope.as_of, datetime(2026, 8, 20, 13, 30, tzinfo=UTC))
        self.assertEqual(stored.envelope.retrieved_at, datetime(2026, 8, 20, 13, 35, tzinfo=UTC))
        self.assertNotEqual(stored.envelope.as_of, stored.envelope.retrieved_at)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            SnapshotEnvelope(
                provider="provider_a",
                dataset="option_chain",
                as_of=datetime(2026, 8, 20, 13, 30),
                retrieved_at=RETRIEVED,
                payload={},
            )
        with self.assertRaisesRegex(ValueError, "cannot be later"):
            SnapshotEnvelope(
                provider="provider_a",
                dataset="option_chain",
                as_of=RETRIEVED,
                retrieved_at=AS_OF,
                payload={},
            )

    def test_sqlite_triggers_enforce_immutability(self) -> None:
        stored = self.store.insert(quote())
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.store.connection.execute("UPDATE snapshots SET provider = 'other' WHERE id = ?", (stored.id,))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.store.connection.execute(
                "DELETE FROM raw_payloads WHERE content_hash = ?", (stored.envelope.raw_payload_hash,)
            )
        loaded = self.store.get(stored.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.envelope.provider, "unusual_whales")

    def test_batch_failure_rolls_back_prior_writes(self) -> None:
        invalid = object()
        with self.assertRaises(TypeError):
            self.store.insert_many((quote(), invalid))  # type: ignore[arg-type]
        self.assertEqual(self.store.count(), 0)
        raw_count = self.store.connection.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0]
        self.assertEqual(raw_count, 0)

    def test_database_artifacts_and_owned_parent_are_owner_private(self) -> None:
        os.chmod(self.database_path.parent, 0o755)
        self.store.close()
        self.store = SnapshotStore(self.database_path)
        self.store.insert(quote())

        self.assertEqual(self.database_path.parent.stat().st_mode & 0o777, 0o700)
        for candidate in (
            self.database_path,
            self.database_path.with_name(f"{self.database_path.name}-wal"),
            self.database_path.with_name(f"{self.database_path.name}-shm"),
        ):
            if candidate.exists():
                self.assertEqual(candidate.stat().st_mode & 0o777, 0o600, candidate)

    def test_concurrent_initialization_is_safe(self) -> None:
        self.store.close()
        database_path = self.database_path
        workers = 6
        barrier = threading.Barrier(workers)

        def open_store() -> int:
            barrier.wait(timeout=5)
            with SnapshotStore(database_path) as store:
                return store.count()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            counts = list(executor.map(lambda _: open_store(), range(workers)))

        self.assertEqual(counts, [0] * workers)
        with SnapshotStore(database_path) as store:
            self.assertEqual(store.count(), 0)


if __name__ == "__main__":
    unittest.main()
