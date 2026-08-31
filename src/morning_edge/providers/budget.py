"""Conservative local accounting for authenticated provider request attempts.

The ledger is independent of market-data storage. It retains only provider
name, UTC timestamps, and operator-approved adjustment identifiers; URLs,
response bodies, and credentials never enter this database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Callable

from .base import ProviderRateLimitError


DEFAULT_WEEKLY_CAP = 30_000
DEFAULT_WEEKLY_RESERVE = 20_000
ROLLING_WINDOW = timedelta(days=7)
SQLITE_STARTUP_TIMEOUT_SECONDS = 5.0
SQLITE_STARTUP_RETRY_SECONDS = 0.05
_ADJUSTMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class ProviderRequestBudgetExceeded(ProviderRateLimitError):
    """Raised before an outbound request would consume protected capacity."""


@dataclass(frozen=True, slots=True)
class WeeklyBudgetUsage:
    provider: str
    window_start: datetime
    observed_at: datetime
    weekly_cap: int
    protected_reserve: int
    transport_attempts: int
    adjustment_attempts: int

    @property
    def attempted_requests(self) -> int:
        return self.transport_attempts + self.adjustment_attempts

    @property
    def usable_requests(self) -> int:
        return self.weekly_cap - self.protected_reserve

    @property
    def remaining_before_reserve(self) -> int:
        return max(0, self.usable_requests - self.attempted_requests)

    def public_dict(self) -> dict[str, int | str]:
        window_hours = int((self.observed_at - self.window_start).total_seconds() // 3600)
        window_rule = (
            "Trailing 7 x 24-hour local accounting window ending at observed_at_utc"
            if window_hours == 168
            else f"Trailing {window_hours}-hour local accounting window ending at observed_at_utc"
        )
        return {
            "provider": self.provider,
            "window_start_utc": self.window_start.isoformat(),
            "observed_at_utc": self.observed_at.isoformat(),
            "window_rule": window_rule,
            "window_cap": self.weekly_cap,
            "weekly_cap": self.weekly_cap,
            "protected_reserve": self.protected_reserve,
            "usable_requests_before_reserve": self.usable_requests,
            "transport_attempts": self.transport_attempts,
            "baseline_adjustment_attempts": self.adjustment_attempts,
            "attempted_requests": self.attempted_requests,
            "remaining_before_reserve": self.remaining_before_reserve,
        }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _window_start(now: datetime, rolling_window: timedelta = ROLLING_WINDOW) -> datetime:
    if rolling_window <= timedelta(0):
        raise ValueError("rolling_window must be positive")
    return _utc(now) - rolling_window


class WeeklyRequestBudget:
    """Append-only counter that blocks before a protected reserve is used.

    Every call to :meth:`reserve_attempt` is charged before the HTTP transport is
    invoked. Retries, network failures, and process crashes after reservation
    remain charged. The seven-day period is a conservative local accounting
    window; it does not infer the provider's billing reset.
    """

    def __init__(
        self,
        path: str | Path = "data/provider-request-usage.sqlite",
        *,
        provider: str = "unusual_whales",
        weekly_cap: int = DEFAULT_WEEKLY_CAP,
        protected_reserve: int = DEFAULT_WEEKLY_RESERVE,
        rolling_window: timedelta = ROLLING_WINDOW,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        provider = provider.strip().lower()
        if not provider:
            raise ValueError("provider must not be empty")
        if weekly_cap < 1 or protected_reserve < 0 or protected_reserve >= weekly_cap:
            raise ValueError("weekly_cap must be positive and protected_reserve must be smaller")
        if rolling_window <= timedelta(0):
            raise ValueError("rolling_window must be positive")
        self.path = Path(path)
        self.provider = provider
        self.weekly_cap = weekly_cap
        self.protected_reserve = protected_reserve
        self.rolling_window = rolling_window
        self._clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._secure_paths()
        self._connection = self._open_connection()
        self._create_schema()
        self._secure_paths()

    def close(self) -> None:
        self._secure_paths()
        self._connection.close()
        self._secure_paths()

    def __enter__(self) -> "WeeklyRequestBudget":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _secure_paths(self) -> None:
        """Keep the ledger directory and every SQLite sidecar owner-private."""
        os.chmod(self.path.parent, 0o700)
        for candidate in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            if candidate.exists():
                os.chmod(candidate, 0o600)

    def _open_connection(self) -> sqlite3.Connection:
        """Open and configure SQLite with a bounded startup lock wait.

        SQLite's WAL-mode transition needs an exclusive lock. Concurrent local
        processes can race during first use, so retry only that initialization
        phase for a fixed time. If it remains locked, initialization fails
        before any provider transport can run.
        """
        deadline = time.monotonic() + SQLITE_STARTUP_TIMEOUT_SECONDS
        last_error: sqlite3.OperationalError | None = None
        while True:
            connection = sqlite3.connect(
                self.path,
                isolation_level=None,
                timeout=SQLITE_STARTUP_TIMEOUT_SECONDS,
            )
            connection.row_factory = sqlite3.Row
            try:
                connection.execute(f"PRAGMA busy_timeout = {int(SQLITE_STARTUP_TIMEOUT_SECONDS * 1000)}")
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                return connection
            except sqlite3.OperationalError as error:
                connection.close()
                last_error = error
                if "locked" not in str(error).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(min(SQLITE_STARTUP_RETRY_SECONDS, max(0.0, deadline - time.monotonic())))
        raise AssertionError("startup retry loop must return or raise") from last_error

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS provider_request_attempts (
                id INTEGER PRIMARY KEY,
                provider TEXT NOT NULL,
                window_start_utc TEXT NOT NULL,
                attempted_at_utc TEXT NOT NULL,
                CHECK(window_start_utc <= attempted_at_utc)
            );
            CREATE INDEX IF NOT EXISTS provider_request_attempts_window
                ON provider_request_attempts(provider, attempted_at_utc, id);
            CREATE TABLE IF NOT EXISTS provider_request_adjustments (
                adjustment_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                attempted_requests INTEGER NOT NULL CHECK(attempted_requests > 0),
                effective_at_utc TEXT NOT NULL,
                evidence_id TEXT NOT NULL,
                inserted_at_utc TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS provider_request_adjustments_window
                ON provider_request_adjustments(provider, effective_at_utc, adjustment_id);
            CREATE TRIGGER IF NOT EXISTS provider_request_attempts_no_update
            BEFORE UPDATE ON provider_request_attempts BEGIN
                SELECT RAISE(ABORT, 'provider request attempts are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS provider_request_attempts_no_delete
            BEFORE DELETE ON provider_request_attempts BEGIN
                SELECT RAISE(ABORT, 'provider request attempts are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS provider_request_adjustments_no_update
            BEFORE UPDATE ON provider_request_adjustments BEGIN
                SELECT RAISE(ABORT, 'provider request adjustments are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS provider_request_adjustments_no_delete
            BEFORE DELETE ON provider_request_adjustments BEGIN
                SELECT RAISE(ABORT, 'provider request adjustments are immutable');
            END;
            """
        )

    def _usage_counts(self, *, observed_at: datetime) -> tuple[int, int]:
        window_start = _window_start(observed_at, self.rolling_window).isoformat()
        now_text = observed_at.isoformat()
        transport_attempts = int(
            self._connection.execute(
                """SELECT COUNT(*) FROM provider_request_attempts
                   WHERE provider = ? AND attempted_at_utc >= ? AND attempted_at_utc <= ?""",
                (self.provider, window_start, now_text),
            ).fetchone()[0]
        )
        adjustment_attempts = int(
            self._connection.execute(
                """SELECT COALESCE(SUM(attempted_requests), 0) FROM provider_request_adjustments
                   WHERE provider = ? AND effective_at_utc >= ? AND effective_at_utc <= ?""",
                (self.provider, window_start, now_text),
            ).fetchone()[0]
        )
        return transport_attempts, adjustment_attempts

    def usage(self, *, now: datetime | None = None) -> WeeklyBudgetUsage:
        observed_at = _utc(self._clock() if now is None else now)
        transport_attempts, adjustment_attempts = self._usage_counts(observed_at=observed_at)
        self._secure_paths()
        return WeeklyBudgetUsage(
            provider=self.provider,
            window_start=_window_start(observed_at, self.rolling_window),
            observed_at=observed_at,
            weekly_cap=self.weekly_cap,
            protected_reserve=self.protected_reserve,
            transport_attempts=transport_attempts,
            adjustment_attempts=adjustment_attempts,
        )

    def add_baseline_adjustment(
        self,
        *,
        adjustment_id: str,
        attempted_requests: int,
        evidence_id: str,
        effective_at: datetime | None = None,
    ) -> WeeklyBudgetUsage:
        """Record a one-time evidenced baseline without fabricating HTTP rows.

        Repeating an adjustment ID is idempotent only when every immutable
        recorded value agrees. A conflicting reuse fails closed.
        """
        if not _ADJUSTMENT_ID.fullmatch(adjustment_id):
            raise ValueError("adjustment_id must be 1-128 safe identifier characters")
        if not _ADJUSTMENT_ID.fullmatch(evidence_id):
            raise ValueError("evidence_id must be 1-128 safe identifier characters")
        if isinstance(attempted_requests, bool) or not isinstance(attempted_requests, int) or attempted_requests < 1:
            raise ValueError("attempted_requests must be a positive integer")
        observed_at = _utc(self._clock())
        effective = _utc(observed_at if effective_at is None else effective_at)
        if effective > observed_at:
            raise ValueError("effective_at must not be in the future")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._connection.execute(
                """SELECT provider, attempted_requests, effective_at_utc, evidence_id
                   FROM provider_request_adjustments WHERE adjustment_id = ?""",
                (adjustment_id,),
            ).fetchone()
            values = (self.provider, attempted_requests, effective.isoformat(), evidence_id)
            if existing is None:
                self._connection.execute(
                    """INSERT INTO provider_request_adjustments(
                        adjustment_id, provider, attempted_requests, effective_at_utc, evidence_id, inserted_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (adjustment_id, *values, observed_at.isoformat()),
                )
            elif tuple(existing) != values:
                raise ValueError("adjustment_id already exists with different immutable values")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")
        self._secure_paths()
        return self.usage(now=observed_at)

    def reserve_attempt(self) -> WeeklyBudgetUsage:
        """Atomically charge one transport attempt or fail before provider I/O."""
        observed_at = _utc(self._clock())
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            transport_attempts, adjustment_attempts = self._usage_counts(observed_at=observed_at)
            usable = self.weekly_cap - self.protected_reserve
            if transport_attempts + adjustment_attempts >= usable:
                raise ProviderRequestBudgetExceeded(
                    "local provider request budget reached; protected reserve remains unavailable"
                )
            self._connection.execute(
                """INSERT INTO provider_request_attempts(provider, window_start_utc, attempted_at_utc)
                   VALUES (?, ?, ?)""",
                (
                    self.provider,
                    _window_start(observed_at, self.rolling_window).isoformat(),
                    observed_at.isoformat(),
                ),
            )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")
        self._secure_paths()
        return self.usage(now=observed_at)
