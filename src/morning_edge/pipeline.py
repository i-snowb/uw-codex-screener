"""Reproducible provider-neutral analysis orchestration.

The pipeline consumes already-normalized input.  It does not fetch data, map
vendor schemas, or make claims about an upstream feed.  Each run carries its
raw snapshot identifiers and a canonical feature payload, so a stored decision
can be audited without reconstructing the provider request.

Ledger integration uses :class:`morning_edge.ledger.ForecastRecord` when the
ledger module is installed.  The returned storage receipt remains opaque so
this module does not assume a database implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import json
from typing import Any, Mapping, Protocol, Sequence

from .ledger import ForecastRecord as LedgerForecastRecord
from .models import canonical_json, payload_digest, timestamp_text, utc_timestamp
from .report import render_markdown
from .scoring import Action, Evidence, MorningInputs, ScoreResult, score


class ForecastLedger(Protocol):
    """Minimal append-only dependency expected by :func:`run_analysis`."""

    def insert_forecast(self, record: Any) -> object:
        """Persist an immutable record and return an implementation receipt."""


def _json_copy(value: Any, *, field_name: str) -> Any:
    """Validate JSON and detach caller-owned mutable containers."""

    try:
        return json.loads(canonical_json(value))
    except ValueError as error:
        raise ValueError(f"{field_name} must be JSON serializable") from error


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """Exact immutable evidence reference used to produce normalized features."""

    snapshot_id: int | str
    as_of: datetime
    retrieved_at: datetime
    feature_payload: Any
    symbol: str | None = None
    _feature_digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.snapshot_id, bool) or (
            not isinstance(self.snapshot_id, (int, str))
        ):
            raise TypeError("snapshot_id must be an integer or non-empty string")
        if isinstance(self.snapshot_id, int) and self.snapshot_id <= 0:
            raise ValueError("integer snapshot_id must be positive")
        if isinstance(self.snapshot_id, str) and not self.snapshot_id.strip():
            raise ValueError("string snapshot_id must not be empty")
        object.__setattr__(self, "as_of", utc_timestamp(self.as_of, field_name="source as_of"))
        object.__setattr__(self, "retrieved_at", utc_timestamp(self.retrieved_at, field_name="source retrieved_at"))
        if self.as_of > self.retrieved_at:
            raise ValueError("source as_of cannot be later than source retrieved_at")
        symbol = self.symbol.strip().upper() if self.symbol is not None else None
        if symbol == "":
            raise ValueError("source symbol must not be empty when supplied")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "feature_payload", _json_copy(self.feature_payload, field_name="feature_payload"))
        object.__setattr__(self, "_feature_digest", payload_digest(self.feature_payload))

    @property
    def id_text(self) -> str:
        return str(self.snapshot_id)

    @property
    def feature_digest(self) -> str:
        return self._feature_digest


@dataclass(frozen=True, slots=True)
class AnalysisRequest:
    """One cutoff-bound analysis request.

    `captured_at` on ``inputs`` must equal `cutoff_at`.  This prevents an
    analysis from being presented as a later snapshot than its normalized
    evidence supports.
    """

    inputs: MorningInputs
    cutoff_at: datetime
    generated_at: datetime
    horizon_sessions: int
    sources: Sequence[SourceSnapshot]
    feature_payload: Any
    trigger_assumptions: Mapping[str, Any] | None = None
    invalidation_assumptions: Mapping[str, Any] | None = None
    option_assumptions: Mapping[str, Any] | None = None
    inputs_digest: str = field(init=False)
    feature_payload_digest: str = field(init=False)
    trigger_assumptions_digest: str | None = field(init=False)
    invalidation_assumptions_digest: str | None = field(init=False)
    option_assumptions_digest: str | None = field(init=False)

    def __post_init__(self) -> None:
        cutoff = utc_timestamp(self.cutoff_at, field_name="cutoff_at")
        captured = utc_timestamp(self.inputs.captured_at, field_name="inputs.captured_at")
        if captured != cutoff:
            raise ValueError("inputs.captured_at must equal cutoff_at")
        object.__setattr__(self, "cutoff_at", cutoff)
        generated = utc_timestamp(self.generated_at, field_name="generated_at")
        if generated < cutoff:
            raise ValueError("generated_at cannot be earlier than cutoff_at")
        object.__setattr__(self, "generated_at", generated)
        if not isinstance(self.horizon_sessions, int) or isinstance(self.horizon_sessions, bool) or self.horizon_sessions < 1:
            raise ValueError("horizon_sessions must be a positive integer")
        sources = tuple(self.sources)
        if not sources:
            raise ValueError("at least one exact source snapshot is required")
        if len({source.id_text for source in sources}) != len(sources):
            raise ValueError("source snapshot IDs must be unique per analysis request")
        for source in sources:
            if source.retrieved_at > cutoff:
                raise ValueError("source retrieved_at cannot be later than the analysis cutoff")
            if source.symbol is not None and source.symbol != self.inputs.ticker:
                raise ValueError("source symbol must match inputs.ticker when supplied")
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "feature_payload", _json_copy(self.feature_payload, field_name="feature_payload"))
        for field_name in ("trigger_assumptions", "invalidation_assumptions", "option_assumptions"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _json_copy(value, field_name=field_name))
        # Cache all hashes at the immutable request boundary.  A caller can hold
        # a reference to a nested mapping despite frozen dataclasses; later
        # mutation must not rewrite the provenance of this run.
        object.__setattr__(self, "inputs_digest", normalized_inputs_digest(self.inputs))
        object.__setattr__(self, "feature_payload_digest", payload_digest(self.feature_payload))
        object.__setattr__(self, "trigger_assumptions_digest", _digest_or_none(self.trigger_assumptions))
        object.__setattr__(self, "invalidation_assumptions_digest", _digest_or_none(self.invalidation_assumptions))
        object.__setattr__(self, "option_assumptions_digest", _digest_or_none(self.option_assumptions))


@dataclass(frozen=True, slots=True)
class AnalysisRecord:
    """The immutable in-memory summary produced before ledger translation."""

    ticker: str
    scoring_version: str
    cutoff_at: datetime
    generated_at: datetime
    horizon_sessions: int
    source_snapshot_ids: tuple[str, ...]
    source_feature_digests: tuple[str, ...]
    normalized_inputs_digest: str
    feature_payload_digest: str
    trigger_assumptions_digest: str | None
    invalidation_assumptions_digest: str | None
    option_assumptions_digest: str | None
    action: Action
    setup_score: int
    directional_probability: float
    confidence: int
    execution_ready: bool
    calibration_ready: bool
    data_gate_passed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    score: ScoreResult
    forecast_record: AnalysisRecord
    ledger_record: Any | None
    report_markdown: str
    ledger_receipt: object | None


def _evidence_payload(value: Evidence | None) -> Mapping[str, Any] | None:
    if value is None:
        return None
    return {
        "value": value.value,
        "as_of": timestamp_text(value.as_of),
        "available_at": timestamp_text(value.available_at),
        "provenance": value.provenance.value,
        "source": value.source,
        "quality": value.quality,
    }


def normalized_inputs_digest(inputs: MorningInputs) -> str:
    """Hash all normalized decision inputs with no provider-specific behavior."""

    position: Mapping[str, Any] | None = None
    if inputs.position is not None:
        position = {
            "contracts": inputs.position.contracts,
            "unrealized_return_pct": inputs.position.unrealized_return_pct,
            "days_to_expiry": inputs.position.days_to_expiry,
            "option_side": inputs.position.option_side,
        }
    payload = {
        "ticker": inputs.ticker,
        "captured_at": timestamp_text(inputs.captured_at),
        "price": _evidence_payload(inputs.price),
        "trend": _evidence_payload(inputs.trend),
        "flow": _evidence_payload(inputs.flow),
        "oi_change": _evidence_payload(inputs.oi_change),
        "gex": _evidence_payload(inputs.gex),
        "iv_rank": _evidence_payload(inputs.iv_rank),
        "bid_ask_spread_pct": _evidence_payload(inputs.bid_ask_spread_pct),
        "catalyst": _evidence_payload(inputs.catalyst),
        "event_risk": _evidence_payload(inputs.event_risk),
        "position": position,
        "execution_ready": inputs.execution_ready,
        "calibration_ready": inputs.calibration_ready,
        "metadata": dict(inputs.metadata),
    }
    return payload_digest(payload)


def _fail_closed(result: ScoreResult) -> ScoreResult:
    """Block entries on bad data while preserving deterministic position exits."""

    if result.data_gate.passed or result.action in {Action.EXIT, Action.TRIM}:
        return result
    reason = "Pipeline fail-closed: data gate blocked; new entries and position WATCH actions are disabled."
    reasons = result.reasons if reason in result.reasons else (*result.reasons, reason)
    return replace(result, action=Action.NO_ACTION, reasons=reasons)


def _digest_or_none(value: Mapping[str, Any] | None) -> str | None:
    return payload_digest(value) if value is not None else None


def _record_for(request: AnalysisRequest, result: ScoreResult) -> AnalysisRecord:
    return AnalysisRecord(
        ticker=request.inputs.ticker,
        scoring_version=result.scoring_version,
        cutoff_at=request.cutoff_at,
        generated_at=request.generated_at,
        horizon_sessions=request.horizon_sessions,
        source_snapshot_ids=tuple(source.id_text for source in request.sources),
        source_feature_digests=tuple(source.feature_digest for source in request.sources),
        normalized_inputs_digest=request.inputs_digest,
        feature_payload_digest=request.feature_payload_digest,
        trigger_assumptions_digest=request.trigger_assumptions_digest,
        invalidation_assumptions_digest=request.invalidation_assumptions_digest,
        option_assumptions_digest=request.option_assumptions_digest,
        action=result.action,
        setup_score=result.setup_score,
        directional_probability=result.directional_probability,
        confidence=result.confidence,
        execution_ready=result.execution_ready,
        calibration_ready=result.calibration_ready,
        data_gate_passed=result.data_gate.passed,
        reasons=result.reasons,
    )


def _ledger_record_for(request: AnalysisRequest, result: ScoreResult) -> Any:
    """Translate an analysis record into the ledger's strict audit contract."""

    if any(not isinstance(source.snapshot_id, int) for source in request.sources):
        raise ValueError("ledger persistence requires positive integer source snapshot IDs")
    trigger = canonical_json(request.trigger_assumptions) if request.trigger_assumptions else "Not specified"
    invalidation = (
        canonical_json(request.invalidation_assumptions)
        if request.invalidation_assumptions
        else "Not specified"
    )
    return LedgerForecastRecord(
        ticker=request.inputs.ticker,
        cutoff_at=request.cutoff_at,
        generated_at=request.generated_at,
        horizon_sessions=request.horizon_sessions,
        scoring_version=result.scoring_version,
        model_version="rules-v1",
        action=result.action.value,
        setup_score=result.setup_score,
        directional_probability=result.directional_probability,
        confidence=result.confidence / 100.0,
        source_snapshot_ids=tuple(source.snapshot_id for source in request.sources),
        trigger=trigger,
        invalidation=invalidation,
        feature_payload=request.feature_payload,
        option_metadata=request.option_assumptions,
        friction_metadata={"data_gate_passed": result.data_gate.passed},
        metadata={
            "calibration_ready": result.calibration_ready,
            "execution_ready": result.execution_ready,
            "normalized_inputs_digest": request.inputs_digest,
            "source_feature_digests": [source.feature_digest for source in request.sources],
            "trigger_assumptions_digest": request.trigger_assumptions_digest,
            "invalidation_assumptions_digest": request.invalidation_assumptions_digest,
            "option_assumptions_digest": request.option_assumptions_digest,
        },
    )


def run_analysis(request: AnalysisRequest, *, ledger: ForecastLedger | None = None) -> AnalysisResult:
    """Score, fail closed if required, append to an optional ledger, and report.

    The only side effect is `ledger.insert_forecast`.  It runs after all request
    validation and before the result is returned; a ledger error is surfaced so
    callers never mistake an unpersisted forecast for an auditable one.
    """

    result = _fail_closed(score(request.inputs))
    if result.action is Action.BUY and (not request.trigger_assumptions or not request.invalidation_assumptions):
        reason = "BUY downgraded to WATCH: explicit nonempty trigger and invalidation assumptions are required."
        result = replace(result, action=Action.WATCH, reasons=(*result.reasons, reason))
    record = _record_for(request, result)
    ledger_record = _ledger_record_for(request, result) if ledger is not None else None
    receipt = ledger.insert_forecast(ledger_record) if ledger is not None else None
    return AnalysisResult(
        score=result,
        forecast_record=record,
        ledger_record=ledger_record,
        report_markdown=render_markdown((result,), generated_at=request.generated_at),
        ledger_receipt=receipt,
    )
