"""Configuration and validation for Codex Screener.

Secrets are read from environment variables but are never serialized, logged, or
included in command output.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_WATCHLIST = (
    "ARM", "QCOM", "INTC", "AAOI", "CBRS", "CSCO", "NOK", "SKHY",
    "NBIS", "BABA", "MRVL", "CRWV", "AMD", "NVDA",
)
OPTIONAL_PARITY_COMPARATOR = "000660.KS"
DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_SNAPSHOT_TIME = "06:55"


class ConfigError(ValueError):
    """Raised when runtime configuration is invalid or unsafe."""


def _setting(
    source: Mapping[str, str],
    legacy_name: str,
    public_name: str,
    default: str | None = None,
) -> str | None:
    """Read a setting without changing established legacy precedence."""

    legacy = source.get(legacy_name)
    if legacy is not None and legacy.strip():
        return legacy
    public = source.get(public_name)
    if public is not None and public.strip():
        return public
    return default


def private_runtime_root(
    source: Mapping[str, str] | None = None,
) -> Path | None:
    """Return the optional private runtime root without creating it."""

    environment = os.environ if source is None else source
    value = environment.get("CODEX_SCREENER_HOME") or environment.get(
        "CODEX_SCREENER_PRIVATE_RUNTIME_ROOT"
    )
    if value is None or not value.strip():
        return None
    return Path(value.strip()).expanduser()


def private_runtime_path(
    relative: str | Path,
    source: Mapping[str, str] | None = None,
) -> Path:
    """Resolve an unset private default under the optional runtime root."""

    root = private_runtime_root(source)
    return (root / relative) if root is not None else Path(relative)


def _parse_watchlist(value: str | None) -> tuple[str, ...]:
    if not value:
        return DEFAULT_WATCHLIST
    symbols = tuple(item.strip().upper() for item in value.split(",") if item.strip())
    if not symbols:
        raise ConfigError("CODEX_SCREENER_WATCHLIST must contain at least one symbol")
    if len(set(symbols)) != len(symbols):
        raise ConfigError("CODEX_SCREENER_WATCHLIST cannot contain duplicate symbols")
    return symbols


def _validate_time(value: str) -> str:
    pieces = value.split(":")
    if len(pieces) != 2 or not all(piece.isdigit() for piece in pieces):
        raise ConfigError("snapshot time must use 24-hour HH:MM format")
    hour, minute = (int(piece) for piece in pieces)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError("snapshot time must be a valid 24-hour time")
    return f"{hour:02d}:{minute:02d}"


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"unknown IANA timezone: {value}") from exc
    return value


def _validate_secret(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    cleaned = value.strip()
    placeholders = {"changeme", "your_api_key_here", "replace_me", "example"}
    if cleaned.lower() in placeholders:
        raise ConfigError("provider API key still contains a placeholder")
    return cleaned


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated runtime settings.

    `provider_api_key` is intentionally omitted from :meth:`public_dict`.
    """

    database_path: Path
    timezone: str
    snapshot_time: str
    watchlist: tuple[str, ...]
    provider_name: str
    provider_api_key: str | None
    provider_usage_path: Path
    private_runtime_root: Path | None

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        source = os.environ if environ is None else environ
        root = private_runtime_root(source)
        database_value = _setting(source, "MORNING_EDGE_DATABASE", "CODEX_SCREENER_DATABASE")
        usage_value = _setting(
            source,
            "MORNING_EDGE_PROVIDER_USAGE_DATABASE",
            "CODEX_SCREENER_PROVIDER_USAGE_DATABASE",
        )
        database_path = (
            Path(database_value).expanduser()
            if database_value is not None
            else private_runtime_path("data/morning-edge.sqlite", source)
        )
        timezone = _validate_timezone(
            str(_setting(source, "MORNING_EDGE_TIMEZONE", "CODEX_SCREENER_TIMEZONE", DEFAULT_TIMEZONE))
        )
        snapshot_time = _validate_time(
            str(
                _setting(
                    source,
                    "MORNING_EDGE_SNAPSHOT_TIME",
                    "CODEX_SCREENER_SNAPSHOT_TIME",
                    DEFAULT_SNAPSHOT_TIME,
                )
            )
        )
        provider_name = str(
            _setting(source, "MORNING_EDGE_PROVIDER", "CODEX_SCREENER_PROVIDER", "unconfigured")
        ).strip().lower()
        if not provider_name:
            raise ConfigError("CODEX_SCREENER_PROVIDER cannot be empty")
        return cls(
            database_path=database_path,
            timezone=timezone,
            snapshot_time=snapshot_time,
            watchlist=_parse_watchlist(
                _setting(source, "MORNING_EDGE_WATCHLIST", "CODEX_SCREENER_WATCHLIST")
            ),
            provider_name=provider_name,
            provider_api_key=_validate_secret(
                _setting(
                    source,
                    "MORNING_EDGE_PROVIDER_API_KEY",
                    "CODEX_SCREENER_PROVIDER_API_KEY",
                )
                or source.get("UNUSUAL_WHALES_API_KEY")
            ),
            provider_usage_path=(
                Path(usage_value).expanduser()
                if usage_value is not None
                else private_runtime_path("data/provider-request-usage.sqlite", source)
            ),
            private_runtime_root=root,
        )

    def public_dict(self) -> dict[str, object]:
        """Return non-secret configuration appropriate for console output."""
        return {
            "database_path": str(self.database_path),
            "timezone": self.timezone,
            "snapshot_time": self.snapshot_time,
            "watchlist": list(self.watchlist),
            "provider_name": self.provider_name,
            "provider_configured": self.provider_api_key is not None,
            "provider_usage_path": str(self.provider_usage_path),
            "private_runtime_root": (
                str(self.private_runtime_root) if self.private_runtime_root is not None else None
            ),
        }
