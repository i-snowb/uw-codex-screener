"""Shadow-only option fit and scenario analysis.

This module ranks stored reference contracts against forecast scenarios. It does
not estimate physical probabilities, expected return, or executable P&L.
"""

from __future__ import annotations

from datetime import datetime
import math
from typing import Any, Mapping, Sequence

from .edge import option_mechanics


OPTION_RESEARCH_VERSION = "option-scenario-selector-v1"


def _number(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _scenario_point(mechanics: Mapping[str, Any], move: float, elapsed: float) -> dict[str, Any] | None:
    rows = mechanics.get("matrix")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return None
    candidates = [row for row in rows if isinstance(row, Mapping) and _number(row.get("underlying_change_pct")) is not None]
    if not candidates:
        return None
    row = min(candidates, key=lambda item: abs(float(item["underlying_change_pct"]) - move))
    points = row.get("points")
    if not isinstance(points, Sequence) or isinstance(points, (str, bytes)):
        return None
    valid = [item for item in points if isinstance(item, Mapping) and _number(item.get("elapsed_fraction")) is not None]
    if not valid:
        return None
    point = min(valid, key=lambda item: abs(float(item["elapsed_fraction"]) - elapsed))
    return {
        "requested_underlying_return": move,
        "modeled_underlying_return": _number(row.get("underlying_change_pct")),
        "modeled_underlying_price": _number(row.get("underlying_price")),
        "elapsed_fraction": _number(point.get("elapsed_fraction")),
        "remaining_days": _number(point.get("remaining_days")),
        "modeled_value": _number(point.get("modeled_value")),
        "modeled_return_on_stored_ask": _number(point.get("return_on_ask")),
    }


def shadow_option_research(
    *,
    direction: str,
    spot: float | None,
    cutoff_at: datetime,
    contracts: Sequence[Mapping[str, Any]],
    forecast_v4: Mapping[str, Any],
) -> dict[str, Any]:
    """Return scenario-fit references without changing the published thesis."""

    wanted = "call" if direction == "BULLISH" else "put" if direction == "BEARISH" else None
    scenario_moves = {
        "p10": _number(forecast_v4.get("p10_return_20d")),
        "center": _number(forecast_v4.get("center_return_20d")),
        "p90": _number(forecast_v4.get("p90_return_20d")),
    }
    if wanted is None or spot is None or spot <= 0:
        return {
            "status": "UNAVAILABLE_NO_DIRECTION",
            "version": OPTION_RESEARCH_VERSION,
            "promotion_eligible": False,
            "rows": [],
        }
    rows: list[dict[str, Any]] = []
    for contract in contracts:
        if str(contract.get("option_type", "")).lower() != wanted:
            continue
        mechanics = contract.get("mechanics")
        if not isinstance(mechanics, Mapping) or mechanics.get("status") != "MODEL_REFERENCE_ONLY":
            mechanics = option_mechanics(contract, spot=spot, cutoff_at=cutoff_at)
        spread = _number(contract.get("spread_pct_of_mid"))
        open_interest = _number(contract.get("open_interest")) or 0.0
        delta = abs(_number(contract.get("delta")) or 0.0)
        dte = _number(contract.get("dte")) or 0.0
        quote_fresh = contract.get("quote_fresh") is True
        liquidity_score = (
            0.40 * max(0.0, 1.0 - (spread if spread is not None else 1.0) / 0.25)
            + 0.25 * max(0.0, 1.0 - abs(delta - 0.45) / 0.35)
            + 0.20 * max(0.0, 1.0 - abs(dte - 75.0) / 150.0)
            + 0.15 * min(1.0, open_interest / 1000.0)
        )
        scenarios = {
            name: _scenario_point(mechanics, move, min(1.0, 20.0 / max(dte, 1.0)))
            for name, move in scenario_moves.items() if move is not None
        }
        rows.append({
            "contract": contract.get("contract"),
            "type": wanted.upper(),
            "expiry": contract.get("expiry"),
            "dte": contract.get("dte"),
            "strike": contract.get("strike"),
            "stored_bid": contract.get("bid"),
            "stored_ask": contract.get("ask"),
            "spread_pct": spread,
            "open_interest": int(open_interest),
            "delta": contract.get("delta"),
            "quote_fresh": quote_fresh,
            "research_fit_score": round(100.0 * liquidity_score, 1),
            "fit_score_is_probability": False,
            "scenario_basis": "Black-Scholes at stored IV; nearest displayed price grid point",
            "scenarios": scenarios,
            "status": "NOT_ELIGIBLE",
        })
    rows.sort(key=lambda row: (-float(row["research_fit_score"]), str(row.get("contract"))))
    complete_scenarios = all(value is not None for value in scenario_moves.values())
    return {
        "status": "SHADOW_ONLY" if rows and complete_scenarios else "PARTIAL_SHADOW_ONLY",
        "version": OPTION_RESEARCH_VERSION,
        "promotion_eligible": False,
        "direction": direction,
        "rows": rows,
        "selected_reference": rows[0].get("contract") if rows else None,
        "limitations": [
            "Research fit is a liquidity and contract-shape score, not chance of profit.",
            "Scenario returns use stored ask, constant stored IV, and a fixed rate; they are not expected returns.",
            "No row is executable and no row can change the active thesis.",
        ],
    }
