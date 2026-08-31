"""Append-only SQLite storage for provider-neutral market snapshots."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import time
from typing import Iterator, Sequence

from .models import (
    SnapshotEnvelope,
    StoredSnapshot,
    canonical_json,
    timestamp_from_text,
    timestamp_text,
)


class SnapshotStore:
    """A local append-only store with deduplicated raw payloads.

    The public API deliberately omits update and delete operations. SQLite
    triggers enforce that same policy for callers with direct database access.
    """

    # Keep startup contention finite. A second worker can wait briefly for the
    # first to establish WAL/schema state, but a stuck writer remains an error
    # instead of an unbounded startup hang.
    BUSY_TIMEOUT_MS = 750
    INITIALIZATION_ATTEMPTS = 5
    INITIALIZATION_RETRY_SECONDS = 0.05
    OWNER_DIRECTORY_MODE = 0o700
    OWNER_FILE_MODE = 0o600

    def __init__(self, database: str | Path) -> None:
        self.path = Path(database)
        self._prepare_parent()
        self._connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=self.BUSY_TIMEOUT_MS / 1_000,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._initialize_connection()
        except BaseException:
            self._connection.close()
            raise

    def _prepare_parent(self) -> None:
        """Create or tighten the task-owned database parent directory."""

        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
            mode=self.OWNER_DIRECTORY_MODE,
        )
        parent_stat = self.path.parent.stat()
        if parent_stat.st_uid == os.geteuid():
            os.chmod(self.path.parent, self.OWNER_DIRECTORY_MODE)

    def _initialize_connection(self) -> None:
        """Set durable SQLite state, retrying only short-lived lock contention."""

        for attempt in range(self.INITIALIZATION_ATTEMPTS):
            try:
                self._connection.execute(f"PRAGMA busy_timeout = {self.BUSY_TIMEOUT_MS}")
                self._connection.execute("PRAGMA foreign_keys = ON")
                journal_mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()
                if journal_mode is None or str(journal_mode[0]).lower() != "wal":
                    raise sqlite3.OperationalError("SQLite refused WAL journal mode")
                self._connection.execute("PRAGMA synchronous = FULL")
                self._create_schema()
                self._harden_database_files()
                return
            except sqlite3.OperationalError as error:
                if not self._is_transient_lock(error) or attempt + 1 >= self.INITIALIZATION_ATTEMPTS:
                    raise
                time.sleep(self.INITIALIZATION_RETRY_SECONDS * (attempt + 1))

    @staticmethod
    def _is_transient_lock(error: sqlite3.OperationalError) -> bool:
        message = str(error).lower()
        return "database is locked" in message or "database is busy" in message

    def _harden_database_files(self) -> None:
        """Make database artifacts owner-private when this process owns them."""

        for candidate in (self.path, self.path.with_name(f"{self.path.name}-wal"), self.path.with_name(f"{self.path.name}-shm")):
            try:
                stat_result = candidate.stat()
            except FileNotFoundError:
                continue
            if stat_result.st_uid != os.geteuid():
                raise PermissionError(f"refusing to change permissions on non-owned database artifact: {candidate}")
            os.chmod(candidate, self.OWNER_FILE_MODE)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SnapshotStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose a read connection for audit queries, not mutation."""

        return self._connection

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS raw_payloads (
                content_hash TEXT PRIMARY KEY CHECK(length(content_hash) = 64),
                content_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key) = 64),
                provider TEXT NOT NULL,
                dataset TEXT NOT NULL,
                symbol TEXT,
                as_of TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
                raw_payload_hash TEXT NOT NULL REFERENCES raw_payloads(content_hash),
                metadata_json TEXT NOT NULL,
                inserted_at TEXT NOT NULL,
                CHECK(as_of <= retrieved_at)
            );
            CREATE INDEX IF NOT EXISTS snapshots_lookup
                ON snapshots (symbol, dataset, as_of DESC, retrieved_at DESC);
            CREATE INDEX IF NOT EXISTS snapshots_provider_lookup
                ON snapshots (provider, dataset, as_of DESC);

            CREATE TRIGGER IF NOT EXISTS snapshots_no_update
            BEFORE UPDATE ON snapshots BEGIN
                SELECT RAISE(ABORT, 'snapshots are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS snapshots_no_delete
            BEFORE DELETE ON snapshots BEGIN
                SELECT RAISE(ABORT, 'snapshots are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS raw_payloads_no_update
            BEFORE UPDATE ON raw_payloads BEGIN
                SELECT RAISE(ABORT, 'raw payloads are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS raw_payloads_no_delete
            BEFORE DELETE ON raw_payloads BEGIN
                SELECT RAISE(ABORT, 'raw payloads are immutable');
            END;
            """
        )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")
            self._harden_database_files()

    def insert(self, envelope: SnapshotEnvelope) -> StoredSnapshot:
        """Persist one snapshot or return its existing immutable record."""

        return self.insert_many((envelope,))[0]

    def insert_many(self, envelopes: Sequence[SnapshotEnvelope]) -> list[StoredSnapshot]:
        """Persist a batch atomically, preserving order and idempotency.

        Envelopes are intentionally normalized during the transaction. If a
        later item is invalid, earlier inserts are rolled back as well.
        """

        if not envelopes:
            return []
        with self._transaction() as connection:
            records = [self._insert_one(connection, envelope) for envelope in envelopes]
        return records

    def _insert_one(
        self, connection: sqlite3.Connection, envelope: SnapshotEnvelope
    ) -> StoredSnapshot:
        if not isinstance(envelope, SnapshotEnvelope):
            raise TypeError("envelope must be a SnapshotEnvelope")
        now = timestamp_text(datetime.now(timezone.utc))
        payload_json = canonical_json(envelope.payload)
        metadata_json = canonical_json(dict(envelope.metadata))
        raw_hash = envelope.raw_payload_hash
        connection.execute(
            """
            INSERT INTO raw_payloads (content_hash, content_json, first_seen_at)
            VALUES (?, ?, ?)
            ON CONFLICT(content_hash) DO NOTHING
            """,
            (raw_hash, payload_json, now),
        )
        connection.execute(
            """
            INSERT INTO snapshots (
                idempotency_key, provider, dataset, symbol, as_of, retrieved_at,
                schema_version, raw_payload_hash, metadata_json, inserted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                envelope.idempotency_key,
                envelope.provider,
                envelope.dataset.value,
                envelope.symbol,
                timestamp_text(envelope.as_of),
                timestamp_text(envelope.retrieved_at),
                envelope.schema_version,
                raw_hash,
                metadata_json,
                now,
            ),
        )
        row = connection.execute(
            """
            SELECT s.*, p.content_json
            FROM snapshots AS s
            JOIN raw_payloads AS p ON p.content_hash = s.raw_payload_hash
            WHERE s.idempotency_key = ?
            """,
            (envelope.idempotency_key,),
        ).fetchone()
        if row is None:  # pragma: no cover - defensive database corruption check
            raise RuntimeError("snapshot insert did not return a record")
        return self._row_to_snapshot(row)

    def get(self, snapshot_id: int) -> StoredSnapshot | None:
        row = self._connection.execute(
            """
            SELECT s.*, p.content_json
            FROM snapshots AS s
            JOIN raw_payloads AS p ON p.content_hash = s.raw_payload_hash
            WHERE s.id = ?
            """,
            (snapshot_id,),
        ).fetchone()
        return self._row_to_snapshot(row) if row else None

    def list(
        self,
        *,
        symbol: str | None = None,
        dataset: str | None = None,
        limit: int = 100,
    ) -> list[StoredSnapshot]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        clauses: list[str] = []
        params: list[object] = []
        if symbol is not None:
            clauses.append("s.symbol = ?")
            params.append(symbol.strip().upper())
        if dataset is not None:
            clauses.append("s.dataset = ?")
            params.append(dataset)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._connection.execute(
            f"""
            SELECT s.*, p.content_json
            FROM snapshots AS s
            JOIN raw_payloads AS p ON p.content_hash = s.raw_payload_hash
            {where}
            ORDER BY s.as_of DESC, s.retrieved_at DESC, s.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._row_to_snapshot(row) for row in rows]

    def count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])

    @staticmethod
    def _row_to_snapshot(row: sqlite3.Row) -> StoredSnapshot:
        import json

        envelope = SnapshotEnvelope(
            provider=row["provider"],
            dataset=row["dataset"],
            symbol=row["symbol"],
            as_of=timestamp_from_text(row["as_of"]),
            retrieved_at=timestamp_from_text(row["retrieved_at"]),
            payload=json.loads(row["content_json"]),
            metadata=json.loads(row["metadata_json"]),
            schema_version=row["schema_version"],
        )
        return StoredSnapshot(
            id=row["id"],
            envelope=envelope,
            inserted_at=timestamp_from_text(row["inserted_at"]),
        )
