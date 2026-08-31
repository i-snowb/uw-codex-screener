"""Read-only normalization of immutable snapshots into agent-safe evidence.

This module deliberately has no provider client and no scoring policy.  It
selects only source records that were retrieved by a caller supplied cutoff,
keeps the selected immutable snapshot IDs, and omits incomplete market data
rather than filling it with estimates.  The resulting :class:`EvidenceBundle`
is compact enough to pass to a narrative/agent layer without giving that layer
access to an unbounded raw archive.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import json
from math import isfinite
from pathlib import Path
import sqlite3
from statistics import median
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from .features import DailyBar, TrendFeatures, build_trend_features
from .models import timestamp_from_text, timestamp_text, utc_timestamp


_GEX_FIELDS = ("call_wall", "put_wall", "gamma_flip", "gamma_magnet")
_MARKET_TIMEZONE = ZoneInfo("America/New_York")


def _number(value: Any) -> float | None:
    """Return a finite numeric value, or ``None`` for absent/vendor-invalid data."""

    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def _payload_rows(payload: Any) -> list[Mapping[str, Any]]:
    value = payload.get("data", payload) if isinstance(payload, Mapping) else payload
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _payload_object(payload: Any) -> Mapping[str, Any]:
    value = payload.get("data", payload) if isinstance(payload, Mapping) else {}
    return value if isinstance(value, Mapping) else {}


def _market_date(metadata: Mapping[str, Any], as_of: datetime) -> date:
    """Use the requested session where recorded, otherwise the snapshot date."""

    requested = metadata.get("requested_market_date")
    if isinstance(requested, str):
        try:
            return date.fromisoformat(requested)
        except ValueError:
            pass
    return as_of.date()


def _timestamp_market_date(value: Any) -> date | None:
    """Parse one provider timestamp into its US market-calendar date."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(_MARKET_TIMEZONE).date()


def _provider_market_date(
    dataset: str,
    metadata: Mapping[str, Any],
    payload: Any,
    as_of: datetime,
) -> date:
    """Prefer provider row/session dates over retrieval-only snapshot times.

    Current endpoints do not always expose a response-wide observation time.
    In those cases the immutable envelope deliberately uses retrieval time for
    ``as_of``.  That fallback must never relabel Friday rows as Monday market
    evidence, so normalization derives a conservative provider date from the
    row fields that belong to each dataset.
    """

    requested = metadata.get("requested_market_date")
    if isinstance(requested, str):
        try:
            return date.fromisoformat(requested)
        except ValueError:
            pass
    if dataset == "dealer_exposure":
        value = _payload_object(payload).get("date")
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                pass
    rows = _payload_rows(payload)
    date_keys: tuple[str, ...]
    timestamp_keys: tuple[str, ...]
    if dataset == "option_chain":
        date_keys, timestamp_keys = (), ("last_tape_time",)
    elif dataset == "option_flow":
        date_keys, timestamp_keys = (), ("created_at", "executed_at")
    elif dataset == "open_interest":
        date_keys, timestamp_keys = ("curr_date", "date"), ()
    elif dataset == "dark_pool":
        date_keys, timestamp_keys = (), ("executed_at", "trf_executed_at")
    elif dataset == "news":
        date_keys, timestamp_keys = (), ("created_at", "published_at")
    elif dataset == "ohlc":
        date_keys, timestamp_keys = ("date",), ()
        rows = [row for row in rows if row.get("market_time") == "r"] or rows
    else:
        return as_of.astimezone(_MARKET_TIMEZONE).date()
    observed: list[date] = []
    for row in rows:
        for key in date_keys:
            value = row.get(key)
            if isinstance(value, str):
                try:
                    observed.append(date.fromisoformat(value[:10]))
                except ValueError:
                    pass
        for key in timestamp_keys:
            parsed = _timestamp_market_date(row.get(key))
            if parsed is not None:
                observed.append(parsed)
    return max(observed) if observed else as_of.astimezone(_MARKET_TIMEZONE).date()


def _utc_date(cutoff_at: datetime) -> date:
    return utc_timestamp(cutoff_at, field_name="cutoff_at").date()


