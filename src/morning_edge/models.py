"""Provider-neutral, immutable market-data snapshot contracts.

The contracts intentionally preserve both timestamps supplied by a provider:
``as_of`` describes when the observation was true and ``retrieved_at``
describes when Codex Screener obtained it.  Consumers must not silently replace
one with the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from typing import Any, Mapping


class Dataset(StrEnum):
    """The supported high-level data families, independent of a vendor."""

    EQUITY_QUOTE = "equity_quote"
    OHLC = "ohlc"
    OPTION_CHAIN = "option_chain"
    OPTION_FLOW = "option_flow"
    OPTION_PRICE_LEVELS = "option_price_levels"
    GREEK_FLOW = "greek_flow"
    OPEN_INTEREST = "open_interest"
    DEALER_EXPOSURE = "dealer_exposure"
    GREEK_EXPOSURE = "greek_exposure"
    IV_TERM_STRUCTURE = "iv_term_structure"
    VOLATILITY_STATS = "volatility_stats"
    VOLATILITY_DIAGNOSTICS = "volatility_diagnostics"
    INTERPOLATED_IV = "interpolated_iv"
    DARK_POOL = "dark_pool"
    DARK_POOL_LEVELS = "dark_pool_levels"
    MARKET_TIDE = "market_tide"
    MARKET_CORRELATION = "market_correlation"
    SECTOR_TIDE = "sector_tide"
    SHORT_INTEREST = "short_interest"
    BORROW = "borrow"
    SHORT_VOLUME = "short_volume"
    NEWS = "news"
    EARNINGS = "earnings"
    CATALYST = "catalyst"
    POSITION = "position"
    SOCIAL_SENTIMENT = "social_sentiment"
    PROVIDER_AUDIT = "provider_audit"


def utc_timestamp(value: datetime, *, field_name: str = "timestamp") -> datetime:
    """Return ``value`` normalized to UTC, rejecting ambiguous datetimes."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def timestamp_text(value: datetime) -> str:
    """Format an aware datetime as a stable, UTC SQLite value."""

    return utc_timestamp(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def timestamp_from_text(value: str) -> datetime:
    """Parse a timestamp produced by :func:`timestamp_text`."""

    return utc_timestamp(datetime.fromisoformat(value.replace("Z", "+00:00")))


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically so hashes are provider-order agnostic."""

    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise ValueError("payload and metadata must be JSON serializable") from error


def payload_digest(payload: Any) -> str:
    """Return a SHA-256 hash of a deterministic JSON representation."""

    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SnapshotEnvelope:
    """One unmodified provider response and its market-time context.

    ``payload`` must be the raw provider response after JSON decoding.  Derived
    fields belong in a separate analysis layer; storing them here would make it
    impossible to reproduce a result from original evidence.
    """

    provider: str
    dataset: Dataset | str
    as_of: datetime
    retrieved_at: datetime
    payload: Any
    symbol: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self) -> None:
        provider = self.provider.strip()
        if not provider:
            raise ValueError("provider must not be empty")
        object.__setattr__(self, "provider", provider)

        dataset = Dataset(self.dataset)
        object.__setattr__(self, "dataset", dataset)

        symbol = self.symbol.strip().upper() if self.symbol is not None else None
        if symbol == "":
            raise ValueError("symbol must not be empty when supplied")
        object.__setattr__(self, "symbol", symbol)

        if self.schema_version < 1:
            raise ValueError("schema_version must be at least 1")

        as_of = utc_timestamp(self.as_of, field_name="as_of")
        retrieved_at = utc_timestamp(self.retrieved_at, field_name="retrieved_at")
        if as_of > retrieved_at:
            raise ValueError("as_of cannot be later than retrieved_at")
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "retrieved_at", retrieved_at)

        # Validate now. This prevents a later caller mutating a mapping into an
        # unserializable object between construction and persistence.
        canonical_json(self.payload)
        canonical_json(dict(self.metadata))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def raw_payload_hash(self) -> str:
        return payload_digest(self.payload)

    @property
    def idempotency_key(self) -> str:
        """Hash every fact that makes a snapshot distinct.

        Two reads of identical content at different retrieval times remain
        distinct evidence. Re-submitting the exact same read is a no-op.
        """

        material = {
            "provider": self.provider,
            "dataset": self.dataset.value,
            "symbol": self.symbol,
            "as_of": timestamp_text(self.as_of),
            "retrieved_at": timestamp_text(self.retrieved_at),
            "payload_hash": self.raw_payload_hash,
            "metadata": dict(self.metadata),
            "schema_version": self.schema_version,
        }
        return payload_digest(material)


@dataclass(frozen=True, slots=True)
class StoredSnapshot:
    """A persisted snapshot, including the local immutable record identifier."""

    id: int
    envelope: SnapshotEnvelope
    inserted_at: datetime
