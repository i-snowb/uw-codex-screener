from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


class RunKind(StrEnum):
    PREOPEN = "preopen"
    OPEN_REFRESH = "open_refresh"


@dataclass(frozen=True, slots=True)
class RunWindow:
    kind: RunKind
    scheduled_at: datetime
    cutoff_at: datetime
    description: str


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include an explicit timezone")
    return value


def eastern(value: datetime) -> datetime:
    return ensure_aware(value).astimezone(NEW_YORK)


def is_weekday(day: date) -> bool:
    return day.weekday() < 5


def _nth_weekday(year: int, month: int, weekday: int, ordinal: int) -> date:
    current = date(year, month, 1)
    return current + timedelta(days=(weekday - current.weekday()) % 7 + 7 * (ordinal - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    following = date(year + (month == 12), month % 12 + 1, 1)
    current = following - timedelta(days=1)
    return current - timedelta(days=(current.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _easter_sunday(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def nyse_holidays(year: int) -> frozenset[date]:
    """Return regular full-day NYSE holidays for the supported modern calendar."""

    holidays = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))
    next_new_year = _observed(date(year + 1, 1, 1))
    if next_new_year.year == year:
        holidays.add(next_new_year)
    return frozenset(holidays)


def is_nyse_session(day: date) -> bool:
    return is_weekday(day) and day not in nyse_holidays(day.year)


def next_nyse_session(day: date, *, include_current: bool = True) -> date:
    candidate = day if include_current else day + timedelta(days=1)
    while not is_nyse_session(candidate):
        candidate += timedelta(days=1)
    return candidate


def previous_nyse_session(day: date, *, include_current: bool = True) -> date:
    """Return the previous regular NYSE session from a calendar date."""

    candidate = day if include_current else day - timedelta(days=1)
    while not is_nyse_session(candidate):
        candidate -= timedelta(days=1)
    return candidate


def next_weekday(day: date) -> date:
    candidate = day
    while not is_weekday(candidate):
        candidate += timedelta(days=1)
    return candidate


def scheduled_windows(day: date) -> tuple[RunWindow, RunWindow]:
    """Return deterministic ET run windows on the next regular NYSE session."""

    session_day = next_nyse_session(day)
    preopen = datetime.combine(session_day, time(6, 55), NEW_YORK)
    open_refresh = datetime.combine(session_day, time(9, 40), NEW_YORK)
    return (
        RunWindow(
            kind=RunKind.PREOPEN,
            scheduled_at=preopen,
            cutoff_at=preopen,
            description="Settled open-interest and modeled positioning snapshot",
        ),
        RunWindow(
            kind=RunKind.OPEN_REFRESH,
            scheduled_at=open_refresh,
            cutoff_at=open_refresh,
            description="Opening quote and options-flow refresh",
        ),
    )


def age_seconds(*, observed_at: datetime, cutoff_at: datetime) -> float:
    observed = ensure_aware(observed_at).astimezone(UTC)
    cutoff = ensure_aware(cutoff_at).astimezone(UTC)
    if observed > cutoff:
        raise ValueError("observation is newer than the scoring cutoff")
    return (cutoff - observed).total_seconds()


def is_fresh(
    *, observed_at: datetime, cutoff_at: datetime, maximum_age: timedelta
) -> bool:
    if maximum_age.total_seconds() < 0:
        raise ValueError("maximum_age must be non-negative")
    return age_seconds(observed_at=observed_at, cutoff_at=cutoff_at) <= maximum_age.total_seconds()