def _rsi_14(closes: list[float]) -> float | None:
    if len(closes) <= 14:
        return None
    changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    gains = [max(value, 0.0) for value in changes]
    losses = [max(-value, 0.0) for value in changes]
    gain = sum(gains[:14]) / 14; loss = sum(losses[:14]) / 14
    for up, down in zip(gains[14:], losses[14:]):
        gain = (gain * 13 + up) / 14; loss = (loss * 13 + down) / 14
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def _ema_series(closes: list[float], period: int) -> list[float | None]:
    if not closes:
        return []
    alpha = 2 / (period + 1); current = closes[0]; output: list[float | None] = []
    for index, close in enumerate(closes):
        current = alpha * close + (1 - alpha) * current
        output.append(current if index + 1 >= period else None)
    return output


@dataclass(frozen=True, slots=True)
class SourceRef:
    """A selected immutable raw snapshot, including both source timestamps."""

    snapshot_id: int
    dataset: str
    market_date: date
    as_of: datetime
    retrieved_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "dataset": self.dataset,
            "market_date": self.market_date.isoformat(),
            "as_of": timestamp_text(self.as_of),
            "retrieved_at": timestamp_text(self.retrieved_at),
        }


@dataclass(frozen=True, slots=True)
class NormalizedBar:
    session_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    source_snapshot_id: int
    available_at: datetime


@dataclass(frozen=True, slots=True)
class ChainSession:
    market_date: date
    source: SourceRef
    raw_contract_count: int
    valid_contract_count: int
    call_open_interest: int
    put_open_interest: int
    call_volume: int
    put_volume: int
    median_implied_volatility: float | None


@dataclass(frozen=True, slots=True)
class GexSession:
    market_date: date
    source: SourceRef
    call_wall: float
    put_wall: float
    gamma_flip: float
    gamma_magnet: float


@dataclass(frozen=True, slots=True)
class PriceEvidence:
    observations: int
    latest_close: float
    return_1d_pct: float | None
    return_5d: float | None
    return_20d: float | None
    return_63d: float | None
    ema_20: float | None
    ema_50: float | None
    ema_200: float | None
    realized_vol_20: float | None
    atr_14_pct: float | None
    volume_ratio_20: float | None
    rsi_14: float | None
    drawdown_126d_pct: float | None
    bars: tuple[tuple[str, float, float | None, float | None], ...]


@dataclass(frozen=True, slots=True)
class ChainEvidence:
    requested_sessions: int
    populated_sessions: int
    latest_market_date: date | None
    latest_valid_contract_count: int | None
    latest_call_open_interest: int | None
    latest_put_open_interest: int | None
    latest_median_implied_volatility: float | None


@dataclass(frozen=True, slots=True)
class GexEvidence:
    eligible_sessions: int
    latest_market_date: date | None
    call_wall: float | None
    put_wall: float | None
    gamma_flip: float | None
    gamma_magnet: float | None


@dataclass(frozen=True, slots=True)
class FlowEvidence:
    source: SourceRef | None
    alert_count: int
    timestamped_alert_count: int
    aggregate_reported_premium: float | None
    market_time_semantics: str


@dataclass(frozen=True, slots=True)
class OpenInterestEvidence:
    source_refs: tuple[SourceRef, ...]
    requested_sessions: int
    row_count: int
    rows_with_explicit_change: int
    aggregate_explicit_change: int | None
    latest_market_date: date | None
    latest_row_count: int
    latest_aggregate_explicit_change: int | None
    market_time_semantics: str


@dataclass(frozen=True, slots=True)
class DarkPoolEvidence:
    source_refs: tuple[SourceRef, ...]
    requested_sessions: int
    unique_print_count: int
    aggregate_reported_premium: float | None
    timestamped_print_count: int
    latest_market_date: date | None
    latest_unique_print_count: int
    latest_aggregate_reported_premium: float | None
    market_time_semantics: str


@dataclass(frozen=True, slots=True)
class NewsItem:
    headline: str
    source: str | None
    published_at: str | None


@dataclass(frozen=True, slots=True)
class NewsEvidence:
    source: SourceRef | None
    headline_count: int
    timestamped_headline_count: int
    latest_headlines: tuple[NewsItem, ...]
    market_time_semantics: str


