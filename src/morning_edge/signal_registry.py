"""Explicit hypotheses and promotion state for Codex Screener signals."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


REGISTRY_VERSION = "codex-screener-signal-registry-v1"


class SignalStatus(StrEnum):
    CONTEXT_ONLY = "CONTEXT_ONLY"
    CANDIDATE = "CANDIDATE"
    VALIDATED = "VALIDATED"
    RETIRED = "RETIRED"


@dataclass(frozen=True, slots=True)
class SignalDefinition:
    signal_id: str
    name: str
    datasets: tuple[str, ...]
    mechanism: str
    expected_horizons: tuple[int, ...]
    falsifier: str
    status: SignalStatus
    collection_priority: int
    decay: str
    promotion_test: str

    def public_dict(self) -> dict[str, Any]:
        return {**asdict(self), "status": self.status.value}


SIGNALS = (
    SignalDefinition(
        "price.relative_trend", "Relative trend", ("ohlc",),
        "Persistent price movement can continue over short horizons but can reverse when crowded.",
        (1, 5, 10, 20), "No held-out lift over a same-ticker momentum baseline.",
        SignalStatus.CANDIDATE, 1, "days to weeks",
        "Positive chronological lift across at least 60 independent origin sessions.",
    ),
    SignalDefinition(
        "volatility.realized_implied_gap", "Realized versus implied volatility", ("ohlc", "interpolated_iv", "volatility_stats"),
        "The gap informs expected movement price and option richness, not direction by itself.",
        (5, 10, 20), "No improvement in interval score or option-expression selection.",
        SignalStatus.CANDIDATE, 1, "days",
        "Improves held-out interval score without reducing coverage below target.",
    ),
    SignalDefinition(
        "flow.single_leg_opening", "Single-leg opening flow", ("option_flow", "open_interest"),
        "Single-leg opening activity with next-session OI confirmation is less ambiguous than aggregate premium.",
        (1, 5), "Sign or magnitude has unstable held-out lift after OI confirmation.",
        SignalStatus.CONTEXT_ONLY, 1, "minutes to one session",
        "Incremental lift over price and volatility after trade-id deduplication and OI confirmation.",
    ),
    SignalDefinition(
        "flow.intraday_surprise", "Time-of-day flow surprise", ("interval_flow",),
        "A flow burst is unusual only relative to the ticker's normal activity at the same time of day.",
        (1,), "Z-score does not predict persistence, close direction, or first passage out of sample.",
        SignalStatus.CONTEXT_ONLY, 1, "one to three intervals",
        "Separate intraday walk-forward test with exact five-minute cutoffs.",
    ),
    SignalDefinition(
        "gex.level_migration", "GEX level migration", ("dealer_exposure", "greek_exposure"),
        "Rapid movement of modeled exposure concentrations can identify changing response levels.",
        (1, 5), "Migration adds no lift beyond price and volatility or changes sign by provider method.",
        SignalStatus.CONTEXT_ONLY, 2, "minutes to one session",
        "Method-consistent historical states and chronological ablation test.",
    ),
    SignalDefinition(
        "surface.risk_reversal_change", "Risk-reversal skew change", ("risk_reversal_skew",),
        "Put-call IV skew change measures repricing of downside versus upside convexity.",
        (1, 5, 20), "Skew change adds no return, tail, or option-selection lift after volatility controls.",
        SignalStatus.CONTEXT_ONLY, 2, "hours to weeks",
        "Point-in-time skew history with expiry/delta normalization and held-out ablation.",
    ),
    SignalDefinition(
        "market.idiosyncratic_residual", "Market and sector residual", ("ohlc", "market_tide", "sector_tide"),
        "Removing market and sector movement isolates ticker-specific price and flow behavior.",
        (1, 5, 20), "Residual signal is unstable across sectors or does not beat raw movement.",
        SignalStatus.CANDIDATE, 1, "one to five sessions",
        "Chronological pooled model with origin-date clustering and sector holdouts.",
    ),
    SignalDefinition(
        "dark_pool.response_level", "Dark-pool response level", ("dark_pool", "dark_pool_levels"),
        "Concentrated reported trade levels can mark later price response zones; they do not reveal owner intent.",
        (1, 5, 20), "No stable response-rate lift versus volume-at-price controls.",
        SignalStatus.CONTEXT_ONLY, 4, "days to weeks",
        "Ablation must justify its high request and storage cost.",
    ),
    SignalDefinition(
        "news.event_impact", "News event impact", ("news", "catalyst"),
        "New company information can change expected cash flows, volatility, or positioning.",
        (1, 5, 20), "Event categories do not show stable abnormal returns or volatility changes.",
        SignalStatus.CONTEXT_ONLY, 2, "event specific",
        "Timestamped event study against market- and sector-matched controls.",
    ),
)


def registry() -> dict[str, Any]:
    return {
        "schema_version": REGISTRY_VERSION,
        "validated_signal_count": sum(item.status is SignalStatus.VALIDATED for item in SIGNALS),
        "policy": "Only VALIDATED signals may alter calibrated confidence. CONTEXT_ONLY signals may explain but not vote.",
        "signals": [item.public_dict() for item in SIGNALS],
    }


def signal(signal_id: str) -> SignalDefinition:
    matches = [item for item in SIGNALS if item.signal_id == signal_id]
    if len(matches) != 1:
        raise KeyError(f"unknown signal: {signal_id}")
    return matches[0]
