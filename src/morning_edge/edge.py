"""Cutoff-safe derived research features from immutable Codex Screener evidence.

This module deliberately produces research context, not trade authorization.
Every result names the immutable snapshot IDs used, exposes sample sizes, and
keeps empirical analog frequencies separate from calibrated probabilities.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
import json
from math import erf, exp, isfinite, log, sqrt
from pathlib import Path
import sqlite3
from statistics import fmean, median, pstdev
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .challengers import shadow_challengers
from .clock import next_nyse_session
from .models import timestamp_text, utc_timestamp


EDGE_FEATURE_VERSION = "edge-research-v2"
ANALOG_MODEL_VERSION = "nearest-analog-v3"
FORECAST_MODEL_VERSION = "analog-path-ensemble-v3"
FORECAST_V4_MODEL_VERSION = "volatility-scaled-analog-ensemble-v4"
NEW_YORK = ZoneInfo("America/New_York")
GEX_LEVEL_METHOD_CHANGE_DATE = date(2026, 8, 22)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _rows(payload: Any) -> list[Mapping[str, Any]]:
    data = payload.get("data", payload) if isinstance(payload, Mapping) else payload
    return [row for row in data if isinstance(row, Mapping)] if isinstance(data, list) else []


def _payload_object(payload: Any) -> Mapping[str, Any]:
    data = payload.get("data", payload) if isinstance(payload, Mapping) else None
    return data if isinstance(data, Mapping) else {}


def _expiry(row: Mapping[str, Any]) -> date | None:
    for key in ("expiry", "expires", "expiration", "expiration_date", "exp_date"):
        value = row.get(key)
        if isinstance(value, str):
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                continue
    return None


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _percentile(value: float | None, history: Sequence[float]) -> float | None:
    if value is None or not history:
        return None
    less = sum(item < value for item in history)
    equal = sum(item == value for item in history)
    return (less + 0.5 * equal) / len(history)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _normal_cdf(value: float) -> float:
    return 0.5 * (1 + erf(value / sqrt(2)))


@dataclass(frozen=True, slots=True)
class RawSnapshot:
    snapshot_id: int
    dataset: str
    market_date: date
    as_of: str
    retrieved_at: str
    metadata: Mapping[str, Any]
    payload: Any


class EdgeAnalyzer:
    """Read-only research feature builder over the append-only snapshot store."""

    def __init__(self, database: str | Path) -> None:
        self.path = Path(database).resolve()
        self._connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self._connection.row_factory = sqlite3.Row
        self._snapshot_cache: dict[tuple[str, str, str], tuple[RawSnapshot, ...]] = {}

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "EdgeAnalyzer":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _market_date(metadata: Mapping[str, Any], payload: Any, as_of: str) -> date:
        requested = metadata.get("requested_market_date")
        if isinstance(requested, str):
            try:
                return date.fromisoformat(requested[:10])
            except ValueError:
                pass
        obj = _payload_object(payload)
        for key in ("date", "market_date"):
            value = obj.get(key)
            if isinstance(value, str):
                try:
                    return date.fromisoformat(value[:10])
                except ValueError:
                    pass
        try:
            observed = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if observed.tzinfo is not None:
                return observed.astimezone(NEW_YORK).date()
        except ValueError:
            pass
        return date.fromisoformat(as_of[:10])

    def snapshots(self, ticker: str, dataset: str, cutoff_at: datetime) -> list[RawSnapshot]:
        cutoff = timestamp_text(utc_timestamp(cutoff_at, field_name="cutoff_at"))
        cache_key = (ticker.strip().upper(), dataset, cutoff)
        cached = self._snapshot_cache.get(cache_key)
        if cached is not None:
            return list(cached)
        rows = self._connection.execute(
            """
            SELECT s.id, s.dataset, s.as_of, s.retrieved_at, s.metadata_json, p.content_json
            FROM snapshots AS s JOIN raw_payloads AS p ON p.content_hash=s.raw_payload_hash
            WHERE s.symbol=? AND s.dataset=? AND s.as_of<=? AND s.retrieved_at<=?
            ORDER BY s.retrieved_at DESC, s.id DESC
            """,
            (cache_key[0], dataset, cutoff, cutoff),
        ).fetchall()
        result: list[RawSnapshot] = []
        for row in rows:
            metadata = json.loads(row["metadata_json"])
            payload = json.loads(row["content_json"])
            result.append(
                RawSnapshot(
                    int(row["id"]), row["dataset"],
                    self._market_date(metadata, payload, row["as_of"]),
                    row["as_of"], row["retrieved_at"], metadata, payload,
                )
            )
        self._snapshot_cache[cache_key] = tuple(result)
        return list(result)

    @staticmethod
    def newest_per_date(snapshots: Iterable[RawSnapshot]) -> list[RawSnapshot]:
        chosen: dict[date, RawSnapshot] = {}
        for snapshot in snapshots:
            chosen.setdefault(snapshot.market_date, snapshot)
        return [chosen[key] for key in sorted(chosen)]

    @staticmethod
    def pages_per_date(snapshots: Iterable[RawSnapshot]) -> dict[date, list[RawSnapshot]]:
        chosen: dict[tuple[date, str], RawSnapshot] = {}
        for snapshot in snapshots:
            page = str(snapshot.metadata.get("page", snapshot.metadata.get("cursor", 0)))
            chosen.setdefault((snapshot.market_date, page), snapshot)
        grouped: dict[date, list[RawSnapshot]] = defaultdict(list)
        for (market_date, _page), snapshot in chosen.items():
            grouped[market_date].append(snapshot)
        return dict(sorted(grouped.items()))

    @staticmethod
    def _surface_for_rows(
        rows: Sequence[Mapping[str, Any]], *, spot: float | None, market_date: date,
    ) -> dict[str, Any]:
        contracts: list[dict[str, Any]] = []
        for row in rows:
            side = str(row.get("option_type", row.get("type", ""))).lower()
            expiry = _expiry(row)
            strike = _number(row.get("strike"))
            iv = _number(row.get("implied_volatility", row.get("iv")))
            bid = _number(row.get("nbbo_bid", row.get("bid")))
            ask = _number(row.get("nbbo_ask", row.get("ask")))
            delta = _number(row.get("delta"))
            oi = _number(row.get("open_interest"))
            volume = _number(row.get("volume"))
            if side not in {"call", "put"} or expiry is None or strike is None or strike <= 0 or iv is None or iv <= 0:
                continue
            dte = (expiry - market_date).days
            if dte < 0 or dte > 730:
                continue
            spread = None
            if bid is not None and ask is not None and bid >= 0 and ask >= bid and bid + ask > 0:
                spread = (ask - bid) / ((ask + bid) / 2)
            contracts.append({
                "side": side, "expiry": expiry, "dte": dte, "strike": strike, "iv": iv,
                "delta": delta, "spread": spread, "oi": max(0.0, oi or 0.0),
                "volume": max(0.0, volume or 0.0),
            })
        expiries: list[dict[str, Any]] = []
        for expiry in sorted({item["expiry"] for item in contracts}):
            expiry_rows = [item for item in contracts if item["expiry"] == expiry]
            calls = [item for item in expiry_rows if item["side"] == "call"]
            puts = [item for item in expiry_rows if item["side"] == "put"]
            if not calls or not puts:
                continue
            reference = spot if spot and spot > 0 else median(item["strike"] for item in expiry_rows)
            call = min(calls, key=lambda item: abs(item["strike"] - reference))
            put = min(puts, key=lambda item: abs(item["strike"] - reference))
            atm_iv = fmean((call["iv"], put["iv"]))
            expiries.append({
                "expiry": expiry.isoformat(), "dte": call["dte"], "atm_iv": atm_iv,
                "implied_move_pct": atm_iv * sqrt(max(call["dte"], 1) / 365),
            })
        front = next((item for item in expiries if item["dte"] >= 7), expiries[0] if expiries else None)
        back = next((item for item in expiries if item["dte"] >= 60), expiries[-1] if expiries else None)
        target_expiry = min(expiries, key=lambda item: abs(item["dte"] - 45)) if expiries else None
        put_skew = None
        if target_expiry:
            expiry_value = date.fromisoformat(target_expiry["expiry"])
            target_rows = [item for item in contracts if item["expiry"] == expiry_value and item["delta"] is not None]
            calls = [item for item in target_rows if item["side"] == "call"]
            puts = [item for item in target_rows if item["side"] == "put"]
            if calls and puts:
                call_25 = min(calls, key=lambda item: abs(item["delta"] - 0.25))
                put_25 = min(puts, key=lambda item: abs(item["delta"] + 0.25))
                put_skew = put_25["iv"] - call_25["iv"]
        liquid = [
            item for item in contracts
            if 30 <= item["dte"] <= 240 and item["spread"] is not None
            and (spot is None or abs(item["strike"] / spot - 1) <= 0.20)
        ]
        return {
            "contract_count": len(contracts), "expiries": expiries,
            "front_iv": front["atm_iv"] if front else None,
            "front_dte": front["dte"] if front else None,
            "back_iv": back["atm_iv"] if back else None,
            "back_dte": back["dte"] if back else None,
            "term_slope": (back["atm_iv"] - front["atm_iv"]) if front and back else None,
            "put_call_skew_25d": put_skew,
            "median_spread_pct": median(item["spread"] for item in liquid) if liquid else None,
            "median_open_interest": median(item["oi"] for item in liquid) if liquid else None,
            "median_volume": median(item["volume"] for item in liquid) if liquid else None,
            "liquid_contract_count": len(liquid),
        }

    def option_surface(
        self, ticker: str, cutoff_at: datetime, *, spot: float | None,
        bars: Sequence[Mapping[str, Any]], realized_vol_20: float | None,
    ) -> dict[str, Any]:
        snapshots = self.newest_per_date(self.snapshots(ticker, "option_chain", cutoff_at))
        if not snapshots:
            return {"status": "UNAVAILABLE", "history_sessions": 0, "source_snapshot_ids": []}
        closes = {date.fromisoformat(str(item["date"])[:10]): _number(item.get("close")) for item in bars}
        close_dates = sorted(closes)

        def price_for(day: date) -> float | None:
            eligible = [item for item in close_dates if item <= day and closes[item] is not None]
            return closes[eligible[-1]] if eligible else spot

        history: list[dict[str, Any]] = []
        for snapshot in snapshots:
            surface = self._surface_for_rows(_rows(snapshot.payload), spot=price_for(snapshot.market_date), market_date=snapshot.market_date)
            if surface.get("front_iv") is not None:
                history.append({
                    "date": snapshot.market_date.isoformat(), "snapshot_id": snapshot.snapshot_id,
                    "front_iv": surface.get("front_iv"), "front_dte": surface.get("front_dte"),
                    "back_iv": surface.get("back_iv"), "back_dte": surface.get("back_dte"),
                    "term_slope": surface.get("term_slope"),
                    "put_call_skew_25d": surface.get("put_call_skew_25d"),
                    "median_spread_pct": surface.get("median_spread_pct"),
                    "median_open_interest": surface.get("median_open_interest"),
                    "median_volume": surface.get("median_volume"),
                })
        latest_snapshot = snapshots[-1]
        latest = self._surface_for_rows(_rows(latest_snapshot.payload), spot=spot, market_date=latest_snapshot.market_date)
        iv_history = [item["front_iv"] for item in history[:-1] if item.get("front_iv") is not None]
        latest_iv = latest.get("front_iv")
        latest.update({
            "status": "RESEARCH_ONLY",
            "market_date": latest_snapshot.market_date.isoformat(),
            "history_sessions": len(history),
            "iv_percentile": _percentile(latest_iv, iv_history[-90:]),
            "iv_rv_gap": (latest_iv - realized_vol_20) if latest_iv is not None and realized_vol_20 is not None else None,
            "history": history,
            "source_snapshot_ids": [item.snapshot_id for item in snapshots],
            "method": "ATM call/put IV by expiry; 25-delta skew and liquidity from stored full chains",
        })
        return latest

    def oi_structure(self, ticker: str, cutoff_at: datetime, *, spot: float | None) -> dict[str, Any]:
        snapshots = self.newest_per_date(self.snapshots(ticker, "option_chain", cutoff_at))
        if len(snapshots) < 2:
            return {"status": "INSUFFICIENT_HISTORY", "history_sessions": len(snapshots), "source_snapshot_ids": [item.snapshot_id for item in snapshots]}

        def contract_map(snapshot: RawSnapshot) -> dict[str, tuple[str, float, float]]:
            result: dict[str, tuple[str, float, float]] = {}
            for row in _rows(snapshot.payload):
                symbol = row.get("option_symbol")
                side = str(row.get("option_type", row.get("type", ""))).lower()
                strike = _number(row.get("strike"))
                oi = _number(row.get("open_interest"))
                if isinstance(symbol, str) and side in {"call", "put"} and strike and oi is not None and oi >= 0:
                    result[symbol] = (side, strike, oi)
            return result

        previous, current = snapshots[-2], snapshots[-1]
        prior_map, current_map = contract_map(previous), contract_map(current)
        changes: list[tuple[str, float, float]] = []
        for symbol, (side, strike, oi) in current_map.items():
            old = prior_map.get(symbol)
            if old is not None:
                changes.append((side, strike, oi - old[2]))
        call_change = sum(change for side, _strike, change in changes if side == "call")
        put_change = sum(change for side, _strike, change in changes if side == "put")
        near = [item for item in changes if spot and abs(item[1] / spot - 1) <= 0.10]
        by_call: dict[float, float] = defaultdict(float)
        by_put: dict[float, float] = defaultdict(float)
        for side, strike, change in changes:
            (by_call if side == "call" else by_put)[strike] += change
        current_oi = [item[2] for item in current_map.values() if item[2] > 0]
        total_oi = sum(current_oi)
        concentration = sum((item / total_oi) ** 2 for item in current_oi) if total_oi else None
        return {
            "status": "DERIVED_FROM_CONSECUTIVE_CHAINS",
            "market_date": current.market_date.isoformat(), "previous_market_date": previous.market_date.isoformat(),
            "matched_contracts": len(changes), "call_oi_change": call_change, "put_oi_change": put_change,
            "net_call_minus_put_change": call_change - put_change,
            "near_spot_call_change": sum(change for side, _strike, change in near if side == "call"),
            "near_spot_put_change": sum(change for side, _strike, change in near if side == "put"),
            "largest_call_build_strike": max(by_call, key=by_call.get) if by_call else None,
            "largest_put_build_strike": max(by_put, key=by_put.get) if by_put else None,
            "contract_oi_concentration": concentration, "history_sessions": len(snapshots),
            "source_snapshot_ids": [previous.snapshot_id, current.snapshot_id],
            "method": "Per-contract OI difference between consecutive full-chain snapshots; does not infer buyer intent",
        }

    @staticmethod
    def _flow_summary(snapshot: RawSnapshot) -> dict[str, Any]:
        dated_rows: list[tuple[date, Mapping[str, Any]]] = []
        undated_rows: list[Mapping[str, Any]] = []
        for row in _rows(snapshot.payload):
            observed_date = None
            for key in ("created_at", "executed_at"):
                observed_at = row.get(key)
                if not isinstance(observed_at, str):
                    continue
                try:
                    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if observed.tzinfo is not None:
                    observed_date = observed.astimezone(NEW_YORK).date()
                    break
            if observed_date is None:
                undated_rows.append(row)
            else:
                dated_rows.append((observed_date, row))
        if dated_rows:
            market_date = max(item[0] for item in dated_rows)
            rows = [row for row_date, row in dated_rows if row_date == market_date]
        else:
            market_date = snapshot.market_date
            rows = undated_rows
        gross = directional = opening_weight = single_weight = sweep_weight = multileg_weight = 0.0
        signed_iv_change = total_volume = 0.0
        ratios: list[float] = []
        for row in rows:
            premium = max(0.0, _number(row.get("total_premium", row.get("premium"))) or 0.0)
            ask = max(0.0, _number(row.get("total_ask_side_prem")) or 0.0)
            bid = max(0.0, _number(row.get("total_bid_side_prem")) or 0.0)
            side = str(row.get("type", row.get("option_type", ""))).lower()
            sign = 1.0 if side == "call" else -1.0 if side == "put" else 0.0
            gross += premium
            directional += sign * (ask - bid)
            weight = premium or max(1.0, _number(row.get("total_size")) or 1.0)
            opening_weight += weight if row.get("all_opening_trades") is True else 0.0
            single_weight += weight if row.get("has_singleleg") is True and row.get("has_multileg") is not True else 0.0
            sweep_weight += weight if row.get("has_sweep") is True else 0.0
            multileg_weight += weight if row.get("has_multileg") is True else 0.0
            start_iv, end_iv = _number(row.get("iv_start")), _number(row.get("iv_end"))
            if start_iv is not None and end_iv is not None:
                signed_iv_change += sign * (end_iv - start_iv) * weight
            total_volume += max(0.0, _number(row.get("volume", row.get("total_size"))) or 0.0)
            ratio = _number(row.get("volume_oi_ratio"))
            if ratio is not None and ratio >= 0:
                ratios.append(ratio)
        denominator = gross if gross > 0 else max(1.0, float(len(rows)))
        return {
            "market_date": market_date.isoformat(), "alert_count": len(rows),
            "gross_premium": gross, "directional_premium": directional,
            "directional_share": directional / gross if gross else None,
            "opening_share": opening_weight / denominator, "single_leg_share": single_weight / denominator,
            "sweep_share": sweep_weight / denominator, "multileg_share": multileg_weight / denominator,
            "signed_iv_change": signed_iv_change / denominator, "total_volume": total_volume,
            "median_volume_oi_ratio": median(ratios) if ratios else None,
            "snapshot_id": snapshot.snapshot_id,
        }

    def flow_conviction(self, ticker: str, cutoff_at: datetime) -> dict[str, Any]:
        snapshots = self.snapshots(ticker, "option_flow", cutoff_at)
        if not snapshots:
            return {"status": "UNAVAILABLE", "history_sessions": 0, "source_snapshot_ids": []}
        newest_by_provider_date: dict[str, dict[str, Any]] = {}
        for snapshot in snapshots:
            summary = self._flow_summary(snapshot)
            newest_by_provider_date.setdefault(summary["market_date"], summary)
        history = [newest_by_provider_date[key] for key in sorted(newest_by_provider_date)]
        latest = dict(history[-1])
        historical = [item["directional_premium"] for item in history[:-1]]
        zscore = None
        if len(historical) >= 5 and pstdev(historical) > 0:
            zscore = (latest["directional_premium"] - fmean(historical)) / pstdev(historical)
        quality = max(0.0, latest["opening_share"]) * max(0.0, latest["single_leg_share"])
        quality *= max(0.0, 1 - latest["multileg_share"])
        latest.update({
            "status": "PROVISIONAL_UNCONFIRMED",
            "history_sessions": len(history), "directional_percentile": _percentile(abs(latest["directional_premium"]), [abs(item) for item in historical]),
            "directional_zscore": zscore, "quality_multiplier": min(1.0, quality),
            "oi_confirmed": False, "oi_confirmation_ratio": None,
            "history": history, "source_snapshot_ids": [item["snapshot_id"] for item in history],
            "method": "Ask-minus-bid call premium minus ask-minus-bid put premium; multi-leg ambiguity penalized",
        })
        return latest

    def gex_topology(self, ticker: str, cutoff_at: datetime, *, spot: float | None) -> dict[str, Any]:
        snapshots = self.newest_per_date(self.snapshots(ticker, "dealer_exposure", cutoff_at))
        rows: list[dict[str, Any]] = []
        for snapshot in snapshots:
            obj = _payload_object(snapshot.payload)
            levels = {key: _number(obj.get(key)) for key in ("call_wall", "put_wall", "gamma_flip", "gamma_magnet")}
            if any(value is None for value in levels.values()):
                continue
            nearby = obj.get("nearby_flips")
            method_version = (
                "spot_directionalized_volume_v2"
                if snapshot.market_date >= GEX_LEVEL_METHOD_CHANGE_DATE
                else "legacy_static_oi_cumulative_v1"
            )
            rows.append({
                "date": snapshot.market_date.isoformat(), "snapshot_id": snapshot.snapshot_id, **levels,
                "nearby_flips": nearby if isinstance(nearby, list) else [],
                "provider_source": str(obj.get("source") or "unknown"),
                "provider_time": str(obj.get("time") or ""),
                "method_version": method_version,
            })
        if not rows:
            return {"status": "UNAVAILABLE", "history_sessions": 0, "source_snapshot_ids": []}
        latest = dict(rows[-1])
        comparable_rows = [row for row in rows if row["method_version"] == latest["method_version"]]
        previous = comparable_rows[-2] if len(comparable_rows) > 1 else None
        flip = latest["gamma_flip"]
        latest.update({
            "status": "MODELED_POSITIONING_REFERENCE", "history_sessions": len(rows),
            "comparable_history_sessions": len(comparable_rows),
            "comparison_status": "COMPARABLE" if previous else "INSUFFICIENT_COMPARABLE_HISTORY",
            "method_boundary_date": GEX_LEVEL_METHOD_CHANGE_DATE.isoformat(),
            "spot_regime": "ABOVE_FLIP" if spot is not None and flip is not None and spot > flip else "BELOW_FLIP" if spot is not None and flip is not None else "UNKNOWN",
            "distance_to_flip_pct": (spot / flip - 1) if spot and flip else None,
            "distance_to_call_wall_pct": (latest["call_wall"] / spot - 1) if spot else None,
            "distance_to_put_wall_pct": (latest["put_wall"] / spot - 1) if spot else None,
            "flip_change": (flip - previous["gamma_flip"]) if previous and flip is not None else None,
            "call_wall_change": (latest["call_wall"] - previous["call_wall"]) if previous else None,
            "put_wall_change": (latest["put_wall"] - previous["put_wall"]) if previous else None,
            "nearby_flip_count": len(latest["nearby_flips"]), "history": rows,
            "source_snapshot_ids": [item["snapshot_id"] for item in rows],
            "method": (
                "Provider gex-levels history. Observations on or after 2026-08-22 use spot directionalized "
                "volume; earlier observations use the legacy static open-interest cumulative method. "
                "Cross-boundary level changes are suppressed. Levels are not verified dealer inventory or "
                "deterministic support/resistance."
            ),
        })
        return latest

    @staticmethod
    def _unique_dark_rows(snapshots: Sequence[RawSnapshot]) -> list[Mapping[str, Any]]:
        unique: dict[str, Mapping[str, Any]] = {}
        for snapshot in snapshots:
            for index, row in enumerate(_rows(snapshot.payload)):
                key = str(row.get("tracking_id") or f"{snapshot.snapshot_id}:{index}")
                unique.setdefault(key, row)
        return list(unique.values())

    @staticmethod
    def _dark_level(rows: Sequence[Mapping[str, Any]], spot: float | None) -> dict[str, Any]:
        bucket_size = max(0.01, (spot or 100.0) * 0.0025)
        premium_by_level: dict[float, float] = defaultdict(float)
        total = near_bid = near_ask = midpoint = 0.0
        valid = 0
        for row in rows:
            price, premium = _number(row.get("price")), _number(row.get("premium"))
            if price is None or price <= 0 or premium is None or premium < 0:
                continue
            level = round(price / bucket_size) * bucket_size
            premium_by_level[level] += premium
            total += premium; valid += 1
            bid, ask = _number(row.get("nbbo_bid")), _number(row.get("nbbo_ask"))
            if bid is not None and ask is not None and ask >= bid:
                tolerance = max(0.005, (ask - bid) * 0.15)
                if price <= bid + tolerance:
                    near_bid += premium
                elif price >= ask - tolerance:
                    near_ask += premium
                else:
                    midpoint += premium
        dominant = max(premium_by_level, key=premium_by_level.get) if premium_by_level else None
        concentration = premium_by_level.get(dominant, 0.0) / total if dominant is not None and total else None
        return {
            "print_count": valid, "aggregate_premium": total or None,
            "dominant_price_level": dominant, "dominant_level_share": concentration,
            "near_bid_premium_share": near_bid / total if total else None,
            "near_ask_premium_share": near_ask / total if total else None,
            "midpoint_premium_share": midpoint / total if total else None,
        }

    def dark_pool_structure(
        self, ticker: str, cutoff_at: datetime, *, spot: float | None,
        average_daily_dollar_volume: float | None,
    ) -> dict[str, Any]:
        grouped = self.pages_per_date(self.snapshots(ticker, "dark_pool", cutoff_at))
        history: list[dict[str, Any]] = []
        for market_date, snapshots in list(grouped.items())[-20:]:
            summary = self._dark_level(self._unique_dark_rows(snapshots), spot)
            history.append({"date": market_date.isoformat(), "snapshot_ids": [item.snapshot_id for item in snapshots], **summary})
        if not history:
            return {"status": "UNAVAILABLE", "history_sessions": 0, "source_snapshot_ids": []}
        latest = dict(history[-1]); previous = history[-2] if len(history) > 1 else None
        level = latest.get("dominant_price_level")
        latest.update({
            "status": "PRICE_RESPONSE_CONTEXT_ONLY", "history_sessions": len(history),
            "dominant_level_change": (level - previous["dominant_price_level"]) if previous and level is not None and previous.get("dominant_price_level") is not None else None,
            "distance_to_dominant_level_pct": (spot / level - 1) if spot and level else None,
            "price_state": "ABOVE_LEVEL" if spot and level and spot > level else "BELOW_LEVEL" if spot and level else "UNKNOWN",
            "premium_to_adv_ratio": latest.get("aggregate_premium") / average_daily_dollar_volume if latest.get("aggregate_premium") and average_daily_dollar_volume else None,
            "history": history,
            "source_snapshot_ids": sorted({sid for item in history for sid in item["snapshot_ids"]}),
            "method": "Premium-weighted 0.25%-of-spot price buckets; print location does not identify beneficial owner or intent",
        })
        return latest

    def earnings_priors(self, ticker: str, cutoff_at: datetime) -> dict[str, Any]:
        snapshots = self.snapshots(ticker, "earnings", cutoff_at)
        if not snapshots:
            return {"status": "UNAVAILABLE", "event_count": 0, "source_snapshot_ids": []}
        snapshot = snapshots[0]
        events = []
        for row in _rows(snapshot.payload):
            report_date = row.get("report_date")
            if not isinstance(report_date, str):
                continue
            expected = _number(row.get("expected_move_perc"))
            actual = _number(row.get("post_earnings_move_1d"))
            straddle_1d = _number(row.get("long_straddle_1d"))
            straddle_1w = _number(row.get("long_straddle_1w"))
            actual_eps = _number(row.get("actual_eps")); estimate = _number(row.get("street_mean_est"))
            events.append({"report_date": report_date[:10], "expected_move_pct": expected, "actual_move_1d": actual,
                           "long_straddle_1d": straddle_1d, "long_straddle_1w": straddle_1w,
                           "eps_surprise_pct": (actual_eps / abs(estimate) - 1) if actual_eps is not None and estimate not in (None, 0) else None})
        today = utc_timestamp(cutoff_at, field_name="cutoff_at").date()
        historical = [item for item in events if item["report_date"] < today.isoformat()]
        future = sorted((item for item in events if item["report_date"] >= today.isoformat()), key=lambda item: item["report_date"])
        comparable = [item for item in historical if item["expected_move_pct"] is not None and item["actual_move_1d"] is not None]
        straddle_1d = [item["long_straddle_1d"] for item in historical if item["long_straddle_1d"] is not None]
        straddle_1w = [item["long_straddle_1w"] for item in historical if item["long_straddle_1w"] is not None]
        actual_moves = [abs(item["actual_move_1d"]) for item in historical if item["actual_move_1d"] is not None]
        return {
            "status": "DESCRIPTIVE_EVENT_PRIOR", "event_count": len(historical),
            "comparable_event_count": len(comparable), "next_event": future[0] if future else None,
            "implied_move_exceed_rate": sum(abs(item["actual_move_1d"]) > abs(item["expected_move_pct"]) for item in comparable) / len(comparable) if comparable else None,
            "median_absolute_move_1d": median(actual_moves) if actual_moves else None,
            "median_long_straddle_return_1d": median(straddle_1d) if straddle_1d else None,
            "median_long_straddle_return_1w": median(straddle_1w) if straddle_1w else None,
            "source_snapshot_ids": [snapshot.snapshot_id],
            "method": "Ticker-specific historical earnings outcomes; small samples are descriptive, not calibrated forecasts",
        }

    def news_signal(self, ticker: str, cutoff_at: datetime) -> dict[str, Any]:
        snapshots = self.snapshots(ticker, "news", cutoff_at)
        if not snapshots:
            return {"status": "UNAVAILABLE", "headline_count": 0, "source_snapshot_ids": []}
        snapshot = snapshots[0]; rows = _rows(snapshot.payload)
        scores: list[float] = []
        tags: set[str] = set(); sources: set[str] = set(); major = 0
        for row in rows:
            sentiment = row.get("sentiment")
            if isinstance(sentiment, str):
                scores.append({"positive": 1.0, "bullish": 1.0, "negative": -1.0, "bearish": -1.0, "neutral": 0.0}.get(sentiment.lower(), 0.0))
            elif (value := _number(sentiment)) is not None:
                scores.append(max(-1.0, min(1.0, value)))
            if row.get("is_major") is True:
                major += 1
            if isinstance(row.get("source"), str):
                sources.add(str(row["source"]))
            if isinstance(row.get("tags"), list):
                tags.update(str(item) for item in row["tags"] if isinstance(item, (str, int)))
        return {
            "status": "CONTEXT_ONLY_UNCALIBRATED", "headline_count": len(rows), "major_count": major,
            "mean_provider_sentiment": fmean(scores) if scores else None, "positive_count": sum(item > 0 for item in scores),
            "negative_count": sum(item < 0 for item in scores), "source_count": len(sources), "tags": sorted(tags)[:20],
            "history_sessions": 1, "source_snapshot_ids": [snapshot.snapshot_id],
            "method": "Provider sentiment and tags are attention context until a timestamped outcome history exists",
        }

    @staticmethod
    def _ema_series(values: Sequence[float], window: int) -> list[float | None]:
        result: list[float | None] = [None] * len(values)
        if len(values) < window:
            return result
        current = fmean(values[:window]); result[window - 1] = current
        weight = 2 / (window + 1)
        for index in range(window, len(values)):
            current = values[index] * weight + current * (1 - weight); result[index] = current
        return result

    def historical_analogs(
        self, *, bars: Sequence[Mapping[str, Any]], surface: Mapping[str, Any],
        gex: Mapping[str, Any], limit: int = 15, lookback_sessions: int = 504,
        selection_embargo_sessions: int = 20,
    ) -> dict[str, Any]:
        clean = [(date.fromisoformat(str(row["date"])[:10]), _number(row.get("close"))) for row in bars if _number(row.get("close")) is not None]
        clean = [(day, close) for day, close in clean if close is not None]
        if len(clean) < 45:
            return {"status": "INSUFFICIENT_HISTORY", "sample_size": 0, "model_version": ANALOG_MODEL_VERSION, "horizons": {}}
        dates = [item[0] for item in clean]; closes = [float(item[1]) for item in clean]
        ema20, ema50 = self._ema_series(closes, 20), self._ema_series(closes, 50)
        surface_by_date = {date.fromisoformat(item["date"]): item for item in surface.get("history", ()) if isinstance(item, Mapping) and isinstance(item.get("date"), str)}
        current_gex_method = gex.get("method_version")
        gex_by_date = {
            date.fromisoformat(item["date"]): item
            for item in gex.get("history", ())
            if isinstance(item, Mapping)
            and isinstance(item.get("date"), str)
            and (current_gex_method is None or item.get("method_version") == current_gex_method)
        }

        def vector(index: int, surface_row: Mapping[str, Any] | None, gex_row: Mapping[str, Any] | None) -> dict[str, float]:
            result: dict[str, float] = {}
            if index >= 5:
                result["return_5d"] = closes[index] / closes[index - 5] - 1
            if index >= 20:
                result["return_20d"] = closes[index] / closes[index - 20] - 1
                log_returns = [
                    log(closes[position] / closes[position - 1])
                    for position in range(index - 19, index + 1)
                    if closes[position] > 0 and closes[position - 1] > 0
                ]
                if len(log_returns) == 20:
                    result["realized_vol_20"] = pstdev(log_returns) * sqrt(252)
            if index >= 63:
                result["return_63d"] = closes[index] / closes[index - 63] - 1
                trailing_high = max(closes[index - 62:index + 1])
                if trailing_high > 0:
                    result["drawdown_63d"] = closes[index] / trailing_high - 1
            if ema20[index]:
                result["ema20_distance"] = closes[index] / float(ema20[index]) - 1
            if ema50[index]:
                result["ema50_distance"] = closes[index] / float(ema50[index]) - 1
            if surface_row and (iv := _number(surface_row.get("front_iv"))) is not None:
                result["front_iv"] = iv
            if gex_row and (flip := _number(gex_row.get("gamma_flip"))) not in (None, 0):
                result["flip_distance"] = closes[index] / float(flip) - 1
            return result

        current_index = len(closes) - 1
        current_vector_all = vector(current_index, surface, gex)
        derivative_features = {
            key for key in ("front_iv", "flip_distance") if key in current_vector_all
        }
        raw_candidates: list[tuple[int, dict[str, float]]] = []
        candidate_start = max(20, len(dates) - lookback_sessions)
        for index, day in enumerate(dates[:-20]):
            if index < candidate_start or index < 20:
                continue
            raw_candidates.append((index, vector(index, surface_by_date.get(day), gex_by_date.get(day))))

        derivative_candidate_indices = [
            index for index, row in raw_candidates
            if derivative_features and derivative_features.issubset(row)
        ]
        independent_derivative_indices: list[int] = []
        for index in derivative_candidate_indices:
            if not independent_derivative_indices or index - independent_derivative_indices[-1] >= selection_embargo_sessions:
                independent_derivative_indices.append(index)
        derivative_match_enabled = bool(derivative_features) and len(independent_derivative_indices) >= 5
        active_features = set(current_vector_all)
        if not derivative_match_enabled:
            active_features -= derivative_features
        current_vector = {
            key: value for key, value in current_vector_all.items() if key in active_features
        }
        candidates: list[tuple[int, dict[str, float]]] = []
        minimum_shared_features = min(5, len(current_vector))
        for index, raw_row in raw_candidates:
            row = {key: value for key, value in raw_row.items() if key in active_features}
            if len(set(row) & set(current_vector)) >= minimum_shared_features:
                candidates.append((index, row))
        if len(candidates) < 8:
            return {"status": "INSUFFICIENT_COMPARABLE_HISTORY", "sample_size": len(candidates), "model_version": ANALOG_MODEL_VERSION, "horizons": {}}
        distributions: dict[str, tuple[float, float]] = {}
        for key in current_vector:
            values = [row[key] for _index, row in candidates if key in row]
            if len(values) >= 5 and pstdev(values) > 0:
                distributions[key] = (fmean(values), pstdev(values))
        distance_minimum = min(minimum_shared_features, len(distributions))
        ranked: list[tuple[float, int]] = []
        for index, row in candidates:
            distances = [abs((row[key] - current_vector[key]) / distributions[key][1]) for key in distributions if key in row]
            if distance_minimum > 0 and len(distances) >= distance_minimum:
                coverage = len(distances) / len(distributions) if distributions else 0
                if coverage > 0:
                    ranked.append((fmean(distances) / coverage, index))
        selected: list[tuple[float, int]] = []
        for candidate in sorted(ranked):
            if all(abs(candidate[1] - chosen[1]) >= selection_embargo_sessions for chosen in selected):
                selected.append(candidate)
            if len(selected) >= limit:
                break
        if len(selected) < 5:
            return {
                "status": "INSUFFICIENT_INDEPENDENT_ANALOGS", "sample_size": len(selected),
                "model_version": ANALOG_MODEL_VERSION, "horizons": {},
                "lookback_sessions": lookback_sessions,
                "selection_embargo_sessions": selection_embargo_sessions,
            }
        analogs = []
        for distance, index in selected:
            path = [closes[index + session] / closes[index] - 1 for session in range(1, 21)]
            state = vector(index, surface_by_date.get(dates[index]), gex_by_date.get(dates[index]))
            peak = max(range(len(path)), key=path.__getitem__)
            trough = min(range(len(path)), key=path.__getitem__)
            first_up_5 = next((session for session, value in enumerate(path, 1) if value >= 0.05), None)
            first_down_5 = next((session for session, value in enumerate(path, 1) if value <= -0.05), None)
            if first_up_5 is None and first_down_5 is None:
                first_5_move = "NEITHER"
            elif first_down_5 is None or (first_up_5 is not None and first_up_5 < first_down_5):
                first_5_move = "UP"
            elif first_up_5 is None or first_down_5 < first_up_5:
                first_5_move = "DOWN"
            else:
                first_5_move = "SAME_SESSION"
            outcomes = {
                str(horizon): closes[index + horizon] / closes[index] - 1
                for horizon in (1, 5, 10, 20)
            }
            analogs.append({
                "date": dates[index].isoformat(), "distance": distance,
                "state_realized_vol_20": state.get("realized_vol_20"),
                "outcomes": outcomes, "forward_path": path,
                "max_favorable_excursion_20d": path[peak],
                "max_adverse_excursion_20d": path[trough],
                "peak_session": peak + 1, "trough_session": trough + 1,
                "first_up_5_session": first_up_5, "first_down_5_session": first_down_5,
                "first_5pct_move": first_5_move,
            })
        horizons: dict[str, Any] = {}
        for horizon in (1, 5, 10, 20):
            values = [item["outcomes"][str(horizon)] for item in analogs if str(horizon) in item["outcomes"]]
            horizons[str(horizon)] = {
                "sample_size": len(values), "up_rate": sum(item > 0 for item in values) / len(values) if values else None,
                "p10_return": _quantile(values, 0.10), "median_return": _quantile(values, 0.50),
                "p90_return": _quantile(values, 0.90),
            }

        path_distribution = []
        for session in range(1, 21):
            values = [item["forward_path"][session - 1] for item in analogs]
            path_distribution.append({
                "session": session, "sample_size": len(values),
                "up_rate": sum(value > 0 for value in values) / len(values),
                "p10_return": _quantile(values, 0.10),
                "median_return": _quantile(values, 0.50),
                "p90_return": _quantile(values, 0.90),
            })

        excursions_up = [item["max_favorable_excursion_20d"] for item in analogs]
        excursions_down = [item["max_adverse_excursion_20d"] for item in analogs]
        first_up = sum(item["first_5pct_move"] == "UP" for item in analogs)
        first_down = sum(item["first_5pct_move"] == "DOWN" for item in analogs)
        terminal = [item["outcomes"]["20"] for item in analogs]
        median_mfe = median(excursions_up)
        median_mae = median(excursions_down)
        payoff_asymmetry = median_mfe / abs(median_mae) if median_mae < 0 else None
        excursion_summary = {
            "sample_size": len(analogs),
            "median_max_favorable_excursion": median_mfe,
            "median_max_adverse_excursion": median_mae,
            "payoff_asymmetry": payoff_asymmetry,
            "hit_up_5_rate": sum(value >= 0.05 for value in excursions_up) / len(analogs),
            "hit_down_5_rate": sum(value <= -0.05 for value in excursions_down) / len(analogs),
            "hit_up_10_rate": sum(value >= 0.10 for value in excursions_up) / len(analogs),
            "hit_down_10_rate": sum(value <= -0.10 for value in excursions_down) / len(analogs),
            "up_first_5_rate": first_up / len(analogs),
            "down_first_5_rate": first_down / len(analogs),
            "neither_5_rate": sum(item["first_5pct_move"] == "NEITHER" for item in analogs) / len(analogs),
            "median_peak_session": median([item["peak_session"] for item in analogs]),
            "median_trough_session": median([item["trough_session"] for item in analogs]),
        }

        baseline_indices: list[int] = []
        for index, _row in candidates:
            if not baseline_indices or index - baseline_indices[-1] >= selection_embargo_sessions:
                baseline_indices.append(index)
        baseline_returns = [closes[index + 20] / closes[index] - 1 for index in baseline_indices]
        baseline_up_rate = (
            sum(value > 0 for value in baseline_returns) / len(baseline_returns)
            if baseline_returns else None
        )
        analog_up_rate = horizons["20"]["up_rate"]
        baseline_median = _quantile(baseline_returns, 0.50)
        analog_median = horizons["20"]["median_return"]
        baseline = {
            "status": "DESCRIPTIVE_NONOVERLAPPING_BASE_RATE",
            "sample_size": len(baseline_returns),
            "up_rate": baseline_up_rate,
            "p10_return": _quantile(baseline_returns, 0.10),
            "median_return": baseline_median,
            "p90_return": _quantile(baseline_returns, 0.90),
            "analog_up_rate_lift": (
                analog_up_rate - baseline_up_rate
                if analog_up_rate is not None and baseline_up_rate is not None else None
            ),
            "analog_median_return_lift": (
                analog_median - baseline_median
                if analog_median is not None and baseline_median is not None else None
            ),
            "selection_embargo_sessions": selection_embargo_sessions,
        }

        distance_values = [item["distance"] for item in analogs]
        historical_disposition = (
            "BULLISH" if median(terminal) >= 0.02 and sum(value > 0 for value in terminal) / len(terminal) >= 0.60
            else "BEARISH" if median(terminal) <= -0.02 and sum(value > 0 for value in terminal) / len(terminal) <= 0.40
            else "MIXED"
        )

        def disposition(values: Sequence[float]) -> str:
            up_rate = sum(value > 0 for value in values) / len(values)
            center = median(values)
            if center >= 0.02 and up_rate >= 0.60:
                return "BULLISH"
            if center <= -0.02 and up_rate <= 0.40:
                return "BEARISH"
            return "MIXED"

        # Leave-one-out sensitivity shows whether one matched state controls the
        # headline disposition. Distance weights are diagnostics only; the
        # displayed empirical quantiles remain unweighted.
        loo_sets = [terminal[:index] + terminal[index + 1:] for index in range(len(terminal))]
        loo_medians = [median(values) for values in loo_sets if values]
        loo_up_rates = [sum(value > 0 for value in values) / len(values) for values in loo_sets if values]
        loo_dispositions = [disposition(values) for values in loo_sets if values]
        raw_weights = [exp(-distance) for distance in distance_values]
        weight_total = sum(raw_weights)
        weights = [value / weight_total for value in raw_weights] if weight_total else []
        max_weight_share = max(weights) if weights else None
        effective_sample_size = 1 / sum(value * value for value in weights) if weights else None
        stable_disposition = all(value == historical_disposition for value in loo_dispositions)
        median_range = (
            max(loo_medians) - min(loo_medians) if loo_medians else None
        )
        stability_label = (
            "STABLE" if stable_disposition and median_range is not None and median_range <= 0.06
            else "SENSITIVE"
        )
        stability = {
            "status": stability_label,
            "leave_one_out_runs": len(loo_sets),
            "disposition_stable": stable_disposition,
            "leave_one_out_dispositions": sorted(set(loo_dispositions)),
            "leave_one_out_median_min": min(loo_medians) if loo_medians else None,
            "leave_one_out_median_max": max(loo_medians) if loo_medians else None,
            "leave_one_out_up_rate_min": min(loo_up_rates) if loo_up_rates else None,
            "leave_one_out_up_rate_max": max(loo_up_rates) if loo_up_rates else None,
            "maximum_distance_weight_share": max_weight_share,
            "effective_sample_size": effective_sample_size,
            "method": "Deterministic leave-one-out sensitivity plus exp(-distance) concentration diagnostics; weights do not alter reported empirical outcomes.",
        }
        return {
            "status": "DESCRIPTIVE_NOT_CALIBRATED", "sample_size": len(analogs), "model_version": ANALOG_MODEL_VERSION,
            "feature_count": len(distributions), "horizons": horizons, "analogs": analogs,
            "path_distribution": path_distribution, "excursion_summary": excursion_summary,
            "baseline_comparison": baseline, "historical_disposition": historical_disposition,
            "stability": stability,
            "match_quality": {
                "median_distance": median(distance_values),
                "best_distance": min(distance_values), "worst_distance": max(distance_values),
                "features": sorted(distributions), "candidate_count": len(candidates),
                "current_features": sorted(current_vector),
                "all_current_features": sorted(current_vector_all),
                "minimum_shared_features": minimum_shared_features,
                "minimum_distance_features": distance_minimum,
                "candidate_feature_coverage": {
                    key: sum(key in row for _index, row in candidates) / len(candidates)
                    for key in sorted(current_vector)
                },
                "derivative_match": {
                    "status": "ENABLED" if derivative_match_enabled else "STAGED_CONTEXT_ONLY",
                    "features": sorted(derivative_features),
                    "candidate_count": len(derivative_candidate_indices),
                    "independent_candidate_count": len(independent_derivative_indices),
                    "required_independent_candidates": 5,
                    "reason": (
                        "Derivative features passed the non-overlapping history gate."
                        if derivative_match_enabled
                        else "Derivative features remain outside analog distance until five non-overlapping historical states exist."
                    ),
                },
                "current_context_not_tested_in_match": [
                    "aggregate_option_flow", "confirmed_open_interest_change",
                    "native_greek_flow", "native_gex_shelves", "dark_pool_levels",
                    "short_crowding", "market_and_sector_tide", "news", "earnings",
                ],
            },
            "lookback_sessions": lookback_sessions, "selection_embargo_sessions": selection_embargo_sessions,
            "method": f"Nearest historical price states within {lookback_sessions} sessions using trend, realized volatility, drawdown, and only cutoff-safe fields. Missing-feature distances receive a coverage penalty. Derivative fields enter matching only after five non-overlapping historical states pass the history gate. Selected dates use a {selection_embargo_sessions}-session embargo. Leave-one-out sensitivity tests single-match dependence. Forward-path frequencies, excursions, and base-rate lifts are descriptive, not calibrated probabilities.",
        }

    @staticmethod
    def forecast_distribution(
        *, technical: Mapping[str, Any], analogs: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Build a transparent 20-session scenario path from analog outcomes.

        The center path blends empirical analog medians with a small, capped
        trend prior. The interval remains the empirical analog p10/p90 range.
        This is deliberately not called a probability forecast.
        """

        bars = technical.get("bars", ()) if isinstance(technical.get("bars"), list) else ()
        spot = _number(technical.get("latest_regular_close"))
        horizons = analogs.get("horizons", {}) if isinstance(analogs.get("horizons"), Mapping) else {}
        if spot is None or spot <= 0 or not bars or analogs.get("status") != "DESCRIPTIVE_NOT_CALIBRATED":
            return {
                "status": "UNAVAILABLE", "model_version": FORECAST_MODEL_VERSION,
                "reason": "At least five embargoed analogs and a valid latest close are required.",
                "calibrated": False, "path": [],
            }
        return_5d = _number(technical.get("return_5d")) or 0.0
        return_20d = _number(technical.get("return_20d")) or 0.0
        daily_trend = max(-0.015, min(0.015, 0.4 * return_5d / 5 + 0.6 * return_20d / 20))
        raw_path = analogs.get("path_distribution")
        empirical_path = (
            [item for item in raw_path if isinstance(item, Mapping)]
            if isinstance(raw_path, Sequence) and not isinstance(raw_path, (str, bytes)) else []
        )
        anchors: list[dict[str, float]] = [{"session": 0.0, "center": 0.0, "low": 0.0, "high": 0.0}]
        if len(empirical_path) >= 20:
            for session, row in enumerate(empirical_path[:20], 1):
                analog_center = _number(row.get("median_return"))
                low = _number(row.get("p10_return")); high = _number(row.get("p90_return"))
                sample = int(_number(row.get("sample_size")) or 0)
                if analog_center is None or low is None or high is None or sample < 5:
                    return {
                        "status": "UNAVAILABLE", "model_version": FORECAST_MODEL_VERSION,
                        "reason": f"The {session}-session empirical path distribution is incomplete.",
                        "calibrated": False, "path": [],
                    }
                center = 0.75 * analog_center + 0.25 * daily_trend * session
                anchors.append({
                    "session": float(session), "center": center,
                    "low": min(low, center), "high": max(high, center),
                })
        else:
            for horizon in (1, 5, 20):
                row = horizons.get(str(horizon), {}) if isinstance(horizons.get(str(horizon)), Mapping) else {}
                analog_center = _number(row.get("median_return"))
                low = _number(row.get("p10_return")); high = _number(row.get("p90_return"))
                sample = int(_number(row.get("sample_size")) or 0)
                if analog_center is None or low is None or high is None or sample < 5:
                    return {
                        "status": "UNAVAILABLE", "model_version": FORECAST_MODEL_VERSION,
                        "reason": f"The {horizon}-session analog distribution is incomplete.",
                        "calibrated": False, "path": [],
                    }
                center = 0.75 * analog_center + 0.25 * daily_trend * horizon
                anchors.append({
                    "session": float(horizon), "center": center,
                    "low": min(low, center), "high": max(high, center),
                })

        last_date = date.fromisoformat(str(bars[-1]["date"])[:10])
        future_dates: list[date] = []
        while len(future_dates) < 20:
            future_dates.append(next_nyse_session(future_dates[-1] if future_dates else last_date, include_current=False))

        def interpolate(session: int, field: str) -> float:
            right = next(item for item in anchors if item["session"] >= session)
            right_index = anchors.index(right)
            left = anchors[max(0, right_index - 1)]
            width = right["session"] - left["session"]
            weight = 0.0 if width == 0 else (session - left["session"]) / width
            return left[field] * (1 - weight) + right[field] * weight

        path = [{
            "session": session, "date": future_dates[session - 1].isoformat(),
            "center_return": interpolate(session, "center"),
            "low_return": interpolate(session, "low"),
            "high_return": interpolate(session, "high"),
            "center_price": spot * (1 + interpolate(session, "center")),
            "low_price": spot * (1 + interpolate(session, "low")),
            "high_price": spot * (1 + interpolate(session, "high")),
        } for session in range(1, 21)]
        terminal = anchors[-1]
        direction = "BULLISH" if terminal["center"] >= 0.01 else "BEARISH" if terminal["center"] <= -0.01 else "NEUTRAL"
        terminal_analogs = horizons.get("20", {}) if isinstance(horizons.get("20"), Mapping) else {}
        up_rate = _number(terminal_analogs.get("up_rate"))
        directional_frequency = (
            up_rate if direction == "BULLISH" else 1 - up_rate
            if direction == "BEARISH" and up_rate is not None else None
        )
        return {
            "status": "EXPERIMENTAL_UNCALIBRATED", "model_version": FORECAST_MODEL_VERSION,
            "calibrated": False, "direction": direction,
            "horizon_sessions": 20, "sample_size": int(analogs.get("sample_size", 0)),
            "center_return_20d": terminal["center"], "low_return_20d": terminal["low"],
            "high_return_20d": terminal["high"], "directional_analog_frequency": directional_frequency,
            "trend_prior_daily": daily_trend, "path": path,
            "calendar_note": "Future labels use the regular NYSE session calendar.",
            "method": "75% session-by-session embargoed-analog median plus 25% capped recent-trend prior; band is the empirical analog p10/p90 path.",
            "limitations": [
                "No registered walk-forward calibration artifact.",
                "Analog samples are small and ticker-specific.",
                "The interval is an empirical scenario range, not a confidence interval.",
            ],
        }

    @staticmethod
    def forecast_distribution_v4(
        *, technical: Mapping[str, Any], analogs: Mapping[str, Any],
        surface: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Build a volatility-scaled analog fan as a shadow forecast.

        The direction layer remains the transparent analog/trend blend. Each
        selected analog path is rescaled from its pre-state realized volatility
        to a current realized/implied variance blend. Scaling is clipped to
        limit domination by one low-volatility historical state. The output is
        experimental until walk-forward coverage and error tests pass.
        """

        bars = technical.get("bars", ()) if isinstance(technical.get("bars"), list) else ()
        spot = _number(technical.get("latest_regular_close"))
        current_rv = _number(technical.get("realized_vol_20"))
        current_iv = _number(surface.get("front_iv"))
        if spot is None or spot <= 0 or not bars or analogs.get("status") != "DESCRIPTIVE_NOT_CALIBRATED":
            return {
                "status": "UNAVAILABLE", "model_version": FORECAST_V4_MODEL_VERSION,
                "reason": "A valid close and at least five embargoed analog paths are required.",
                "calibrated": False, "path": [],
            }
        variance_inputs: list[tuple[str, float, float]] = []
        if current_rv is not None and current_rv > 0:
            variance_inputs.append(("realized_vol_20", current_rv, 0.65 if current_iv else 1.0))
        if current_iv is not None and current_iv > 0:
            variance_inputs.append(("front_iv", current_iv, 0.35 if current_rv else 1.0))
        weight_total = sum(item[2] for item in variance_inputs)
        if not variance_inputs or weight_total <= 0:
            return {
                "status": "UNAVAILABLE", "model_version": FORECAST_V4_MODEL_VERSION,
                "reason": "Current realized or implied volatility is required for volatility scaling.",
                "calibrated": False, "path": [],
            }
        target_volatility = sqrt(
            sum(weight * value * value for _name, value, weight in variance_inputs) / weight_total
        )
        raw_analogs = analogs.get("analogs")
        analog_rows = (
            [row for row in raw_analogs if isinstance(row, Mapping)]
            if isinstance(raw_analogs, Sequence) and not isinstance(raw_analogs, (str, bytes)) else []
        )
        scaled_paths: list[list[float]] = []
        scale_factors: list[float] = []
        clipped_count = 0
        for row in analog_rows:
            state_volatility = _number(row.get("state_realized_vol_20"))
            path = row.get("forward_path")
            if (
                state_volatility is None or state_volatility <= 0
                or not isinstance(path, Sequence) or isinstance(path, (str, bytes))
                or len(path) < 20
            ):
                continue
            raw_scale = target_volatility / state_volatility
            scale = max(0.60, min(1.75, raw_scale))
            clipped_count += int(scale != raw_scale)
            transformed: list[float] = []
            valid = True
            for value in path[:20]:
                observed = _number(value)
                if observed is None or observed <= -1:
                    valid = False
                    break
                transformed.append(exp(log(1 + observed) * scale) - 1)
            if valid and len(transformed) == 20:
                scaled_paths.append(transformed)
                scale_factors.append(scale)
        if len(scaled_paths) < 5:
            return {
                "status": "UNAVAILABLE", "model_version": FORECAST_V4_MODEL_VERSION,
                "reason": "Five analog paths with valid pre-state volatility are required.",
                "calibrated": False, "path": [],
                "eligible_analog_count": len(scaled_paths),
            }

        return_5d = _number(technical.get("return_5d")) or 0.0
        return_20d = _number(technical.get("return_20d")) or 0.0
        daily_trend = max(-0.015, min(0.015, 0.4 * return_5d / 5 + 0.6 * return_20d / 20))
        last_date = date.fromisoformat(str(bars[-1]["date"])[:10])
        future_dates: list[date] = []
        while len(future_dates) < 20:
            future_dates.append(next_nyse_session(future_dates[-1] if future_dates else last_date, include_current=False))

        path_rows: list[dict[str, Any]] = []
        for session in range(1, 21):
            values = [row[session - 1] for row in scaled_paths]
            p10 = _quantile(values, 0.10); p25 = _quantile(values, 0.25)
            p50 = _quantile(values, 0.50); p75 = _quantile(values, 0.75)
            p90 = _quantile(values, 0.90)
            if None in (p10, p25, p50, p75, p90):
                return {
                    "status": "UNAVAILABLE", "model_version": FORECAST_V4_MODEL_VERSION,
                    "reason": f"The {session}-session scaled path distribution is incomplete.",
                    "calibrated": False, "path": [],
                }
            assert p10 is not None and p25 is not None and p50 is not None and p75 is not None and p90 is not None
            center = max(p10, min(p90, 0.75 * p50 + 0.25 * daily_trend * session))
            path_rows.append({
                "session": session, "date": future_dates[session - 1].isoformat(),
                "center_return": center,
                "p10_return": p10, "p25_return": p25, "p50_return": p50,
                "p75_return": p75, "p90_return": p90,
                "center_price": spot * (1 + center),
                "p10_price": spot * (1 + p10), "p25_price": spot * (1 + p25),
                "p50_price": spot * (1 + p50), "p75_price": spot * (1 + p75),
                "p90_price": spot * (1 + p90),
                # Compatibility aliases let existing range evaluators score v4.
                "low_return": p10, "high_return": p90,
                "low_price": spot * (1 + p10), "high_price": spot * (1 + p90),
            })
        terminal = path_rows[-1]
        one_week = path_rows[4]
        center_return = float(terminal["center_return"])
        direction = "BULLISH" if center_return >= 0.01 else "BEARISH" if center_return <= -0.01 else "NEUTRAL"
        terminal_analogs = analogs.get("horizons", {}).get("20", {}) if isinstance(analogs.get("horizons"), Mapping) else {}
        up_rate = _number(terminal_analogs.get("up_rate")) if isinstance(terminal_analogs, Mapping) else None
        directional_frequency = (
            up_rate if direction == "BULLISH" else 1 - up_rate
            if direction == "BEARISH" and up_rate is not None else None
        )
        return {
            "status": "SHADOW_EXPERIMENTAL_UNCALIBRATED",
            "model_version": FORECAST_V4_MODEL_VERSION, "calibrated": False,
            "promotion_eligible": False, "direction": direction,
            "horizon_sessions": 20, "sample_size": len(scaled_paths),
            "center_return_5d": one_week["center_return"],
            "p10_return_5d": one_week["p10_return"], "p25_return_5d": one_week["p25_return"],
            "p50_return_5d": one_week["p50_return"], "p75_return_5d": one_week["p75_return"],
            "p90_return_5d": one_week["p90_return"],
            "center_return_20d": center_return,
            "p10_return_20d": terminal["p10_return"], "p25_return_20d": terminal["p25_return"],
            "p50_return_20d": terminal["p50_return"], "p75_return_20d": terminal["p75_return"],
            "p90_return_20d": terminal["p90_return"],
            "low_return_20d": terminal["p10_return"], "high_return_20d": terminal["p90_return"],
            "directional_analog_frequency": directional_frequency,
            "trend_prior_daily": daily_trend, "path": path_rows,
            "volatility_scaling": {
                "target_annualized_volatility": target_volatility,
                "realized_volatility_20": current_rv, "front_implied_volatility": current_iv,
                "variance_blend": {name: weight / weight_total for name, _value, weight in variance_inputs},
                "minimum_scale": min(scale_factors), "median_scale": median(scale_factors),
                "maximum_scale": max(scale_factors), "clip_floor": 0.60, "clip_ceiling": 1.75,
                "clipped_analog_count": clipped_count,
            },
            "calendar_note": "Future labels use the regular NYSE session calendar.",
            "method": "Volatility-scaled analog paths using a 65% realized-variance and 35% front-implied-variance target when both are available; scale factors are clipped to 0.60–1.75. The center remains 75% scaled analog median plus 25% capped recent trend. Fan bands are empirical scaled-path quantiles.",
            "limitations": [
                "Shadow model with no resolved 20-session prospective evaluation.",
                "Fixed variance-blend weights and scale caps are not calibrated.",
                "Implied volatility prices risk-neutral movement and is not a directional signal.",
                "The fan is a scenario distribution, not a confidence interval or probability forecast.",
            ],
        }

    @staticmethod
    def edge_dimensions(
        *, technical: Mapping[str, Any], surface: Mapping[str, Any], flow: Mapping[str, Any],
        gex: Mapping[str, Any], earnings: Mapping[str, Any], news: Mapping[str, Any],
        analogs: Mapping[str, Any], as_of_date: date,
    ) -> dict[str, Any]:
        directional = 50.0
        price = _number(technical.get("latest_regular_close"))
        for key, weight in (("ema_20", 9), ("ema_50", 9), ("ema_200", 7)):
            level = _number(technical.get(key))
            if price is not None and level is not None:
                directional += weight if price > level else -weight
        for key, scale in (("return_5d", 0.06), ("return_20d", 0.12), ("return_63d", 0.25)):
            value = _number(technical.get(key))
            if value is not None:
                directional += max(-8.0, min(8.0, value / scale * 8))
        analog20 = analogs.get("horizons", {}).get("20", {}) if isinstance(analogs.get("horizons"), Mapping) else {}
        if (analog_return := _number(analog20.get("median_return"))) is not None:
            directional += max(-8.0, min(8.0, analog_return / 0.12 * 8))

        volatility = 50.0
        if (rank := _number(surface.get("iv_percentile"))) is not None:
            volatility = (1 - rank) * 100
        if (gap := _number(surface.get("iv_rv_gap"))) is not None:
            volatility += max(-20.0, min(20.0, -gap / 0.20 * 20))

        positioning = 50.0
        if gex.get("spot_regime") == "ABOVE_FLIP":
            positioning += 10
        elif gex.get("spot_regime") == "BELOW_FLIP":
            positioning -= 10
        if (share := _number(flow.get("directional_share"))) is not None:
            positioning += max(-20.0, min(20.0, share * 20 * (_number(flow.get("quality_multiplier")) or 0.25)))

        tradeability = 0.0
        if (spread := _number(surface.get("median_spread_pct"))) is not None:
            tradeability += max(0.0, 55 * (1 - min(1.0, spread / 0.20)))
        if (_number(surface.get("median_open_interest")) or 0) >= 100:
            tradeability += 25
        if (_number(surface.get("median_volume")) or 0) >= 10:
            tradeability += 20

        catalyst = 20.0
        next_event = earnings.get("next_event")
        if isinstance(next_event, Mapping) and isinstance(next_event.get("report_date"), str):
            try:
                days = (date.fromisoformat(next_event["report_date"]) - as_of_date).days
                catalyst = 95.0 if 0 <= days <= 7 else 70.0 if days <= 30 else 35.0
            except ValueError:
                pass
        catalyst = min(100.0, catalyst + min(20, int(news.get("major_count", 0)) * 5))

        history_counts = [int(surface.get("history_sessions", 0)), int(gex.get("history_sessions", 0)), int(analogs.get("sample_size", 0))]
        evidence_quality = 20 + min(35, history_counts[0] / 2) + min(25, history_counts[1] / 2) + min(20, history_counts[2])
        return {
            "directional_edge": round(_clamp(directional), 1),
            "long_volatility_attractiveness": round(_clamp(volatility), 1),
            "positioning_context": round(_clamp(positioning), 1),
            "tradeability": round(_clamp(tradeability), 1),
            "catalyst_risk": round(_clamp(catalyst), 1),
            "evidence_quality": round(_clamp(evidence_quality), 1),
            "calibrated_probability_available": False,
            "interpretation": "Separate research dimensions; no composite value is a probability or trade instruction.",
        }

    def analyze(
        self, ticker: str, cutoff_at: datetime, *, technical: Mapping[str, Any],
    ) -> dict[str, Any]:
        spot = _number(technical.get("latest_regular_close"))
        bars = technical.get("bars", ()) if isinstance(technical.get("bars"), list) else ()
        realized = _number(technical.get("realized_vol_20"))
        volumes = []
        # The dashboard bars omit volume. Pull normalized OHLC payload values for ADV context.
        ohlc = self.snapshots(ticker, "ohlc", cutoff_at)
        if ohlc:
            for row in _rows(ohlc[0].payload)[-20:]:
                close, volume = _number(row.get("close")), _number(row.get("volume"))
                if close is not None and volume is not None and close > 0 and volume >= 0:
                    volumes.append(close * volume)
        adv = fmean(volumes) if volumes else None
        surface = self.option_surface(ticker, cutoff_at, spot=spot, bars=bars, realized_vol_20=realized)
        oi = self.oi_structure(ticker, cutoff_at, spot=spot)
        flow = self.flow_conviction(ticker, cutoff_at)
        gex = self.gex_topology(ticker, cutoff_at, spot=spot)
        dark = self.dark_pool_structure(ticker, cutoff_at, spot=spot, average_daily_dollar_volume=adv)
        earnings = self.earnings_priors(ticker, cutoff_at)
        news = self.news_signal(ticker, cutoff_at)
        analogs = self.historical_analogs(bars=bars, surface=surface, gex=gex)
        forecast = self.forecast_distribution(technical=technical, analogs=analogs)
        forecast_v4 = self.forecast_distribution_v4(
            technical=technical, analogs=analogs, surface=surface,
        )
        challengers = shadow_challengers(bars=bars)
        dimensions = self.edge_dimensions(
            technical=technical, surface=surface, flow=flow, gex=gex,
            earnings=earnings, news=news, analogs=analogs,
            as_of_date=utc_timestamp(cutoff_at, field_name="cutoff_at").date(),
        )
        source_ids = sorted({
            int(item) for section in (surface, oi, flow, gex, dark, earnings, news)
            for item in section.get("source_snapshot_ids", ()) if isinstance(item, int)
        })
        return {
            "feature_version": EDGE_FEATURE_VERSION, "ticker": ticker, "cutoff_at": timestamp_text(cutoff_at),
            "dimensions": dimensions, "option_surface": surface, "open_interest": oi,
            "flow_conviction": flow, "gex_topology": gex, "dark_pool": dark,
            "earnings_priors": earnings, "news_signal": news, "historical_analogs": analogs,
            "forecast": forecast, "forecast_v4": forecast_v4,
            "shadow_challengers": challengers,
            "source_snapshot_ids": source_ids,
            "calibration": {
                "status": "NOT_CALIBRATED", "ready": False,
                "reason": "No registered walk-forward calibration artifact; analog frequencies remain descriptive.",
                "required": ["walk-forward evaluation", "overlap embargo", "friction-adjusted outcomes", "baseline comparison"],
            },
        }


def option_mechanics(
    contract: Mapping[str, Any], *, spot: float | None, cutoff_at: datetime,
    risk_free_rate: float = 0.04,
) -> dict[str, Any]:
    """Return transparent Black-Scholes reference mechanics for one long option.

    This uses the stored IV and ask as explicit assumptions. It is a scenario
    calculator, not a physical-probability forecast or executable quote.
    """

    option_type = str(contract.get("option_type", contract.get("type", ""))).lower()
    strike = _number(contract.get("strike")); premium = _number(contract.get("ask"))
    sigma = _number(contract.get("implied_volatility")); expiry_value = contract.get("expiry")
    if option_type not in {"call", "put"} or spot is None or spot <= 0 or strike is None or strike <= 0 or premium is None or premium <= 0 or sigma is None or sigma <= 0 or not isinstance(expiry_value, str):
        return {"status": "UNAVAILABLE", "reason": "Spot, strike, ask, IV, option type, and expiry are required."}
    try:
        expiry_date = date.fromisoformat(expiry_value[:10])
    except ValueError:
        return {"status": "UNAVAILABLE", "reason": "Expiry is invalid."}
    dte = max(0, (expiry_date - utc_timestamp(cutoff_at, field_name="cutoff_at").date()).days)
    breakeven = strike + premium if option_type == "call" else strike - premium

    def value(underlying: float, remaining_days: int) -> float:
        intrinsic = max(0.0, underlying - strike) if option_type == "call" else max(0.0, strike - underlying)
        if remaining_days <= 0:
            return intrinsic
        t = remaining_days / 365
        d1 = (log(underlying / strike) + (risk_free_rate + sigma * sigma / 2) * t) / (sigma * sqrt(t))
        d2 = d1 - sigma * sqrt(t)
        if option_type == "call":
            return underlying * _normal_cdf(d1) - strike * exp(-risk_free_rate * t) * _normal_cdf(d2)
        return strike * exp(-risk_free_rate * t) * _normal_cdf(-d2) - underlying * _normal_cdf(-d1)

    terminal_t = max(dte, 1) / 365
    threshold = max(0.01, breakeven)
    z = (log(spot / threshold) + (risk_free_rate - sigma * sigma / 2) * terminal_t) / (sigma * sqrt(terminal_t))
    risk_neutral_be = _normal_cdf(z) if option_type == "call" else _normal_cdf(-z)
    price_moves = (-0.20, -0.10, -0.05, 0.0, 0.05, 0.10, 0.20)
    elapsed_fractions = (0.0, 0.25, 0.50, 0.75, 1.0)
    matrix = []
    for move in price_moves:
        row = {"underlying_change_pct": move, "underlying_price": spot * (1 + move), "points": []}
        for elapsed in elapsed_fractions:
            remaining = max(0, round(dte * (1 - elapsed)))
            modeled = value(row["underlying_price"], remaining)
            row["points"].append({"elapsed_fraction": elapsed, "remaining_days": remaining, "modeled_value": modeled, "return_on_ask": modeled / premium - 1})
        matrix.append(row)
    return {
        "status": "MODEL_REFERENCE_ONLY", "method": "Black-Scholes with constant stored IV and 4% rate",
        "premium_basis": premium, "breakeven": breakeven,
        "breakeven_move_pct": breakeven / spot - 1,
        "risk_neutral_breakeven_probability": risk_neutral_be,
        "physical_probability_available": False, "dte": dte, "matrix": matrix,
    }