@dataclass(frozen=True, slots=True)
class EarningsEvidence:
    source: SourceRef | None
    report_date: str | None
    report_time: str | None
    expected_move: float | None
    last_report_date: str | None
    last_report_time: str | None
    market_time_semantics: str


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """Compact cutoff-bound deterministic evidence for one ticker.

    ``source_refs`` is the complete lineage of the selected source snapshots;
    it is intentionally separate from compact aggregate fields so a later agent
    can cite evidence without silently receiving all licensed raw contracts.
    """

    ticker: str
    cutoff_at: datetime
    price: PriceEvidence | None
    chain: ChainEvidence
    gex: GexEvidence
    flow: FlowEvidence
    open_interest: OpenInterestEvidence
    dark_pool: DarkPoolEvidence
    news: NewsEvidence
    earnings: EarningsEvidence
    source_refs: tuple[SourceRef, ...]
    exclusions: tuple[str, ...]

    def to_agent_input(self) -> dict[str, object]:
        """Return JSON-ready, compact evidence with explicit non-predictive scope."""

        return {
            "ticker": self.ticker,
            "cutoff_at": timestamp_text(self.cutoff_at),
            "price": asdict(self.price) if self.price else None,
            "chain": {
                **asdict(self.chain),
                "latest_market_date": self.chain.latest_market_date.isoformat() if self.chain.latest_market_date else None,
            },
            "gex": {
                **asdict(self.gex),
                "latest_market_date": self.gex.latest_market_date.isoformat() if self.gex.latest_market_date else None,
            },
            "flow": {**asdict(self.flow), "source": self.flow.source.to_dict() if self.flow.source else None},
            "open_interest": {
                **asdict(self.open_interest),
                "latest_market_date": self.open_interest.latest_market_date.isoformat() if self.open_interest.latest_market_date else None,
                "source_refs": [source.to_dict() for source in self.open_interest.source_refs],
            },
            "dark_pool": {
                **asdict(self.dark_pool),
                "latest_market_date": self.dark_pool.latest_market_date.isoformat() if self.dark_pool.latest_market_date else None,
                "source_refs": [source.to_dict() for source in self.dark_pool.source_refs],
            },
            "news": {
                **asdict(self.news), "source": self.news.source.to_dict() if self.news.source else None,
            },
            "earnings": {**asdict(self.earnings), "source": self.earnings.source.to_dict() if self.earnings.source else None},
            "source_refs": [source.to_dict() for source in self.source_refs],
            "exclusions": list(self.exclusions),
            "scope": "deterministic historical evidence; not a recommendation or forecast",
        }


@dataclass(frozen=True, slots=True)
class _RawSnapshot:
    source: SourceRef
    metadata: Mapping[str, Any]
    payload: Any


