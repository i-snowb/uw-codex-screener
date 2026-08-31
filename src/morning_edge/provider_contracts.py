"""Machine-readable provider field semantics used before feature derivation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


CONTRACT_VERSION = "unusual-whales-semantics-v1"


class Accumulation(StrEnum):
    INTERVAL_DELTA = "interval_delta"
    PARTIAL_BUCKET = "partial_bucket"
    DAILY_CUMULATIVE = "daily_cumulative"
    CURRENT_STATE = "current_state"
    EVENT = "event"


@dataclass(frozen=True, slots=True)
class FieldContract:
    topic: str
    message: str
    field: str
    unit: str
    accumulation: Accumulation
    reset: str
    revision_behavior: str
    directional_semantics: str
    safe_aggregation: str
    source_url: str

    @property
    def contract_id(self) -> str:
        return f"{self.topic}:{self.message}.{self.field}"

    def public_dict(self) -> dict[str, Any]:
        return {"contract_id": self.contract_id, **asdict(self), "accumulation": self.accumulation.value}


CONTRACTS = (
    FieldContract(
        "greek-flow", "GreekFlow", "dir_delta_flow", "delta shares",
        Accumulation.PARTIAL_BUCKET, "minute bucket", "multiple partial messages share a timestamp",
        "absolute delta exposure signed positive for bullish-classified trades and negative for bearish-classified trades",
        "sum messages with the same ticker and minute; then sum distinct minute buckets",
        "https://api.unusualwhales.com/docs/kafka/types/GreekFlow",
    ),
    FieldContract(
        "greek-flow", "GreekFlow", "dir_vega_flow", "vega exposure",
        Accumulation.PARTIAL_BUCKET, "minute bucket", "multiple partial messages share a timestamp",
        "absolute vega signed positive for ask-side buys and negative for bid-side sells",
        "sum messages with the same ticker and minute; then sum distinct minute buckets",
        "https://api.unusualwhales.com/docs/kafka/types/GreekFlow",
    ),
    FieldContract(
        "interval-flow", "TickerInterval", "net_call_prem", "USD premium",
        Accumulation.INTERVAL_DELTA, "five-minute interval", "final interval can update before close",
        "provider-defined net call premium for the interval",
        "sum non-overlapping interval messages of the same interval_type only",
        "https://api.unusualwhales.com/docs/kafka/types/TickerInterval",
    ),
    FieldContract(
        "option-states", "OptionState", "volume", "contracts",
        Accumulation.DAILY_CUMULATIVE, "trading session", "can decrease after a canceled transaction",
        "none",
        "use the latest state; do not sum states; compute deltas only with cancellation handling",
        "https://api.unusualwhales.com/docs/kafka/types/OptionState",
    ),
    FieldContract(
        "option-states", "OptionState", "canceled_volume", "contracts",
        Accumulation.DAILY_CUMULATIVE, "trading session", "increases when cancellations are recognized",
        "none", "use the latest state; do not sum states",
        "https://api.unusualwhales.com/docs/kafka/types/OptionState",
    ),
    FieldContract(
        "chain-frag", "ChainFrag", "volume", "contracts",
        Accumulation.INTERVAL_DELTA, "five-minute interval", "interval can update before close",
        "none", "sum one finalized value per non-overlapping interval",
        "https://api.unusualwhales.com/docs/kafka/types/ChainFrag",
    ),
    FieldContract(
        "chain-frag", "ChainFrag", "total_volume", "contracts",
        Accumulation.DAILY_CUMULATIVE, "trading session", "latest state supersedes earlier state",
        "none", "use latest state; do not sum messages",
        "https://api.unusualwhales.com/docs/kafka/types/ChainFrag",
    ),
    FieldContract(
        "risk-reversal-skew", "RiskReversalSkew", "skew", "volatility points",
        Accumulation.CURRENT_STATE, "ticker, expiry, and delta bucket", "new state supersedes earlier state",
        "put IV minus call IV; positive means puts are more expensive",
        "use latest state per ticker, expiry, and delta; changes require two timestamped states",
        "https://api.unusualwhales.com/docs/kafka/types/RiskReversalSkew",
    ),
    FieldContract(
        "flow-alerts", "FlowAlert", "total_premium", "USD premium",
        Accumulation.EVENT, "unique alert id", "alert is an aggregate event and can share trades with no other deduplicated alert",
        "not directional by itself",
        "deduplicate by alert id and linked trade ids; derive direction from side fields and contract type",
        "https://api.unusualwhales.com/docs/kafka/types/FlowAlert",
    ),
    FieldContract(
        "live-gex", "GexStrike", "call_gamma_oi", "gamma exposure",
        Accumulation.CURRENT_STATE, "ticker and strike", "new state supersedes earlier state",
        "modeled exposure; not verified dealer inventory",
        "use latest state per ticker and strike; preserve the provider timestamp",
        "https://api.unusualwhales.com/docs/kafka/types/GexStrike",
    ),
    FieldContract(
        "multi-leg-spreads", "MultiLegSpread", "net_delta", "delta exposure",
        Accumulation.EVENT, "unique spread id", "classified spread is immutable unless provider republishes it",
        "provider-classified spread direction; not owner identity",
        "deduplicate by spread id; analyze at spread level, not as independent legs",
        "https://api.unusualwhales.com/docs/kafka/types/MultiLegSpread",
    ),
)


def contract_catalog() -> dict[str, Any]:
    rows = [item.public_dict() for item in CONTRACTS]
    return {
        "schema_version": CONTRACT_VERSION,
        "status": "ENFORCED_FOR_REGISTERED_FIELDS",
        "contracts": rows,
        "unregistered_field_policy": "CONTEXT_ONLY_UNTIL_CONTRACTED",
    }


def contract_for(topic: str, message: str, field: str) -> FieldContract:
    matches = [
        item for item in CONTRACTS
        if item.topic == topic and item.message == message and item.field == field
    ]
    if len(matches) != 1:
        raise KeyError(f"no unique provider contract for {topic}:{message}.{field}")
    return matches[0]
