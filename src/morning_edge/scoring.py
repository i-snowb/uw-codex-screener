"""Versioned, conservative morning decision scoring.

This module is deliberately a decision-support layer, not a price predictor.
It keeps setup quality, directional probability, and action gating separate so a
clean technical setup cannot bypass missing, stale, or contradictory evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Mapping


SCORING_VERSION = "1.2.0"
MIN_ACCEPTABLE_QUALITY = 0.70
# Market time and local availability remain separate. Prior-session evidence can
# inform the morning watchlist; executable price and spread data cannot be
# inherited from the close.
FIELD_MAX_AGE = {
    "price": timedelta(minutes=5),
    "bid_ask_spread_pct": timedelta(minutes=5),
    "trend": timedelta(days=5),
    "flow": timedelta(days=5),
    "oi_change": timedelta(days=5),
    "gex": timedelta(days=5),
    "iv_rank": timedelta(days=5),
}
PREOPEN_REFERENCE_QUOTE_MAX_AGE = timedelta(days=5)


class Provenance(str, Enum):
    """How a value entered the system; never silently collapse these classes."""

    OBSERVED = "observed"
    INFERRED = "inferred"
    MODELED = "modeled"


class Action(str, Enum):
    BUY = "BUY"
    WATCH = "WATCH"
    NO_ACTION = "NO_ACTION"
    TRIM = "TRIM"
    EXIT = "EXIT"


@dataclass(frozen=True)
class Evidence:
    """One time-stamped input with explicit source and reliability metadata."""

    value: float
    as_of: datetime
    provenance: Provenance
    source: str
    quality: float = 1.0
    available_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise ValueError("Evidence.as_of must be timezone-aware")
        available_at = self.available_at or self.as_of
        if available_at.tzinfo is None:
            raise ValueError("Evidence.available_at must be timezone-aware")
        if self.as_of > available_at:
            raise ValueError("Evidence.as_of cannot be later than available_at")
        object.__setattr__(self, "available_at", available_at)
        if not 0.0 <= self.quality <= 1.0:
            raise ValueError("Evidence.quality must be between 0 and 1")

    def age(self, now: datetime) -> timedelta:
        """Age of the underlying market observation."""

        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return now - self.as_of

    def availability_age(self, now: datetime) -> timedelta:
        """Age since the value became available to the pipeline."""

        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        assert self.available_at is not None
        return now - self.available_at


@dataclass(frozen=True)
class PortfolioPosition:
    """Minimal current-trade data needed for risk-first management."""

    contracts: int
    unrealized_return_pct: float
    days_to_expiry: int
    option_side: str = "call"

    def __post_init__(self) -> None:
        if self.contracts <= 0:
            raise ValueError("contracts must be positive")
        if self.days_to_expiry < 0:
            raise ValueError("days_to_expiry cannot be negative")
        if self.option_side not in {"call", "put"}:
            raise ValueError("option_side must be 'call' or 'put'")


@dataclass(frozen=True)
class MorningInputs:
    """Normalized inputs, all signed indicators use -1 (bearish) to +1 (bullish).

    `trend`, `flow`, `oi_change`, and `gex` are evidence components.  `catalyst`
    measures directionally aligned catalyst support, while `event_risk` measures
    binary-event risk independently.  This avoids treating a catalyst as a fact
    about direction.
    """

    ticker: str
    captured_at: datetime
    price: Evidence | None = None
    trend: Evidence | None = None
    flow: Evidence | None = None
    oi_change: Evidence | None = None
    gex: Evidence | None = None
    iv_rank: Evidence | None = None
    bid_ask_spread_pct: Evidence | None = None
    catalyst: Evidence | None = None
    event_risk: Evidence | None = None
    position: PortfolioPosition | None = None
    execution_ready: bool = False
    calibration_ready: bool = False
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        if not self.ticker or self.ticker != self.ticker.upper():
            raise ValueError("ticker must be non-empty uppercase")


@dataclass(frozen=True)
class DataGate:
    passed: bool
    reasons: tuple[str, ...]
    completeness: float
    reliability: float


@dataclass(frozen=True)
class ScoreResult:
    ticker: str
    scoring_version: str
    as_of: datetime
    action: Action
    setup_score: int
    directional_probability: float
    confidence: int
    execution_ready: bool
    calibration_ready: bool
    data_gate: DataGate
    reasons: tuple[str, ...]
    component_scores: Mapping[str, float]
    provenance_summary: Mapping[Provenance, int]


_REQUIRED = ("price", "trend", "flow", "oi_change", "iv_rank", "bid_ask_spread_pct")
_ALL_FIELDS = (
    "price", "trend", "flow", "oi_change", "gex", "iv_rank",
    "bid_ask_spread_pct", "catalyst", "event_risk",
)

# These are normalized signal values, not raw vendor fields. Price and spread
# remain direct observations. Trend/IV/GEX are calculations; flow direction is
# inferred from execution context; OI confirmation can be observed or inferred
# depending on whether the normalizer stores the raw delta or its signed read.
_ALLOWED_REQUIRED_PROVENANCE = {
    "price": frozenset({Provenance.OBSERVED}),
    "trend": frozenset({Provenance.INFERRED, Provenance.MODELED}),
    "flow": frozenset({Provenance.INFERRED}),
    "oi_change": frozenset({Provenance.OBSERVED, Provenance.INFERRED}),
    "iv_rank": frozenset({Provenance.MODELED}),
    "bid_ask_spread_pct": frozenset({Provenance.OBSERVED}),
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _signal(value: Evidence | None) -> float:
    return _clamp(value.value, -1.0, 1.0) if value else 0.0


def _valid_indicator(name: str, evidence: Evidence | None) -> str | None:
    if evidence is None:
        return f"Missing required {name.replace('_', ' ')} data"
    allowed = _ALLOWED_REQUIRED_PROVENANCE[name]
    if evidence.provenance not in allowed:
        expected = "/".join(sorted(item.value for item in allowed))
        return (
            f"{name.replace('_', ' ').title()} provenance is {evidence.provenance.value}; "
            f"expected {expected}"
        )
    return None


def evaluate_data_gate(inputs: MorningInputs) -> DataGate:
    """Evaluate analysis freshness, with stricter quotes when execution is live.

    At 06:55, a prior-session close/chain can support a research WATCH when it
    remains within the reference-data policy.  Once ``execution_ready`` is
    true, price and bid/ask spread immediately tighten to five minutes.
    """

    reasons: list[str] = []
    available = [getattr(inputs, name) for name in _ALL_FIELDS if getattr(inputs, name) is not None]
    completeness = len(available) / len(_ALL_FIELDS)
    required_reliability: list[float] = []

    for name in _REQUIRED:
        item = getattr(inputs, name)
        issue = _valid_indicator(name, item)
        if issue:
            reasons.append(issue)
            required_reliability.append(0.0)
            continue
        assert item is not None
        item_age = item.age(inputs.captured_at)
        availability_age = item.availability_age(inputs.captured_at)
        max_age: timedelta | None = None
        if item_age < timedelta(0) or availability_age < timedelta(0):
            reasons.append(f"{name.replace('_', ' ').title()} is future-dated relative to the snapshot cutoff")
        else:
            max_age = FIELD_MAX_AGE[name]
            if not inputs.execution_ready and name in {"price", "bid_ask_spread_pct"}:
                max_age = PREOPEN_REFERENCE_QUOTE_MAX_AGE
            if item_age <= max_age:
                max_age = None
        if item_age >= timedelta(0) and max_age is not None:
            reasons.append(
                f"{name.replace('_', ' ').title()} is stale "
                f"({int(item_age.total_seconds() // 60)}m old; policy {int(max_age.total_seconds() // 60)}m)"
            )
        policy_age = FIELD_MAX_AGE[name]
        if not inputs.execution_ready and name in {"price", "bid_ask_spread_pct"}:
            policy_age = PREOPEN_REFERENCE_QUOTE_MAX_AGE
        age_fraction = _clamp(item_age.total_seconds() / policy_age.total_seconds(), 0.0, 1.0)
        freshness_weight = 1.0 - 0.5 * age_fraction
        required_reliability.append(item.quality * freshness_weight)
        if item.quality < MIN_ACCEPTABLE_QUALITY:
            reasons.append(f"{name.replace('_', ' ').title()} quality is below {MIN_ACCEPTABLE_QUALITY:.0%}")

    spread = inputs.bid_ask_spread_pct
    if spread and spread.value > 8.0:
        reasons.append(f"Option spread is too wide ({spread.value:.1f}%)")
    if inputs.trend and inputs.flow and abs(inputs.trend.value) >= 0.55 and abs(inputs.flow.value) >= 0.55:
        if inputs.trend.value * inputs.flow.value < 0:
            reasons.append("Trend and options flow conflict materially")
    if inputs.flow and inputs.oi_change and abs(inputs.flow.value) >= 0.65 and abs(inputs.oi_change.value) >= 0.65:
        if inputs.flow.value * inputs.oi_change.value < 0:
            reasons.append("Options flow and open-interest change conflict materially")

    reliability = sum(required_reliability) / len(_REQUIRED)
    return DataGate(not reasons, tuple(reasons), completeness, reliability)


def _provenance_weight(evidence: Evidence | None) -> float:
    if evidence is None:
        return 0.0
    return {
        Provenance.OBSERVED: 1.0,
        Provenance.INFERRED: 0.65,
        Provenance.MODELED: 0.40,
    }[evidence.provenance] * evidence.quality


def _signed_component(evidence: Evidence | None) -> float:
    return _signal(evidence) * _provenance_weight(evidence)


def score(inputs: MorningInputs) -> ScoreResult:
    """Score a snapshot and emit an explainable gated action.

    Formula changes require a version increment.  Values are intentionally kept
    simple enough to recompute from a saved JSON snapshot.
    """

    gate = evaluate_data_gate(inputs)
    trend = _signed_component(inputs.trend)
    flow = _signed_component(inputs.flow)
    oi = _signed_component(inputs.oi_change)
    gex = _signed_component(inputs.gex)
    catalyst = _signed_component(inputs.catalyst)
    event_risk = abs(_signed_component(inputs.event_risk))
    iv = inputs.iv_rank.value if inputs.iv_rank else 100.0
    spread = inputs.bid_ask_spread_pct.value if inputs.bid_ask_spread_pct else 100.0

    # Setup measures tradeability, not a claim about the next price move.
    trend_quality = (trend + 1.0) / 2.0
    flow_quality = (abs(flow) + 1.0) / 2.0
    oi_quality = (abs(oi) + 1.0) / 2.0
    gex_quality = (gex + 1.0) / 2.0
    liquidity = _clamp(1.0 - (spread / 10.0), 0.0, 1.0)
    iv_suitability = _clamp(1.0 - max(0.0, iv - 70.0) / 30.0, 0.0, 1.0)
    catalyst_quality = _clamp((catalyst + 1.0) / 2.0 - event_risk * 0.30, 0.0, 1.0)
    setup_raw = (
        0.25 * trend_quality + 0.20 * flow_quality + 0.10 * oi_quality
        + 0.10 * gex_quality + 0.20 * liquidity + 0.05 * iv_suitability
        + 0.10 * catalyst_quality
    )
    setup_score = round(100 * setup_raw)

    # Direction is a separately-calibrated estimate.  It deliberately does not
    # turn IV rank or spread into a directional claim.
    directional_edge = 0.34 * trend + 0.28 * flow + 0.12 * oi + 0.12 * gex + 0.14 * catalyst
    reliability = gate.reliability * (0.75 + 0.25 * gate.completeness)
    probability = _clamp(0.50 + 0.40 * directional_edge * reliability, 0.05, 0.95)
    confidence = round(100 * _clamp(0.50 * gate.reliability + 0.25 * gate.completeness + 0.25 * abs(probability - 0.5) * 2, 0.0, 1.0))

    reasons: list[str] = []
    if not gate.passed:
        reasons.extend(gate.reasons)
    else:
        reasons.append(f"Setup {setup_score}/100; directional estimate {probability:.0%}")
        if abs(trend) >= 0.45:
            reasons.append("Trend evidence is materially directional")
        if abs(flow) >= 0.45:
            reasons.append("Options flow corroborates the directional case")
        if inputs.event_risk and inputs.event_risk.value >= 0.60:
            reasons.append("Elevated catalyst/event risk limits position sizing")

    action = _decide_action(
        inputs.position,
        inputs.execution_ready,
        inputs.calibration_ready,
        gate,
        setup_score,
        probability,
        confidence,
        reasons,
    )
    if action is Action.NO_ACTION and gate.passed and setup_score < 50:
        reasons.append("Setup quality is below the watch threshold")
    elif action is Action.WATCH:
        reasons.append("Wait for confirmation; conditions do not clear the entry threshold")

    summary = {kind: 0 for kind in Provenance}
    for name in _ALL_FIELDS:
        item = getattr(inputs, name)
        if item is not None:
            summary[item.provenance] += 1

    return ScoreResult(
        ticker=inputs.ticker,
        scoring_version=SCORING_VERSION,
        as_of=inputs.captured_at,
        action=action,
        setup_score=setup_score,
        directional_probability=probability,
        confidence=confidence,
        execution_ready=inputs.execution_ready,
        calibration_ready=inputs.calibration_ready,
        data_gate=gate,
        reasons=tuple(reasons),
        component_scores={
            "trend": trend, "flow": flow, "open_interest": oi, "gex": gex,
            "catalyst": catalyst, "event_risk": event_risk, "liquidity": liquidity,
            "iv_suitability": iv_suitability,
        },
        provenance_summary=summary,
    )


def _decide_action(
    position: PortfolioPosition | None,
    execution_ready: bool,
    calibration_ready: bool,
    gate: DataGate,
    setup_score: int,
    probability: float,
    confidence: int,
    reasons: list[str],
) -> Action:
    if position:
        adverse_probability = probability if position.option_side == "put" else 1.0 - probability
        if position.days_to_expiry <= 5:
            reasons.append("Position has five or fewer days to expiry")
            return Action.EXIT
        if position.unrealized_return_pct <= -50:
            reasons.append("Position loss reached the -50% risk stop")
            return Action.EXIT
        if adverse_probability >= 0.65 and setup_score < 45:
            reasons.append("Evidence is materially adverse to the open position")
            return Action.EXIT
        if position.unrealized_return_pct >= 75:
            reasons.append("Position return reached the +75% trim rule")
            return Action.TRIM
        if position.days_to_expiry <= 14 and position.unrealized_return_pct >= 30:
            reasons.append("Near-expiry profit meets the trim rule")
            return Action.TRIM
        return Action.WATCH

    if not gate.passed:
        return Action.NO_ACTION
    if not execution_ready:
        reasons.append("Execution is not ready; pre-open or non-executable data cannot authorize a BUY")
        return Action.WATCH
    if not calibration_ready:
        reasons.append("Calibration is not ready; shadow-mode estimates cannot authorize a BUY")
        return Action.WATCH
    if setup_score >= 68 and probability >= 0.60 and confidence >= 70:
        reasons.append("Entry gates passed: setup, probability, and data confidence")
        return Action.BUY
    if setup_score >= 50 and probability >= 0.52:
        return Action.WATCH
    return Action.NO_ACTION


def utc_now() -> datetime:
    """Convenience helper for adapters; tests should pass fixed timestamps."""

    return datetime.now(timezone.utc)