class EvidenceReader:
    """Read immutable SQLite evidence without making changes to the database."""

    def __init__(self, database: str | Path) -> None:
        path = Path(database).resolve()
        # ``mode=ro`` preserves SQLite's WAL visibility.  ``immutable=1`` would
        # ignore an active WAL and can therefore miss just-inserted immutable
        # evidence until an external checkpoint occurs.
        self._connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self._connection.row_factory = sqlite3.Row

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "EvidenceReader":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _snapshots(self, ticker: str, dataset: str, cutoff_at: datetime) -> list[_RawSnapshot]:
        cutoff = utc_timestamp(cutoff_at, field_name="cutoff_at")
        rows = self._connection.execute(
            """
            SELECT s.id, s.dataset, s.as_of, s.retrieved_at, s.metadata_json, p.content_json
            FROM snapshots AS s JOIN raw_payloads AS p ON p.content_hash=s.raw_payload_hash
            WHERE s.symbol=? AND s.dataset=? AND s.as_of<=? AND s.retrieved_at<=?
            ORDER BY s.retrieved_at DESC, s.id DESC
            """,
            (ticker, dataset, timestamp_text(cutoff), timestamp_text(cutoff)),
        ).fetchall()
        snapshots: list[_RawSnapshot] = []
        for row in rows:
            as_of = timestamp_from_text(row["as_of"])
            metadata = json.loads(row["metadata_json"])
            payload = json.loads(row["content_json"])
            snapshots.append(_RawSnapshot(
                source=SourceRef(
                    snapshot_id=int(row["id"]), dataset=row["dataset"],
                    market_date=_provider_market_date(row["dataset"], metadata, payload, as_of), as_of=as_of,
                    retrieved_at=timestamp_from_text(row["retrieved_at"]),
                ),
                metadata=metadata, payload=payload,
            ))
        return snapshots

    @staticmethod
    def _newest_by_market_date(snapshots: Iterable[_RawSnapshot], cutoff_date: date) -> list[_RawSnapshot]:
        """Deduplicate provider retries: newest usable raw snapshot wins per date."""

        chosen: dict[date, _RawSnapshot] = {}
        for snapshot in snapshots:
            if snapshot.source.market_date <= cutoff_date:
                chosen.setdefault(snapshot.source.market_date, snapshot)
        return [chosen[key] for key in sorted(chosen)]

    def normalize_bars(self, ticker: str, *, cutoff_at: datetime) -> tuple[NormalizedBar, ...]:
        """Merge cutoff-safe rolling OHLC windows; newest source wins per date."""

        snapshots = self._snapshots(ticker, "ohlc", cutoff_at)
        if not snapshots:
            return ()
        cutoff_date = _utc_date(cutoff_at)
        by_date: dict[date, NormalizedBar] = {}
        for snapshot in snapshots:
            if (
                snapshot.metadata.get("backfill_plan_id") is not None
                and snapshot.metadata.get("historical_scope_verified") is not True
            ):
                continue
            for row in _payload_rows(snapshot.payload):
                if row.get("market_time") != "r" or not isinstance(row.get("date"), str):
                    continue
                try:
                    session = date.fromisoformat(row["date"])
                except ValueError:
                    continue
                if session in by_date:
                    continue
                values = [_number(row.get(name)) for name in ("open", "high", "low", "close", "volume")]
                if session > cutoff_date or any(value is None for value in values):
                    continue
                assert all(value is not None for value in values)
                try:
                    bar = DailyBar(session, values[0], values[1], values[2], values[3], values[4], snapshot.source.retrieved_at)
                except ValueError:
                    continue
                by_date[session] = NormalizedBar(
                    session, bar.open, bar.high, bar.low, bar.close, bar.volume,
                    snapshot.source.snapshot_id, snapshot.source.retrieved_at,
                )
        return tuple(by_date[key] for key in sorted(by_date))

    def normalize_chain(self, ticker: str, *, cutoff_at: datetime) -> tuple[ChainSession, ...]:
        cutoff_date = _utc_date(cutoff_at)
        sessions: list[ChainSession] = []
        for snapshot in self._newest_by_market_date(self._snapshots(ticker, "option_chain", cutoff_at), cutoff_date):
            contracts = _payload_rows(snapshot.payload)
            call_oi = put_oi = call_vol = put_vol = valid = 0
            valid_ivs: list[float] = []
            for contract in contracts:
                side = str(contract.get("option_type", contract.get("type", ""))).lower()
                strike = _number(contract.get("strike"))
                bid = _number(contract.get("nbbo_bid", contract.get("bid")))
                ask = _number(contract.get("nbbo_ask", contract.get("ask")))
                greeks = [_number(contract.get(name)) for name in ("delta", "gamma", "theta", "vega")]
                if side not in {"call", "put"} or strike is None or strike <= 0:
                    continue
                oi = int(_number(contract.get("open_interest")) or 0)
                volume = int(_number(contract.get("volume")) or 0)
                if side == "call":
                    call_oi += max(oi, 0); call_vol += max(volume, 0)
                else:
                    put_oi += max(oi, 0); put_vol += max(volume, 0)
                if bid is None or ask is None or bid <= 0 or ask < bid or any(value is None for value in greeks):
                    continue
                valid += 1
                iv = _number(contract.get("implied_volatility", contract.get("iv")))
                if iv is not None and iv > 0:
                    valid_ivs.append(iv)
            sessions.append(ChainSession(
                snapshot.source.market_date, snapshot.source, len(contracts), valid,
                call_oi, put_oi, call_vol, put_vol, median(valid_ivs) if valid_ivs else None,
            ))
        return tuple(sessions)

    def normalize_gex(self, ticker: str, *, cutoff_at: datetime) -> tuple[GexSession, ...]:
        cutoff_date = _utc_date(cutoff_at)
        sessions: list[GexSession] = []
        for snapshot in self._newest_by_market_date(self._snapshots(ticker, "dealer_exposure", cutoff_at), cutoff_date):
            if snapshot.metadata.get("derived_gex_eligible") is False:
                continue
            payload = _payload_object(snapshot.payload)
            levels = [_number(payload.get(name)) for name in _GEX_FIELDS]
            if any(value is None for value in levels):
                continue
            assert all(value is not None for value in levels)
            sessions.append(GexSession(snapshot.source.market_date, snapshot.source, *levels))
        return tuple(sessions)

    @staticmethod
    def _newest_by_page(snapshots: Iterable[_RawSnapshot], cutoff_date: date) -> list[_RawSnapshot]:
        """Retain distinct paginated captures while removing retry duplicates."""

        chosen: dict[tuple[date, object], _RawSnapshot] = {}
        for snapshot in snapshots:
            if snapshot.source.market_date <= cutoff_date:
                key = (snapshot.source.market_date, snapshot.metadata.get("page", 0))
                chosen.setdefault(key, snapshot)
        return [chosen[key] for key in sorted(chosen, key=lambda value: (value[0], str(value[1])))]

    def _latest_snapshot(self, ticker: str, dataset: str, cutoff_at: datetime) -> _RawSnapshot | None:
        snapshots = self._snapshots(ticker, dataset, cutoff_at)
        return snapshots[0] if snapshots else None

    def flow_evidence(self, ticker: str, *, cutoff_at: datetime) -> FlowEvidence:
        snapshot = self._latest_snapshot(ticker, "option_flow", cutoff_at)
        if snapshot is None:
            return FlowEvidence(None, 0, 0, None, "no cutoff-safe flow-alert snapshot")
        rows = _payload_rows(snapshot.payload)
        premiums = [_number(row.get("premium", row.get("total_premium"))) for row in rows]
        timestamps = sum(isinstance(row.get("created_at", row.get("executed_at")), str) for row in rows)
        values = [value for value in premiums if value is not None and value >= 0]
        return FlowEvidence(snapshot.source, len(rows), timestamps, sum(values) if values else None,
                            "provider alert created_at/executed_at when present; otherwise retrieval time only")

    def open_interest_evidence(self, ticker: str, *, cutoff_at: datetime) -> OpenInterestEvidence:
        snapshots = self._newest_by_page(self._snapshots(ticker, "open_interest", cutoff_at), _utc_date(cutoff_at))
        rows = [row for snapshot in snapshots for row in _payload_rows(snapshot.payload)]
        changes = [_number(row.get("open_interest_change", row.get("oi_change"))) for row in rows]
        usable = [value for value in changes if value is not None]
        latest_date = max((snapshot.source.market_date for snapshot in snapshots), default=None)
        latest_rows = [
            row for snapshot in snapshots if snapshot.source.market_date == latest_date
            for row in _payload_rows(snapshot.payload)
        ]
        latest_changes = [
            value for row in latest_rows
            if (value := _number(row.get("open_interest_change", row.get("oi_change")))) is not None
        ]
        return OpenInterestEvidence(tuple(snapshot.source for snapshot in snapshots), len({item.source.market_date for item in snapshots}),
                                    len(rows), len(usable), int(sum(usable)) if usable else None,
                                    latest_date, len(latest_rows), int(sum(latest_changes)) if latest_changes else None,
                                    "provider curr_date/last_date fields when present; paginated rows are observations, not a directional inference")

    def dark_pool_evidence(self, ticker: str, *, cutoff_at: datetime) -> DarkPoolEvidence:
        snapshots = self._newest_by_page(self._snapshots(ticker, "dark_pool", cutoff_at), _utc_date(cutoff_at))
        def unique_rows(source_rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
            seen: set[str] = set(); unique: list[Mapping[str, Any]] = []
            for index, row in enumerate(source_rows):
                identifier = row.get("tracking_id")
                key = str(identifier) if identifier not in (None, "") else f"row:{index}"
                if key not in seen:
                    seen.add(key); unique.append(row)
            return unique

        rows = [row for snapshot in snapshots for row in _payload_rows(snapshot.payload)]
        unique = unique_rows(rows)
        premiums = [_number(row.get("premium")) for row in unique]
        values = [value for value in premiums if value is not None and value >= 0]
        timestamped = sum(isinstance(row.get("executed_at", row.get("trf_executed_at")), str) for row in unique)
        latest_date = max((snapshot.source.market_date for snapshot in snapshots), default=None)
        latest = unique_rows([
            row for snapshot in snapshots if snapshot.source.market_date == latest_date
            for row in _payload_rows(snapshot.payload)
        ])
        latest_values = [
            value for row in latest if (value := _number(row.get("premium"))) is not None and value >= 0
        ]
        return DarkPoolEvidence(tuple(snapshot.source for snapshot in snapshots), len({item.source.market_date for item in snapshots}),
                                 len(unique), sum(values) if values else None, timestamped,
                                 latest_date, len(latest), sum(latest_values) if latest_values else None,
                                 "executed_at/TRF execution time when present; prints do not identify beneficial owner or direction")

    def news_evidence(self, ticker: str, *, cutoff_at: datetime) -> NewsEvidence:
        snapshot = self._latest_snapshot(ticker, "news", cutoff_at)
        if snapshot is None:
            return NewsEvidence(None, 0, 0, (), "no cutoff-safe news snapshot")
        rows = _payload_rows(snapshot.payload)
        ordered = sorted(
            rows,
            key=lambda row: str(row.get("created_at", row.get("published_at", ""))),
            reverse=True,
        )
        items = tuple(NewsItem(str(row.get("headline", row.get("title", "Untitled"))),
                               str(row["source"]) if row.get("source") is not None else None,
                               str(row["created_at"]) if row.get("created_at") is not None else None)
                      for row in ordered[:5])
        timestamped_count = sum(isinstance(row.get("created_at", row.get("published_at")), str) for row in rows)
        return NewsEvidence(snapshot.source, len(rows), timestamped_count, items,
                            "created_at is provider headline publication/creation time when present")

    def earnings_evidence(self, ticker: str, *, cutoff_at: datetime) -> EarningsEvidence:
        snapshot = self._latest_snapshot(ticker, "earnings", cutoff_at)
        if snapshot is None:
            return EarningsEvidence(None, None, None, None, None, None, "no cutoff-safe earnings snapshot")
        rows = _payload_rows(snapshot.payload)
        cutoff_date = _utc_date(cutoff_at).isoformat()
        dated = sorted(
            (row for row in rows if isinstance(row.get("report_date"), str)),
            key=lambda row: str(row["report_date"]),
        )
        upcoming = next((row for row in dated if row["report_date"] >= cutoff_date), None)
        past = next((row for row in reversed(dated) if row["report_date"] < cutoff_date), None)
        move = _number(upcoming.get("expected_move_perc", upcoming.get("expected_move"))) if upcoming else None
        return EarningsEvidence(
            snapshot.source,
            str(upcoming.get("report_date")) if upcoming else None,
            str(upcoming["report_time"]) if upcoming and upcoming.get("report_time") is not None else None,
            move,
            str(past.get("report_date")) if past else None,
            str(past["report_time"]) if past and past.get("report_time") is not None else None,
            "report_date/report_time identify the next cutoff-safe provider calendar event when available; "
            "last_report_date/last_report_time preserve the latest past event; expected move is not a directional forecast",
        )

    def bundle(self, ticker: str, *, cutoff_at: datetime) -> EvidenceBundle:
        """Build a compact, lineage-preserving bundle for one uppercase ticker."""

        symbol = ticker.strip().upper()
        if not symbol:
            raise ValueError("ticker must not be empty")
        cutoff = utc_timestamp(cutoff_at, field_name="cutoff_at")
        bars = self.normalize_bars(symbol, cutoff_at=cutoff)
        chains = self.normalize_chain(symbol, cutoff_at=cutoff)
        gex = self.normalize_gex(symbol, cutoff_at=cutoff)
        exclusions: list[str] = []
        price: PriceEvidence | None = None
        if bars:
            trend: TrendFeatures = build_trend_features(
                [DailyBar(bar.session_date, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.available_at) for bar in bars],
                cutoff_at=cutoff,
            )
            closes = [bar.close for bar in bars]
            ema20 = _ema_series(closes, 20); ema50 = _ema_series(closes, 50)
            trailing = closes[-126:]
            price = PriceEvidence(
                trend.observations, trend.close,
                (closes[-1] / closes[-2] - 1) * 100 if len(closes) > 1 else None,
                trend.return_5d, trend.return_20d,
                trend.return_63d, trend.ema_20, trend.ema_50, trend.ema_200,
                trend.realized_vol_20, trend.atr_14_pct, trend.volume_ratio_20,
                _rsi_14(closes),
                (closes[-1] / max(trailing) - 1) * 100 if len(trailing) >= 126 else None,
                tuple((bar.session_date.isoformat(), bar.close, ema20[index], ema50[index])
                      for index, bar in list(enumerate(bars))[-126:]),
            )
        else:
            exclusions.append("no cutoff-safe regular-session OHLC bars")
        latest_chain = chains[-1] if chains else None
        latest_gex = gex[-1] if gex else None
        flow = self.flow_evidence(symbol, cutoff_at=cutoff)
        open_interest = self.open_interest_evidence(symbol, cutoff_at=cutoff)
        dark_pool = self.dark_pool_evidence(symbol, cutoff_at=cutoff)
        news = self.news_evidence(symbol, cutoff_at=cutoff)
        earnings = self.earnings_evidence(symbol, cutoff_at=cutoff)
        if not chains:
            exclusions.append("no cutoff-safe option-chain sessions")
        elif latest_chain and latest_chain.valid_contract_count == 0:
            exclusions.append("latest option chain has no valid NBBO-and-Greeks contracts")
        if not gex:
            exclusions.append("no eligible complete GEX sessions")
        if flow.source is None:
            exclusions.append("no cutoff-safe flow-alert snapshot")
        if not open_interest.source_refs:
            exclusions.append("no cutoff-safe open-interest-change snapshot")
        if not dark_pool.source_refs:
            exclusions.append("no cutoff-safe dark-pool snapshot")
        if news.source is None:
            exclusions.append("no cutoff-safe news snapshot")
        if earnings.source is None:
            exclusions.append("no cutoff-safe earnings snapshot")
        # Price uses one selected response; retain its exact source alongside
        # every deduplicated option/GEX session selected above.
        source_refs: dict[int, SourceRef] = {}
        raw_ohlc = self._snapshots(symbol, "ohlc", cutoff)
        if bars and raw_ohlc:
            source_refs[raw_ohlc[0].source.snapshot_id] = raw_ohlc[0].source
        for session in (*chains, *gex):
            source_refs[session.source.snapshot_id] = session.source
        for source in (flow.source, news.source, earnings.source, *open_interest.source_refs, *dark_pool.source_refs):
            if source is not None:
                source_refs[source.snapshot_id] = source
        return EvidenceBundle(
            ticker=symbol, cutoff_at=cutoff, price=price,
            chain=ChainEvidence(
                len(chains), sum(item.raw_contract_count > 0 for item in chains),
                latest_chain.market_date if latest_chain else None,
                latest_chain.valid_contract_count if latest_chain else None,
                latest_chain.call_open_interest if latest_chain else None,
                latest_chain.put_open_interest if latest_chain else None,
                latest_chain.median_implied_volatility if latest_chain else None,
            ),
            gex=GexEvidence(
                len(gex), latest_gex.market_date if latest_gex else None,
                latest_gex.call_wall if latest_gex else None, latest_gex.put_wall if latest_gex else None,
                latest_gex.gamma_flip if latest_gex else None, latest_gex.gamma_magnet if latest_gex else None,
            ),
            flow=flow, open_interest=open_interest, dark_pool=dark_pool, news=news, earnings=earnings,
            source_refs=tuple(source_refs[key] for key in sorted(source_refs)),
            exclusions=tuple(exclusions),
        )


def build_evidence_bundle(database: str | Path, ticker: str, *, cutoff_at: datetime) -> EvidenceBundle:
    """Convenience one-shot reader for immutable local evidence."""

    with EvidenceReader(database) as reader:
        return reader.bundle(ticker, cutoff_at=cutoff_at)
