"""Point-in-time derived-feature and replay records for model research.

Raw provider snapshots remain in :mod:`morning_edge.store`. This module stores
only compact, versioned derived values that were available at a declared model
cutoff. Records are immutable and idempotent so a historical experiment can be
replayed without silently changing its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from .models import canonical_json, payload_digest, timestamp_from_text, timestamp_text, utc_timestamp


FEATURE_MART_SCHEMA = "codex-screener-feature-mart-v1"


@dataclass(frozen=True, slots=True)
class FeatureRecord:
    ticker: str
    effective_session: date
    cutoff_at: datetime
    available_at: datetime
    feature_version: str
    values: Mapping[str, Any]
    source_snapshot_ids: Sequence[int]
    quality_status: str = "VALID"
    missing_reasons: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ticker = self.ticker.strip().upper()
        if not ticker:
            raise ValueError("ticker is required")
        cutoff = utc_timestamp(self.cutoff_at, field_name="cutoff_at")
        available = utc_timestamp(self.available_at, field_name="available_at")
        if available > cutoff:
            raise ValueError("available_at cannot be later than cutoff_at")
        version = self.feature_version.strip()
        if not version:
            raise ValueError("feature_version is required")
        sources = tuple(sorted({int(value) for value in self.source_snapshot_ids if int(value) > 0}))
        if not sources:
            raise ValueError("at least one source snapshot ID is required")
        canonical_json(dict(self.values))
        canonical_json(dict(self.missing_reasons))
        object.__setattr__(self, "ticker", ticker)
        object.__setattr__(self, "cutoff_at", cutoff)
        object.__setattr__(self, "available_at", available)
        object.__setattr__(self, "feature_version", version)
        object.__setattr__(self, "source_snapshot_ids", sources)
        object.__setattr__(self, "values", dict(self.values))
        object.__setattr__(self, "missing_reasons", dict(self.missing_reasons))

    @property
    def feature_digest(self) -> str:
        return payload_digest(dict(self.values))

    @property
    def record_digest(self) -> str:
        return payload_digest({
            "ticker": self.ticker,
            "effective_session": self.effective_session.isoformat(),
            "cutoff_at": timestamp_text(self.cutoff_at),
            "available_at": timestamp_text(self.available_at),
            "feature_version": self.feature_version,
            "values": dict(self.values),
            "source_snapshot_ids": list(self.source_snapshot_ids),
            "quality_status": self.quality_status,
            "missing_reasons": dict(self.missing_reasons),
        })


class FeatureMart:
    OWNER_FILE_MODE = 0o600
    OWNER_DIRECTORY_MODE = 0o700

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=self.OWNER_DIRECTORY_MODE)
        if self.path.parent.stat().st_uid == os.geteuid():
            os.chmod(self.path.parent, self.OWNER_DIRECTORY_MODE)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self._create_schema()
        self._harden()

    def _harden(self) -> None:
        for candidate in (self.path, self.path.with_name(self.path.name + "-wal"), self.path.with_name(self.path.name + "-shm")):
            if candidate.exists() and candidate.stat().st_uid == os.geteuid():
                os.chmod(candidate, self.OWNER_FILE_MODE)

    def _create_schema(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS feature_records (
            id INTEGER PRIMARY KEY,
            record_digest TEXT NOT NULL UNIQUE CHECK(length(record_digest)=64),
            ticker TEXT NOT NULL,
            effective_session TEXT NOT NULL,
            cutoff_at TEXT NOT NULL,
            available_at TEXT NOT NULL,
            feature_version TEXT NOT NULL,
            feature_digest TEXT NOT NULL CHECK(length(feature_digest)=64),
            values_json TEXT NOT NULL,
            source_snapshot_ids_json TEXT NOT NULL,
            quality_status TEXT NOT NULL,
            missing_reasons_json TEXT NOT NULL,
            inserted_at TEXT NOT NULL,
            CHECK(available_at <= cutoff_at)
        );
        CREATE INDEX IF NOT EXISTS feature_records_lookup
          ON feature_records(ticker, effective_session, cutoff_at, feature_version);
        CREATE TABLE IF NOT EXISTS replay_manifests (
            replay_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            cutoff_at TEXT NOT NULL,
            input_digest TEXT NOT NULL CHECK(length(input_digest)=64),
            feature_record_ids_json TEXT NOT NULL,
            model_versions_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS feature_records_no_update BEFORE UPDATE ON feature_records
        BEGIN SELECT RAISE(ABORT, 'feature records are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS feature_records_no_delete BEFORE DELETE ON feature_records
        BEGIN SELECT RAISE(ABORT, 'feature records are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS replay_manifests_no_update BEFORE UPDATE ON replay_manifests
        BEGIN SELECT RAISE(ABORT, 'replay manifests are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS replay_manifests_no_delete BEFORE DELETE ON replay_manifests
        BEGIN SELECT RAISE(ABORT, 'replay manifests are immutable'); END;
        """)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
        self._harden()

    def __enter__(self) -> "FeatureMart":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def insert(self, record: FeatureRecord) -> int:
        inserted_at = timestamp_text(datetime.now().astimezone())
        self.connection.execute(
            """INSERT OR IGNORE INTO feature_records (
                record_digest,ticker,effective_session,cutoff_at,available_at,
                feature_version,feature_digest,values_json,source_snapshot_ids_json,
                quality_status,missing_reasons_json,inserted_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record.record_digest, record.ticker, record.effective_session.isoformat(),
                timestamp_text(record.cutoff_at), timestamp_text(record.available_at),
                record.feature_version, record.feature_digest, canonical_json(record.values),
                canonical_json(list(record.source_snapshot_ids)), record.quality_status,
                canonical_json(record.missing_reasons), inserted_at,
            ),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT id FROM feature_records WHERE record_digest=?", (record.record_digest,)
        ).fetchone()
        if row is None:
            raise RuntimeError("feature record insert failed")
        self._harden()
        return int(row["id"])

    def register_replay(
        self, *, run_id: str, cutoff_at: datetime, input_payload: Mapping[str, Any],
        feature_record_ids: Sequence[int], model_versions: Sequence[str],
    ) -> str:
        cutoff = utc_timestamp(cutoff_at, field_name="cutoff_at")
        identifiers = tuple(sorted({int(value) for value in feature_record_ids if int(value) > 0}))
        if not identifiers:
            raise ValueError("feature_record_ids cannot be empty")
        input_digest = payload_digest(dict(input_payload))
        replay_id = payload_digest({
            "run_id": run_id, "cutoff_at": timestamp_text(cutoff),
            "input_digest": input_digest, "feature_record_ids": identifiers,
            "model_versions": sorted(set(model_versions)),
        })
        self.connection.execute(
            "INSERT OR IGNORE INTO replay_manifests VALUES (?,?,?,?,?,?,?)",
            (
                replay_id, run_id, timestamp_text(cutoff), input_digest,
                canonical_json(list(identifiers)), canonical_json(sorted(set(model_versions))),
                timestamp_text(datetime.now().astimezone()),
            ),
        )
        self.connection.commit()
        self._harden()
        return replay_id

    def manifest(self, replay_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM replay_manifests WHERE replay_id=?", (replay_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "schema_version": FEATURE_MART_SCHEMA,
            "replay_id": row["replay_id"], "run_id": row["run_id"],
            "cutoff_at": timestamp_text(timestamp_from_text(row["cutoff_at"])),
            "input_digest": row["input_digest"],
            "feature_record_ids": json.loads(row["feature_record_ids_json"]),
            "model_versions": json.loads(row["model_versions_json"]),
        }
