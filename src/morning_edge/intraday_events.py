"""Point-in-time intraday event records and fail-closed shadow estimates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from .models import timestamp_text, utc_timestamp
from .private_io import ensure_private_directory, harden_sqlite_files


INTRADAY_EVENT_VERSION = "intraday-event-shadow-v1"
INTRADAY_HORIZONS = ("30_MINUTE", "TO_CLOSE", "NEXT_OPEN")


@dataclass(frozen=True)
class IntradayEventRecord:
    ticker: str
    observed_at: datetime
    daily_origin_session: str
    event_type: str
    features: Mapping[str, Any]
    source_snapshot_ids: tuple[int, ...]

    def payload(self) -> dict[str, Any]:
        observed = utc_timestamp(self.observed_at, field_name="observed_at")
        return {
            "version": INTRADAY_EVENT_VERSION,
            "ticker": self.ticker.strip().upper(),
            "observed_at": timestamp_text(observed),
            "daily_origin_session": self.daily_origin_session,
            "event_type": self.event_type.strip().upper(),
            "features": dict(self.features),
            "source_snapshot_ids": sorted({int(value) for value in self.source_snapshot_ids if int(value) > 0}),
        }


class IntradayEventLedger:
    """Append-only event ledger with idempotent inserts."""

    def __init__(self, database: str | Path):
        self.path = Path(database)
        ensure_private_directory(self.path.parent)
        self.connection = sqlite3.connect(str(self.path))
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS intraday_events (
                    digest TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            self.connection.commit()
            harden_sqlite_files(self.path)
        except BaseException:
            self.connection.close()
            harden_sqlite_files(self.path)
            raise

    def __enter__(self) -> "IntradayEventLedger":
        return self

    def __exit__(self, *_args: object) -> None:
        self.connection.close()
        harden_sqlite_files(self.path)

    def insert(self, record: IntradayEventRecord) -> bool:
        payload = record.payload()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO intraday_events(digest,ticker,observed_at,payload_json) VALUES(?,?,?,?)",
            (digest, payload["ticker"], payload["observed_at"], encoded),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def comparable(self, *, ticker: str, event_type: str) -> list[dict[str, Any]]:
        """Return prior same-ticker, same-event records in chronological order."""

        rows = self.connection.execute(
            "SELECT payload_json FROM intraday_events WHERE ticker=? ORDER BY observed_at",
            (ticker.strip().upper(),),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row[0])
            if str(payload.get("event_type", "")).upper() == event_type.strip().upper():
                result.append(payload)
        return result


def shadow_intraday_event_model(
    *, event: IntradayEventRecord, comparable_events: Sequence[Mapping[str, Any]], minimum_history: int = 60,
) -> dict[str, Any]:
    """Create an auditable event-model state; do not borrow daily forecast outputs."""

    same_type = [row for row in comparable_events if str(row.get("event_type", "")).upper() == event.event_type.upper()]
    if len(same_type) < minimum_history:
        return {
            "status": "UNAVAILABLE_INSUFFICIENT_POINT_IN_TIME_HISTORY",
            "version": INTRADAY_EVENT_VERSION,
            "promotion_eligible": False,
            "event_type": event.event_type.upper(),
            "comparable_events": len(same_type),
            "minimum_required": minimum_history,
            "horizons": list(INTRADAY_HORIZONS),
            "reason": "Exact-cutoff intraday outcomes are not deep enough for chronological evaluation.",
        }
    return {
        "status": "SHADOW_EVALUATION_REQUIRED",
        "version": INTRADAY_EVENT_VERSION,
        "promotion_eligible": False,
        "event_type": event.event_type.upper(),
        "comparable_events": len(same_type),
        "horizons": list(INTRADAY_HORIZONS),
        "reason": "History exists, but chronological calibration and baseline tests are not yet complete.",
    }
