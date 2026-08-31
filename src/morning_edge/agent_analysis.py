"""Evidence-bound contract for optional daily analyst synthesis.

This module intentionally separates a language-model explanation from the
deterministic feature and decision layers.  An analyst may summarize, compare,
and surface contradictions, but it cannot cite evidence outside the immutable
bundle, turn an uncalibrated scenario into a probability, or authorize a BUY.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
import json
import re
from typing import Any, Mapping, Protocol, Sequence
from urllib import error as urlerror
from urllib import request as urlrequest

from .models import canonical_json, payload_digest, timestamp_text, utc_timestamp
from .providers.base import ProviderResponseError, open_without_redirects, read_bounded_body


ANALYSIS_SCHEMA_VERSION = "1.0.0"


class AnalysisStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    ABSTAIN = "ABSTAIN"
    BLOCKED = "BLOCKED"


class FeatureStatus(StrEnum):
    VALID = "VALID"
    MISSING = "MISSING"
    STALE = "STALE"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    ENTITLEMENT_DENIED = "ENTITLEMENT_DENIED"
    NOT_REQUESTED = "NOT_REQUESTED"


class Direction(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class AssertionType(StrEnum):
    OBSERVATION = "OBSERVATION"
    INFERENCE = "INFERENCE"
    HYPOTHESIS = "HYPOTHESIS"


class ScenarioName(StrEnum):
    BULL = "BULL"
    BASE = "BASE"
    BEAR = "BEAR"


class SuggestedAction(StrEnum):
    NO_RECOMMENDATION = "NO_RECOMMENDATION"
    WATCH = "WATCH"
    BUY = "BUY"


class AgentAnalysisValidationError(ValueError):
    """Raised when synthesized analysis is not supported by its evidence."""


def _json_copy(value: Any, *, field_name: str) -> Any:
    try:
        return json.loads(canonical_json(value))
    except ValueError as error:
        raise ValueError(f"{field_name} must be JSON serializable") from error


def _non_empty(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _unique(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    result = tuple(_non_empty(value, field_name) for value in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} values must be unique")
    return result


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    """A source pointer only; provider payload remains in the snapshot store."""

    source_id: str
    snapshot_id: int | str
    dataset: str
    as_of: datetime
    retrieved_at: datetime
    payload_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _non_empty(self.source_id, "source_id"))
        if isinstance(self.snapshot_id, bool) or not isinstance(self.snapshot_id, (int, str)):
            raise TypeError("snapshot_id must be an integer or non-empty string")
        if isinstance(self.snapshot_id, int) and self.snapshot_id < 1:
            raise ValueError("integer snapshot_id must be positive")
        if isinstance(self.snapshot_id, str):
            object.__setattr__(self, "snapshot_id", _non_empty(self.snapshot_id, "snapshot_id"))
        object.__setattr__(self, "dataset", _non_empty(self.dataset, "dataset"))
        as_of = utc_timestamp(self.as_of, field_name="source as_of")
        retrieved = utc_timestamp(self.retrieved_at, field_name="source retrieved_at")
        if as_of > retrieved:
            raise ValueError("source as_of cannot be later than source retrieved_at")
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "retrieved_at", retrieved)
        digest = self.payload_hash.lower().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("payload_hash must be a lowercase SHA-256 digest")
        object.__setattr__(self, "payload_hash", digest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id, "snapshot_id": self.snapshot_id, "dataset": self.dataset,
            "as_of": timestamp_text(self.as_of), "retrieved_at": timestamp_text(self.retrieved_at),
            "payload_hash": self.payload_hash,
        }


@dataclass(frozen=True, slots=True)
class EvidenceFeature:
    """One deterministic calculation with explicit source lineage."""

    feature_id: str
    value: Any
    unit: str
    provenance: str
    transform_id: str
    source_refs: tuple[str, ...]
    available_at: datetime
    quality: float
    status: FeatureStatus = FeatureStatus.VALID

    def __post_init__(self) -> None:
        object.__setattr__(self, "feature_id", _non_empty(self.feature_id, "feature_id"))
        object.__setattr__(self, "value", _json_copy(self.value, field_name="feature value"))
        for name in ("unit", "provenance", "transform_id"):
            object.__setattr__(self, name, _non_empty(getattr(self, name), name))
        object.__setattr__(self, "source_refs", _unique(self.source_refs, "source_refs"))
        object.__setattr__(self, "available_at", utc_timestamp(self.available_at, field_name="feature available_at"))
        if not 0.0 <= self.quality <= 1.0:
            raise ValueError("feature quality must be between 0 and 1")
        object.__setattr__(self, "status", FeatureStatus(self.status))

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_id": self.feature_id, "value": self.value, "unit": self.unit,
            "provenance": self.provenance, "transform_id": self.transform_id,
            "source_refs": list(self.source_refs), "available_at": timestamp_text(self.available_at),
            "quality": self.quality, "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """All evidence an analyst is allowed to use for one cutoff-bound read."""

    analysis_id: str
    ticker: str
    cutoff_at: datetime
    feature_version: str
    sources: tuple[EvidenceSource, ...]
    features: tuple[EvidenceFeature, ...]
    required_feature_ids: tuple[str, ...] = ()
    execution_ready: bool = False
    calibration_ready: bool = False
    buy_authorized: bool = False
    calibration_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "analysis_id", _non_empty(self.analysis_id, "analysis_id"))
        ticker = _non_empty(self.ticker, "ticker").upper()
        if ticker != self.ticker:
            raise ValueError("ticker must be uppercase")
        object.__setattr__(self, "ticker", ticker)
        cutoff = utc_timestamp(self.cutoff_at, field_name="cutoff_at")
        object.__setattr__(self, "cutoff_at", cutoff)
        object.__setattr__(self, "feature_version", _non_empty(self.feature_version, "feature_version"))
        sources = tuple(self.sources)
        if not sources:
            raise ValueError("at least one evidence source is required")
        source_ids = _unique(tuple(source.source_id for source in sources), "source_id")
        for source in sources:
            if source.retrieved_at > cutoff:
                raise ValueError("source retrieved_at cannot be later than cutoff_at")
        features = tuple(self.features)
        feature_ids = _unique(tuple(feature.feature_id for feature in features), "feature_id")
        for feature in features:
            if feature.available_at > cutoff:
                raise ValueError("feature available_at cannot be later than cutoff_at")
            unknown = set(feature.source_refs) - set(source_ids)
            if unknown:
                raise ValueError(f"feature {feature.feature_id} refers to unknown sources: {sorted(unknown)}")
        required = _unique(self.required_feature_ids, "required_feature_ids")
        unknown_required = set(required) - set(feature_ids)
        if unknown_required:
            raise ValueError(f"required features are absent: {sorted(unknown_required)}")
        calibration_ids = _unique(self.calibration_ids, "calibration_ids")
        if self.buy_authorized and not (self.execution_ready and self.calibration_ready):
            raise ValueError("buy_authorized requires execution_ready and calibration_ready")
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "features", features)
        object.__setattr__(self, "required_feature_ids", required)
        object.__setattr__(self, "calibration_ids", calibration_ids)

    @property
    def valid_features(self) -> tuple[EvidenceFeature, ...]:
        return tuple(feature for feature in self.features if feature.status is FeatureStatus.VALID)

    @property
    def digest(self) -> str:
        return payload_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_id": self.analysis_id, "ticker": self.ticker, "cutoff_at": timestamp_text(self.cutoff_at),
            "feature_version": self.feature_version, "sources": [source.to_dict() for source in self.sources],
            "features": [feature.to_dict() for feature in self.features],
            "required_feature_ids": list(self.required_feature_ids), "execution_ready": self.execution_ready,
            "calibration_ready": self.calibration_ready, "buy_authorized": self.buy_authorized,
            "calibration_ids": list(self.calibration_ids),
        }


@dataclass(frozen=True, slots=True)
class Claim:
    claim_id: str
    topic: str
    direction: Direction
    horizon_sessions: int
    statement: str
    assertion_type: AssertionType
    supporting_feature_ids: tuple[str, ...]
    contradicting_feature_ids: tuple[str, ...]
    source_refs: tuple[str, ...]
    counterevidence_summary: str
    limitations: str

    def __post_init__(self) -> None:
        for name in ("claim_id", "topic", "statement", "counterevidence_summary", "limitations"):
            object.__setattr__(self, name, _non_empty(getattr(self, name), name))
        if self.horizon_sessions < 1:
            raise ValueError("claim horizon_sessions must be positive")
        object.__setattr__(self, "direction", Direction(self.direction))
        object.__setattr__(self, "assertion_type", AssertionType(self.assertion_type))
        object.__setattr__(self, "supporting_feature_ids", _unique(self.supporting_feature_ids, "supporting_feature_ids"))
        object.__setattr__(self, "contradicting_feature_ids", _unique(self.contradicting_feature_ids, "contradicting_feature_ids"))
        object.__setattr__(self, "source_refs", _unique(self.source_refs, "claim source_refs"))
        if not self.supporting_feature_ids or not self.source_refs:
            raise ValueError("claims require supporting_feature_ids and source_refs")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["direction"] = self.direction.value
        data["assertion_type"] = self.assertion_type.value
        return data


@dataclass(frozen=True, slots=True)
class Scenario:
    name: ScenarioName
    conditions: tuple[str, ...]
    directional_outcome: Direction
    horizon_sessions: int
    evidence_feature_ids: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    probability: float | None = None
    calibration_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", ScenarioName(self.name))
        object.__setattr__(self, "directional_outcome", Direction(self.directional_outcome))
        object.__setattr__(self, "conditions", _unique(self.conditions, "scenario conditions"))
        object.__setattr__(self, "evidence_feature_ids", _unique(self.evidence_feature_ids, "scenario evidence_feature_ids"))
        object.__setattr__(self, "invalidation_conditions", _unique(self.invalidation_conditions, "scenario invalidation_conditions"))
        if not self.conditions or not self.invalidation_conditions:
            raise ValueError("scenarios require conditions and invalidation conditions")
        if self.horizon_sessions < 1:
            raise ValueError("scenario horizon_sessions must be positive")
        if self.probability is not None and not 0.0 <= self.probability <= 1.0:
            raise ValueError("scenario probability must be between 0 and 1")
        if self.calibration_id is not None:
            object.__setattr__(self, "calibration_id", _non_empty(self.calibration_id, "calibration_id"))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["name"] = self.name.value
        data["directional_outcome"] = self.directional_outcome.value
        return data


@dataclass(frozen=True, slots=True)
class Unknown:
    field_or_question: str
    reason: str
    blocked_capability: str
    resolution: str

    def __post_init__(self) -> None:
        for name in ("field_or_question", "reason", "blocked_capability", "resolution"):
            object.__setattr__(self, name, _non_empty(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class Contradiction:
    feature_ids: tuple[str, ...]
    severity: str
    impact: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "feature_ids", _unique(self.feature_ids, "contradiction feature_ids"))
        if len(self.feature_ids) < 2:
            raise ValueError("a contradiction requires at least two feature IDs")
        object.__setattr__(self, "severity", _non_empty(self.severity, "severity"))
        object.__setattr__(self, "impact", _non_empty(self.impact, "impact"))


@dataclass(frozen=True, slots=True)
class Confidence:
    overall: int
    data_completeness: int
    freshness: int
    evidence_agreement: int
    source_quality: int
    calibration_quality: int
    execution_quality: int
    event_uncertainty: int
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "overall", "data_completeness", "freshness", "evidence_agreement", "source_quality",
            "calibration_quality", "execution_quality", "event_uncertainty",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or not 0 <= value <= 100:
                raise ValueError(f"confidence {name} must be an integer between 0 and 100")
        object.__setattr__(self, "blockers", _unique(self.blockers, "confidence blockers"))


@dataclass(frozen=True, slots=True)
class AgentAnalysis:
    analysis_schema_version: str
    analysis_id: str
    status: AnalysisStatus
    evidence_summary: str
    claims: tuple[Claim, ...]
    scenarios: tuple[Scenario, ...]
    confidence: Confidence
    suggested_action: SuggestedAction
    unknowns: tuple[Unknown, ...] = ()
    contradictions: tuple[Contradiction, ...] = ()
    agent_model_id: str = "deterministic-fallback-v1"
    prompt_version: str = "agent-contract-v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "analysis_schema_version", _non_empty(self.analysis_schema_version, "analysis_schema_version"))
        object.__setattr__(self, "analysis_id", _non_empty(self.analysis_id, "analysis_id"))
        object.__setattr__(self, "status", AnalysisStatus(self.status))
        object.__setattr__(self, "evidence_summary", _non_empty(self.evidence_summary, "evidence_summary"))
        object.__setattr__(self, "claims", tuple(self.claims))
        scenarios = tuple(self.scenarios)
        if {item.name for item in scenarios} != set(ScenarioName) or len(scenarios) != 3:
            raise ValueError("analysis requires exactly one BULL, BASE, and BEAR scenario")
        object.__setattr__(self, "scenarios", scenarios)
        object.__setattr__(self, "suggested_action", SuggestedAction(self.suggested_action))
        object.__setattr__(self, "unknowns", tuple(self.unknowns))
        object.__setattr__(self, "contradictions", tuple(self.contradictions))
        object.__setattr__(self, "agent_model_id", _non_empty(self.agent_model_id, "agent_model_id"))
        object.__setattr__(self, "prompt_version", _non_empty(self.prompt_version, "prompt_version"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_schema_version": self.analysis_schema_version, "analysis_id": self.analysis_id,
            "status": self.status.value, "evidence_summary": self.evidence_summary,
            "claims": [claim.to_dict() for claim in self.claims],
            "scenarios": [scenario.to_dict() for scenario in self.scenarios],
            "confidence": asdict(self.confidence), "suggested_action": self.suggested_action.value,
            "unknowns": [asdict(item) for item in self.unknowns],
            "contradictions": [asdict(item) for item in self.contradictions],
            "agent_model_id": self.agent_model_id, "prompt_version": self.prompt_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentAnalysis":
        required = {
            "analysis_schema_version", "analysis_id", "status", "evidence_summary", "claims", "scenarios",
            "confidence", "suggested_action",
        }
        missing = required - set(data)
        if missing:
            raise AgentAnalysisValidationError(f"agent result is missing fields: {sorted(missing)}")
        allowed = required | {"unknowns", "contradictions", "agent_model_id", "prompt_version"}
        extra = set(data) - allowed
        if extra:
            raise AgentAnalysisValidationError(f"agent result has unknown fields: {sorted(extra)}")
        try:
            return cls(
                analysis_schema_version=data["analysis_schema_version"], analysis_id=data["analysis_id"],
                status=data["status"], evidence_summary=data["evidence_summary"],
                claims=tuple(Claim(**item) for item in data["claims"]),
                scenarios=tuple(Scenario(**item) for item in data["scenarios"]),
                confidence=Confidence(**data["confidence"]), suggested_action=data["suggested_action"],
                unknowns=tuple(Unknown(**item) for item in data.get("unknowns", ())),
                contradictions=tuple(Contradiction(**item) for item in data.get("contradictions", ())),
                agent_model_id=data.get("agent_model_id", "unknown-agent"),
                prompt_version=data.get("prompt_version", "unknown-prompt"),
            )
        except (TypeError, ValueError) as error:
            raise AgentAnalysisValidationError(str(error)) from error


# A concise machine-readable schema for remote structured-output APIs.  Python
# validation below remains the authority because it can compare output to the
# cutoff-bound EvidenceBundle.
ANALYSIS_JSON_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["analysis_schema_version", "analysis_id", "status", "evidence_summary", "claims", "scenarios", "confidence", "suggested_action"],
    "properties": {
        "analysis_schema_version": {"type": "string"}, "analysis_id": {"type": "string"},
        "status": {"enum": [item.value for item in AnalysisStatus]}, "evidence_summary": {"type": "string"},
        "claims": {"type": "array"}, "scenarios": {"type": "array", "minItems": 3, "maxItems": 3},
        "confidence": {"type": "object"}, "suggested_action": {"enum": [item.value for item in SuggestedAction]},
        "unknowns": {"type": "array"}, "contradictions": {"type": "array"},
        "agent_model_id": {"type": "string"}, "prompt_version": {"type": "string"},
    },
}


class AnalysisCritic:
    """Deterministic cross-checker for agent output against a supplied bundle."""

    _BUY_WORD = re.compile(r"\bbuy(?:ing)?\b", re.IGNORECASE)

    @classmethod
    def validate(cls, bundle: EvidenceBundle, analysis: AgentAnalysis) -> None:
        errors: list[str] = []
        if analysis.analysis_schema_version != ANALYSIS_SCHEMA_VERSION:
            errors.append("analysis schema version is unsupported")
        if analysis.analysis_id != bundle.analysis_id:
            errors.append("analysis_id does not match the evidence bundle")
        feature_ids = {feature.feature_id for feature in bundle.features}
        source_ids = {source.source_id for source in bundle.sources}
        for claim in analysis.claims:
            unknown_features = (
                set(claim.supporting_feature_ids) | set(claim.contradicting_feature_ids)
            ) - feature_ids
            if unknown_features:
                errors.append(f"claim {claim.claim_id} refers to unknown features: {sorted(unknown_features)}")
            unknown_sources = set(claim.source_refs) - source_ids
            if unknown_sources:
                errors.append(f"claim {claim.claim_id} refers to unknown sources: {sorted(unknown_sources)}")
        for scenario in analysis.scenarios:
            unknown_features = set(scenario.evidence_feature_ids) - feature_ids
            if unknown_features:
                errors.append(f"scenario {scenario.name.value} refers to unknown features: {sorted(unknown_features)}")
            if scenario.probability is not None:
                if not bundle.calibration_ready or not scenario.calibration_id or scenario.calibration_id not in bundle.calibration_ids:
                    errors.append(f"scenario {scenario.name.value} has unsupported probability")
            elif scenario.calibration_id is not None:
                errors.append(f"scenario {scenario.name.value} has calibration_id without probability")
        for contradiction in analysis.contradictions:
            unknown_features = set(contradiction.feature_ids) - feature_ids
            if unknown_features:
                errors.append(f"contradiction refers to unknown features: {sorted(unknown_features)}")
        if analysis.status is AnalysisStatus.COMPLETE and analysis.unknowns:
            errors.append("COMPLETE analysis cannot contain unresolved unknowns")
        if analysis.status is AnalysisStatus.BLOCKED and not analysis.unknowns:
            errors.append("BLOCKED analysis requires a visible unknown or blocker")
        if analysis.suggested_action is SuggestedAction.BUY:
            if not bundle.buy_authorized or analysis.status is not AnalysisStatus.COMPLETE:
                errors.append("BUY language is unsupported by the evidence authorization")
        text = " ".join(
            [analysis.evidence_summary]
            + [claim.statement for claim in analysis.claims]
            + [unknown.blocked_capability for unknown in analysis.unknowns]
        )
        if cls._BUY_WORD.search(text) and not bundle.buy_authorized:
            errors.append("BUY language is unsupported by the evidence authorization")
        if errors:
            raise AgentAnalysisValidationError("; ".join(errors))


class AnalystBackend(Protocol):
    """Optional backend. Implementations must return output that the critic accepts."""

    def analyze(self, bundle: EvidenceBundle) -> AgentAnalysis:
        ...


class DeterministicAnalystBackend:
    """Safe baseline used when no external analyst backend is configured."""

    model_id = "deterministic-fallback-v1"

    def analyze(self, bundle: EvidenceBundle) -> AgentAnalysis:
        valid = bundle.valid_features
        unknowns = tuple(
            Unknown(
                field_or_question=feature.feature_id,
                reason=f"Feature status is {feature.status.value}.",
                blocked_capability="Directional interpretation of this feature",
                resolution="Collect an accepted, fresh source snapshot and rebuild the feature.",
            )
            for feature in bundle.features if feature.status is not FeatureStatus.VALID
        )
        required_invalid = {
            feature.feature_id for feature in bundle.features
            if feature.feature_id in bundle.required_feature_ids and feature.status is not FeatureStatus.VALID
        }
        if required_invalid:
            status = AnalysisStatus.BLOCKED
        elif not valid:
            status = AnalysisStatus.ABSTAIN
        elif unknowns:
            status = AnalysisStatus.PARTIAL
        else:
            status = AnalysisStatus.COMPLETE
        source_refs = tuple(sorted({source for feature in valid for source in feature.source_refs}))
        feature_ids = tuple(feature.feature_id for feature in valid)
        claims: tuple[Claim, ...]
        if valid:
            claims = (Claim(
                claim_id="evidence-coverage", topic="evidence coverage", direction=Direction.UNKNOWN,
                horizon_sessions=1,
                statement="The deterministic fallback records available evidence but does not infer market direction.",
                assertion_type=AssertionType.OBSERVATION, supporting_feature_ids=feature_ids,
                contradicting_feature_ids=(), source_refs=source_refs,
                counterevidence_summary="No directional interpretation is produced by the fallback.",
                limitations="A configured analyst backend and validated deterministic signal definitions are required for directional synthesis.",
            ),)
        else:
            claims = ()
        scenario_features = feature_ids or tuple(feature.feature_id for feature in bundle.features)
        scenarios = (
            Scenario(ScenarioName.BULL, ("Positive directional confirmation must be supplied by validated features.",), Direction.BULLISH, 20, scenario_features, ("Do not treat this conditional scenario as a forecast.",)),
            Scenario(ScenarioName.BASE, ("No calibrated directional probability is available.",), Direction.NEUTRAL, 20, scenario_features, ("New accepted evidence may change the state.",)),
            Scenario(ScenarioName.BEAR, ("Negative directional confirmation must be supplied by validated features.",), Direction.BEARISH, 20, scenario_features, ("Do not treat this conditional scenario as a forecast.",)),
        )
        completeness = round(100 * len(valid) / len(bundle.features)) if bundle.features else 0
        confidence = Confidence(
            overall=0, data_completeness=completeness, freshness=0, evidence_agreement=0,
            source_quality=round(100 * sum(feature.quality for feature in valid) / len(valid)) if valid else 0,
            calibration_quality=100 if bundle.calibration_ready else 0,
            execution_quality=100 if bundle.execution_ready else 0,
            event_uncertainty=100, blockers=("Deterministic fallback does not make directional or trade recommendations.",),
        )
        analysis = AgentAnalysis(
            analysis_schema_version=ANALYSIS_SCHEMA_VERSION, analysis_id=bundle.analysis_id, status=status,
            evidence_summary=f"{len(valid)} of {len(bundle.features)} features are valid; no directional conclusion is generated.",
            claims=claims, scenarios=scenarios, confidence=confidence,
            suggested_action=SuggestedAction.NO_RECOMMENDATION, unknowns=unknowns,
            agent_model_id=self.model_id,
        )
        AnalysisCritic.validate(bundle, analysis)
        return analysis


class ResponsesApiBackend:
    """Optional stdlib-only OpenAI Responses API backend.

    The key remains in memory only.  The caller supplies it directly; this
    module does not read environment variables, files, or secret stores.
    """

    endpoint = "https://api.openai.com/v1/responses"
    max_response_bytes = 2 * 1024 * 1024

    def __init__(self, *, api_key: str, model: str, timeout_seconds: float = 30.0) -> None:
        self._api_key = _non_empty(api_key, "api_key")
        self.model = _non_empty(model, "model")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = float(timeout_seconds)

    def analyze(self, bundle: EvidenceBundle) -> AgentAnalysis:
        instructions = (
            "Return only JSON matching the supplied schema. Use only feature IDs and source IDs in the evidence bundle. "
            "Do not add a probability without a supplied calibration ID. Do not use BUY language unless buy_authorized is true."
        )
        payload = {
            "model": self.model,
            "instructions": instructions,
            "input": json.dumps({"evidence_bundle": bundle.to_dict()}, separators=(",", ":")),
            "text": {"format": {"type": "json_schema", "name": "morning_edge_agent_analysis", "strict": True, "schema": ANALYSIS_JSON_SCHEMA}},
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urlrequest.Request(
            self.endpoint, data=body,
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}, method="POST",
        )
        try:
            with open_without_redirects(request, timeout_seconds=self.timeout_seconds) as response:
                response_headers = dict(response.headers.items())
                response_payload = json.loads(
                    read_bounded_body(
                        response,
                        response_headers,
                        maximum_bytes=self.max_response_bytes,
                    ).decode("utf-8")
                )
        except (urlerror.URLError, TimeoutError, json.JSONDecodeError, ProviderResponseError) as error:
            raise RuntimeError("analyst backend request failed") from error
        text = response_payload.get("output_text")
        if not isinstance(text, str):
            text = _response_text(response_payload)
        if not isinstance(text, str):
            raise RuntimeError("analyst backend returned no structured text")
        try:
            analysis = AgentAnalysis.from_dict(json.loads(text))
        except json.JSONDecodeError as error:
            raise AgentAnalysisValidationError("analyst backend returned invalid JSON") from error
        AnalysisCritic.validate(bundle, analysis)
        return analysis


def _response_text(response_payload: Mapping[str, Any]) -> str | None:
    for output in response_payload.get("output", ()):
        for content in output.get("content", ()):
            text = content.get("text")
            if isinstance(text, str):
                return text
    return None
