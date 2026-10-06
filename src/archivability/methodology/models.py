from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping


class ResultState(StrEnum):
    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"
    CONTEXT = "context"


class GateState(StrEnum):
    INACTIVE = "inactive"
    TRIGGERED = "triggered"
    NOT_TRIGGERED = "not_triggered"
    UNKNOWN = "unknown"


class OverallState(StrEnum):
    BLOCKED = "blocked"
    ASSESSABLE = "assessable"
    PARTIALLY_ASSESSABLE = "partially_assessable"
    INDETERMINATE = "indeterminate"


class MethodologyValidationError(ValueError):
    """Raised when a methodology package is inconsistent or unsafe to evaluate."""


class ScoringInputError(ValueError):
    """Raised when analysis results cannot be scored deterministically."""


@dataclass(frozen=True, slots=True)
class DimensionDefinition:
    id: str
    name: str
    weight: float
    score_enabled: bool


@dataclass(frozen=True, slots=True)
class IndicatorDefinition:
    id: str
    slug: str
    dimension: str | None
    classification: str
    score_enabled: bool
    weight: float
    score_rule: str
    blocking_candidates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GateCondition:
    indicator_id: str
    states: tuple[ResultState, ...] | None = None
    score_lte: float | None = None


@dataclass(frozen=True, slots=True)
class GateDefinition:
    id: str
    name: str
    status: str
    effect: str
    minimum_confidence: float
    conditions: tuple[GateCondition, ...]


@dataclass(frozen=True, slots=True)
class MethodologyConfig:
    id: str
    version: str
    status: str
    dimensions: Mapping[str, DimensionDefinition]
    indicators: Mapping[str, IndicatorDefinition]
    gates: Mapping[str, GateDefinition]
    status_score_map: Mapping[ResultState, float]
    excluded_states: frozenset[ResultState]
    minimum_coverage: float
    legacy_facet_max_weights: Mapping[str, float]

    @staticmethod
    def immutable_mapping(values: Mapping[str, Any]) -> Mapping[str, Any]:
        return MappingProxyType(dict(values))


@dataclass(frozen=True, slots=True)
class IndicatorResult:
    indicator_id: str
    state: ResultState
    confidence: float
    score: float | None = None
    analysis_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    evidence_hashes: tuple[str, ...] = ()
    measured_at: datetime | None = None
    probe_ids: tuple[str, ...] = ()
    tool_versions: tuple[str, ...] = ()
    measurement_method_version: str | None = None

    def __post_init__(self) -> None:
        if not self.indicator_id:
            raise ScoringInputError("indicator_id must not be empty")
        if not isinstance(self.state, ResultState):
            raise ScoringInputError("state must be a ResultState")
        if not 0 <= self.confidence <= 1:
            raise ScoringInputError("confidence must be between 0 and 1")
        if self.score is not None and not 0 <= self.score <= 100:
            raise ScoringInputError("indicator score must be between 0 and 100")
        if self.state in {
            ResultState.UNKNOWN,
            ResultState.NOT_APPLICABLE,
            ResultState.CONTEXT,
        } and self.score is not None:
            raise ScoringInputError(f"{self.state.value} results cannot carry a score")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ScoringInputError("evidence_ids must be unique")
        if len(self.evidence_ids) != len(self.evidence_hashes):
            raise ScoringInputError(
                "evidence_ids and evidence_hashes must have the same length"
            )
        if any(not value for value in self.evidence_ids):
            raise ScoringInputError("evidence_ids cannot contain empty values")
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in self.evidence_hashes):
            raise ScoringInputError("evidence_hashes must contain SHA-256 digests")
        if len(set(self.probe_ids)) != len(self.probe_ids):
            raise ScoringInputError("probe_ids must be unique")
        if len(set(self.tool_versions)) != len(self.tool_versions):
            raise ScoringInputError("tool_versions must be unique")
        if any(not value for value in (*self.probe_ids, *self.tool_versions)):
            raise ScoringInputError("provenance values cannot be empty")
        if self.measured_at is not None and (
            self.measured_at.tzinfo is None
            or self.measured_at.utcoffset() is None
        ):
            raise ScoringInputError("measured_at must include a timezone")

    def to_dict(self) -> dict[str, Any]:
        return {
            "indicator_id": self.indicator_id,
            "state": self.state.value,
            "confidence": self.confidence,
            "score": self.score,
            "analysis_id": self.analysis_id,
            "evidence_ids": list(self.evidence_ids),
            "evidence_hashes": list(self.evidence_hashes),
            "measured_at": self.measured_at.isoformat() if self.measured_at else None,
            "probe_ids": list(self.probe_ids),
            "tool_versions": list(self.tool_versions),
            "measurement_method_version": self.measurement_method_version,
        }


@dataclass(frozen=True, slots=True)
class DimensionResult:
    dimension_id: str
    score: float | None
    coverage: float
    status: str
    known_indicator_ids: tuple[str, ...]
    unknown_indicator_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "coverage": self.coverage,
            "status": self.status,
            "known_indicator_ids": list(self.known_indicator_ids),
            "unknown_indicator_ids": list(self.unknown_indicator_ids),
        }


@dataclass(frozen=True, slots=True)
class GateEvaluation:
    gate_id: str
    state: GateState
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"state": self.state.value, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class LegacyClearPlusInput:
    facets: Mapping[str, float]
    applicable_facet_weights: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class LegacyClearPlusResult:
    flat: float
    fixed_max: float
    paper_applicable: float
    default_denominator_policy: str = "fixed_max"

    def to_dict(self) -> dict[str, float | str]:
        return {
            "flat": self.flat,
            "fixed_max": self.fixed_max,
            "paper_applicable": self.paper_applicable,
            "default_denominator_policy": self.default_denominator_policy,
        }


@dataclass(frozen=True, slots=True)
class AssessmentResult:
    methodology_version: str
    overall_state: OverallState
    dimensions: Mapping[str, DimensionResult]
    gates: Mapping[str, GateEvaluation]
    legacy_clear_plus: LegacyClearPlusResult | None = None

    @property
    def triggered_gate_ids(self) -> tuple[str, ...]:
        return tuple(
            gate_id
            for gate_id, evaluation in self.gates.items()
            if evaluation.state is GateState.TRIGGERED
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "methodology_version": self.methodology_version,
            "overall_state": self.overall_state.value,
            "dimensions": {
                key: value.to_dict() for key, value in self.dimensions.items()
            },
            "gates": {key: value.to_dict() for key, value in self.gates.items()},
            "triggered_gate_ids": list(self.triggered_gate_ids),
            "legacy_clear_plus": (
                self.legacy_clear_plus.to_dict() if self.legacy_clear_plus else None
            ),
        }
