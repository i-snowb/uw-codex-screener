"""Append-only forecast and outcome audit ledger.

The ledger is deliberately separate from scoring.  It records exactly what a
model was allowed to know at a decision cutoff, then records observations made
after that cutoff.  This makes later calibration possible without rewriting
history or leaking future evidence into a forecast.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator, Mapping, Sequence

from .models import canonical_json, payload_digest, timestamp_from_text, timestamp_text, utc_timestamp
from .store import SnapshotStore


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACTIONS = frozenset({"BUY", "WATCH", "NO_ACTION", "TRIM", "EXIT"})


def _non_empty(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _finite_optional(value: float | None, field_name: str) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    return float(value)


@dataclass(frozen=True, slots=True)
class ForecastRecord:
    """A fully reproducible forecast produced at one immutable cutoff."""

    ticker: str
    cutoff_at: datetime
    generated_at: datetime
    horizon_sessions: int
    scoring_version: str
    model_version: str
    action: str
    setup_score: int
    directional_probability: float
    confidence: float
    source_snapshot_ids: tuple[int, ...]
    trigger: str
    invalidation: str
    feature_payload: Any | None = None
    feature_hash: str | None = None
    option_metadata: Mapping[str, Any] | None = None
    friction_metadata: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ticker = self.ticker.strip().upper()
        if not ticker:
            raise ValueError("ticker must not be empty")
        object.__setattr__(self, "ticker", ticker)

        cutoff_at = utc_timestamp(self.cutoff_at, field_name="cutoff_at")
        generated_at = utc_timestamp(self.generated_at, field_name="generated_at")
        if generated_at < cutoff_at:
            raise ValueError("generated_at cannot be earlier than cutoff_at")
        object.__setattr__(self, "cutoff_at", cutoff_at)
        object.__setattr__(self, "generated_at", generated_at)

        if self.horizon_sessions < 1:
            raise ValueError("horizon_sessions must be at least 1")
        object.__setattr__(self, "scoring_version", _non_empty(self.scoring_version, "scoring_version"))
        object.__setattr__(self, "model_version", _non_empty(self.model_version, "model_version"))
        action = _non_empty(self.action, "action").upper()
        if action not in _ACTIONS:
            raise ValueError(f"action must be one of {sorted(_ACTIONS)}")
        object.__setattr__(self, "action", action)
        if not 0 <= self.setup_score <= 100:
            raise ValueError("setup_score must be between 0 and 100")
        if not 0.0 <= self.directional_probability <= 1.0:
            raise ValueError("directional_probability must be between 0 and 1")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")

        # Source sets have no semantic ordering. Canonicalize them so a replay
        # loaded from SQLite preserves the same idempotency key.
        source_ids = tuple(sorted(self.source_snapshot_ids))
        if not source_ids or any(not isinstance(item, int) or item < 1 for item in source_ids):
            raise ValueError("source_snapshot_ids must contain positive integer IDs")
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("source_snapshot_ids must not contain duplicates")
        object.__setattr__(self, "source_snapshot_ids", source_ids)
        object.__setattr__(self, "trigger", _non_empty(self.trigger, "trigger"))
        object.__setattr__(self, "invalidation", _non_empty(self.invalidation, "invalidation"))

        payload_hash = payload_digest(self.feature_payload) if self.feature_payload is not None else None
        supplied_hash = self.feature_hash.lower() if self.feature_hash is not None else None
        if supplied_hash is not None and not _SHA256.fullmatch(supplied_hash):
            raise ValueError("feature_hash must be a lowercase SHA-256 hexadecimal digest")
        if payload_hash is None and supplied_hash is None:
            raise ValueError("feature_payload or feature_hash is required")
        if payload_hash is not None and supplied_hash is not None and payload_hash != supplied_hash:
            raise ValueError("feature_hash does not match feature_payload")
        object.__setattr__(self, "feature_hash", supplied_hash or payload_hash)

        for attribute in ("option_metadata", "friction_metadata", "metadata"):
            value = getattr(self, attribute)
            if value is not None:
                canonical_json(dict(value))
                object.__setattr__(self, attribute, dict(value))

    @property
    def idempotency_key(self) -> str:
        """Stable identity for one exact model decision and its evidence."""

        return payload_digest(
            {
                "ticker": self.ticker,
                "cutoff_at": timestamp_text(self.cutoff_at),
                "generated_at": timestamp_text(self.generated_at),
                "horizon_sessions": self.horizon_sessions,
                "scoring_version": self.scoring_version,
                "model_version": self.model_version,
                "action": self.action,
                "setup_score": self.setup_score,
                "directional_probability": self.directional_probability,
                "confidence": self.confidence,
                "source_snapshot_ids": self.source_snapshot_ids,
                "trigger": self.trigger,
                "invalidation": self.invalidation,
                "feature_hash": self.feature_hash,
                "option_metadata": self.option_metadata,
                "friction_metadata": self.friction_metadata,
                "metadata": self.metadata,
            }
        )


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """A realized observation for a forecast, captured strictly post-cutoff."""

    forecast_id: int
    observed_at: datetime
    underlying_return_pct: float | None = None
    option_return_pct: float | None = None
    max_adverse_excursion_pct: float | None = None
    realized_volatility: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.forecast_id, int) or self.forecast_id < 1:
            raise ValueError("forecast_id must be a positive integer")
        object.__setattr__(self, "observed_at", utc_timestamp(self.observed_at, field_name="observed_at"))
        for attribute in (
            "underlying_return_pct",
            "option_return_pct",
            "max_adverse_excursion_pct",
            "realized_volatility",
        ):
            object.__setattr__(self, attribute, _finite_optional(getattr(self, attribute), attribute))
        canonical_json(dict(self.metadata))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def idempotency_key(self) -> str:
        return payload_digest(
            {
                "forecast_id": self.forecast_id,
                "observed_at": timestamp_text(self.observed_at),
                "underlying_return_pct": self.underlying_return_pct,
                "option_return_pct": self.option_return_pct,
                "max_adverse_excursion_pct": self.max_adverse_excursion_pct,
                "realized_volatility": self.realized_volatility,
                "metadata": self.metadata,
            }
        )


@dataclass(frozen=True, slots=True)
class StoredForecast:
    id: int
    record: ForecastRecord
    inserted_at: datetime


@dataclass(frozen=True, slots=True)
class StoredOutcome:
    id: int
    record: OutcomeRecord
    inserted_at: datetime


class ForecastLedger:
    """Append-only SQLite audit trail for forecasts, lineage, and outcomes."""

    def __init__(self, database: str | Path) -> None:
        self.path = Path(database)
        # Forecast lineage references the existing immutable snapshot schema.
        with SnapshotStore(self.path):
            pass
        self._connection = sqlite3.connect(self.path, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._create_schema()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "ForecastLedger":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """Read-only audit access; SQLite triggers reject ledger mutation."""

        return self._connection

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS forecasts (
                id INTEGER PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key) = 64),
                ticker TEXT NOT NULL,
                cutoff_at TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                horizon_sessions INTEGER NOT NULL CHECK(horizon_sessions >= 1),
                scoring_version TEXT NOT NULL,
                model_version TEXT NOT NULL,
                action TEXT NOT NULL,
                setup_score INTEGER NOT NULL CHECK(setup_score BETWEEN 0 AND 100),
                directional_probability REAL NOT NULL CHECK(directional_probability BETWEEN 0 AND 1),
                confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                feature_hash TEXT NOT NULL CHECK(length(feature_hash) = 64),
                feature_payload_json TEXT,
                trigger_text TEXT NOT NULL,
                invalidation_text TEXT NOT NULL,
                option_metadata_json TEXT,
                friction_metadata_json TEXT,
                metadata_json TEXT NOT NULL,
                inserted_at TEXT NOT NULL,
                CHECK(generated_at >= cutoff_at)
            );
            CREATE INDEX IF NOT EXISTS forecasts_lookup
                ON forecasts (ticker, cutoff_at DESC, horizon_sessions);

            CREATE TABLE IF NOT EXISTS forecast_sources (
                forecast_id INTEGER NOT NULL REFERENCES forecasts(id),
                snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
                PRIMARY KEY (forecast_id, snapshot_id)
            );
            CREATE INDEX IF NOT EXISTS forecast_sources_snapshot_lookup
                ON forecast_sources (snapshot_id);

            CREATE TABLE IF NOT EXISTS outcomes (
                id INTEGER PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key) = 64),
                forecast_id INTEGER NOT NULL REFERENCES forecasts(id),
                observed_at TEXT NOT NULL,
                underlying_return_pct REAL,
                option_return_pct REAL,
                max_adverse_excursion_pct REAL,
                realized_volatility REAL,
                metadata_json TEXT NOT NULL,
                inserted_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS outcomes_forecast_lookup
                ON outcomes (forecast_id, observed_at DESC);

            CREATE TRIGGER IF NOT EXISTS forecasts_no_update
            BEFORE UPDATE ON forecasts BEGIN
                SELECT RAISE(ABORT, 'forecasts are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS forecasts_no_delete
            BEFORE DELETE ON forecasts BEGIN
                SELECT RAISE(ABORT, 'forecasts are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS forecast_sources_no_update
            BEFORE UPDATE ON forecast_sources BEGIN
                SELECT RAISE(ABORT, 'forecast sources are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS forecast_sources_no_delete
            BEFORE DELETE ON forecast_sources BEGIN
                SELECT RAISE(ABORT, 'forecast sources are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS outcomes_no_update
            BEFORE UPDATE ON outcomes BEGIN
                SELECT RAISE(ABORT, 'outcomes are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS outcomes_no_delete
            BEFORE DELETE ON outcomes BEGIN
                SELECT RAISE(ABORT, 'outcomes are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS forecast_sources_no_lookahead
            BEFORE INSERT ON forecast_sources
            WHEN EXISTS (
                SELECT 1
                FROM forecasts AS f JOIN snapshots AS s ON s.id = NEW.snapshot_id
                WHERE f.id = NEW.forecast_id
                  AND (s.as_of > f.cutoff_at OR s.retrieved_at > f.cutoff_at)
            )
            BEGIN
                SELECT RAISE(ABORT, 'source snapshot is after forecast cutoff');
            END;
            DROP TRIGGER IF EXISTS outcomes_no_lookahead;
            CREATE TRIGGER outcomes_no_lookahead
            BEFORE INSERT ON outcomes
            WHEN EXISTS (
                SELECT 1 FROM forecasts AS f
                WHERE f.id = NEW.forecast_id AND NEW.observed_at <= f.generated_at
            )
            BEGIN
                SELECT RAISE(ABORT, 'outcome observation must be after forecast generation');
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

    def insert_forecast(self, record: ForecastRecord) -> StoredForecast:
        """Append a forecast with snapshot lineage, or return its prior record."""

        if not isinstance(record, ForecastRecord):
            raise TypeError("record must be a ForecastRecord")
        with self._transaction() as connection:
            self._assert_sources_are_available(connection, record)
            now = timestamp_text(datetime.now(timezone.utc))
            connection.execute(
                """
                INSERT INTO forecasts (
                    idempotency_key, ticker, cutoff_at, generated_at, horizon_sessions,
                    scoring_version, model_version, action, setup_score,
                    directional_probability, confidence, feature_hash,
                    feature_payload_json, trigger_text, invalidation_text,
                    option_metadata_json, friction_metadata_json, metadata_json, inserted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    record.idempotency_key,
                    record.ticker,
                    timestamp_text(record.cutoff_at),
                    timestamp_text(record.generated_at),
                    record.horizon_sessions,
                    record.scoring_version,
                    record.model_version,
                    record.action,
                    record.setup_score,
                    record.directional_probability,
                    record.confidence,
                    record.feature_hash,
                    canonical_json(record.feature_payload) if record.feature_payload is not None else None,
                    record.trigger,
                    record.invalidation,
                    canonical_json(dict(record.option_metadata)) if record.option_metadata is not None else None,
                    canonical_json(dict(record.friction_metadata)) if record.friction_metadata is not None else None,
                    canonical_json(dict(record.metadata)),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM forecasts WHERE idempotency_key = ?", (record.idempotency_key,)
            ).fetchone()
            if row is None:  # pragma: no cover - defensive database corruption check
                raise RuntimeError("forecast insert did not return a record")
            forecast_id = int(row["id"])
            for snapshot_id in record.source_snapshot_ids:
                connection.execute(
                    "INSERT INTO forecast_sources (forecast_id, snapshot_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                    (forecast_id, snapshot_id),
                )
            return self._row_to_forecast(row, record.source_snapshot_ids)

    def _assert_sources_are_available(
        self, connection: sqlite3.Connection, record: ForecastRecord
    ) -> None:
        placeholders = ",".join("?" for _ in record.source_snapshot_ids)
        rows = connection.execute(
            f"SELECT id, as_of, retrieved_at FROM snapshots WHERE id IN ({placeholders})",
            record.source_snapshot_ids,
        ).fetchall()
        if len(rows) != len(record.source_snapshot_ids):
            found = {int(row["id"]) for row in rows}
            missing = sorted(set(record.source_snapshot_ids) - found)
            raise ValueError(f"source snapshot IDs do not exist: {missing}")
        cutoff = timestamp_text(record.cutoff_at)
        future = [
            int(row["id"])
            for row in rows
            if row["as_of"] > cutoff or row["retrieved_at"] > cutoff
        ]
        if future:
            raise ValueError(f"source snapshots are unavailable at cutoff: {sorted(future)}")

    def insert_outcome(self, record: OutcomeRecord) -> StoredOutcome:
        """Append a post-cutoff realized outcome, or return the exact prior row."""

        if not isinstance(record, OutcomeRecord):
            raise TypeError("record must be an OutcomeRecord")
        with self._transaction() as connection:
            forecast = connection.execute(
                "SELECT cutoff_at, generated_at FROM forecasts WHERE id = ?", (record.forecast_id,)
            ).fetchone()
            if forecast is None:
                raise ValueError(f"forecast {record.forecast_id} does not exist")
            if record.observed_at <= timestamp_from_text(forecast["generated_at"]):
                raise ValueError("observed_at must be after forecast generation")
            now = timestamp_text(datetime.now(timezone.utc))
            connection.execute(
                """
                INSERT INTO outcomes (
                    idempotency_key, forecast_id, observed_at, underlying_return_pct,
                    option_return_pct, max_adverse_excursion_pct, realized_volatility,
                    metadata_json, inserted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    record.idempotency_key,
                    record.forecast_id,
                    timestamp_text(record.observed_at),
                    record.underlying_return_pct,
                    record.option_return_pct,
                    record.max_adverse_excursion_pct,
                    record.realized_volatility,
                    canonical_json(dict(record.metadata)),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM outcomes WHERE idempotency_key = ?", (record.idempotency_key,)
            ).fetchone()
            if row is None:  # pragma: no cover - defensive database corruption check
                raise RuntimeError("outcome insert did not return a record")
            return self._row_to_outcome(row)

    def get_forecast(self, forecast_id: int) -> StoredForecast | None:
        row = self._connection.execute("SELECT * FROM forecasts WHERE id = ?", (forecast_id,)).fetchone()
        if row is None:
            return None
        sources = self._source_ids(forecast_id)
        return self._row_to_forecast(row, sources)

    def get_outcome(self, outcome_id: int) -> StoredOutcome | None:
        row = self._connection.execute("SELECT * FROM outcomes WHERE id = ?", (outcome_id,)).fetchone()
        return self._row_to_outcome(row) if row else None

    def _source_ids(self, forecast_id: int) -> tuple[int, ...]:
        rows = self._connection.execute(
            "SELECT snapshot_id FROM forecast_sources WHERE forecast_id = ? ORDER BY snapshot_id",
            (forecast_id,),
        ).fetchall()
        return tuple(int(row["snapshot_id"]) for row in rows)

    @staticmethod
    def _row_to_forecast(row: sqlite3.Row, source_snapshot_ids: Sequence[int]) -> StoredForecast:
        record = ForecastRecord(
            ticker=row["ticker"],
            cutoff_at=timestamp_from_text(row["cutoff_at"]),
            generated_at=timestamp_from_text(row["generated_at"]),
            horizon_sessions=row["horizon_sessions"],
            scoring_version=row["scoring_version"],
            model_version=row["model_version"],
            action=row["action"],
            setup_score=row["setup_score"],
            directional_probability=row["directional_probability"],
            confidence=row["confidence"],
            source_snapshot_ids=tuple(source_snapshot_ids),
            trigger=row["trigger_text"],
            invalidation=row["invalidation_text"],
            feature_payload=json.loads(row["feature_payload_json"]) if row["feature_payload_json"] else None,
            feature_hash=row["feature_hash"],
            option_metadata=json.loads(row["option_metadata_json"]) if row["option_metadata_json"] else None,
            friction_metadata=json.loads(row["friction_metadata_json"]) if row["friction_metadata_json"] else None,
            metadata=json.loads(row["metadata_json"]),
        )
        return StoredForecast(
            id=int(row["id"]), record=record, inserted_at=timestamp_from_text(row["inserted_at"])
        )

    @staticmethod
    def _row_to_outcome(row: sqlite3.Row) -> StoredOutcome:
        record = OutcomeRecord(
            forecast_id=row["forecast_id"],
            observed_at=timestamp_from_text(row["observed_at"]),
            underlying_return_pct=row["underlying_return_pct"],
            option_return_pct=row["option_return_pct"],
            max_adverse_excursion_pct=row["max_adverse_excursion_pct"],
            realized_volatility=row["realized_volatility"],
            metadata=json.loads(row["metadata_json"]),
        )
        return StoredOutcome(
            id=int(row["id"]), record=record, inserted_at=timestamp_from_text(row["inserted_at"])
        )
