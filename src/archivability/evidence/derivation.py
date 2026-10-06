from __future__ import annotations

from collections.abc import Sequence

from archivability.evidence.models import Evidence, EvidenceValidationError
from archivability.methodology.models import (
    IndicatorResult,
    MethodologyConfig,
    ResultState,
    ScoringInputError,
)


def derive_indicator_result(
    methodology: MethodologyConfig,
    *,
    indicator_id: str,
    state: ResultState,
    evidence: Sequence[Evidence],
    score: float | None = None,
    confidence: float | None = None,
) -> IndicatorResult:
    """Derive a scored result while preserving its complete evidence provenance."""
    definition = methodology.indicators.get(indicator_id)
    if definition is None:
        raise ScoringInputError(f"unknown indicator result: {indicator_id}")
    if not evidence:
        raise EvidenceValidationError("an indicator result requires evidence")
    if any(item.indicator_id != indicator_id for item in evidence):
        raise EvidenceValidationError("all evidence must target the requested indicator")
    analysis_ids = {item.analysis_id for item in evidence}
    if len(analysis_ids) != 1:
        raise EvidenceValidationError("all evidence must belong to one analysis")
    if len({item.evidence_id for item in evidence}) != len(evidence):
        raise EvidenceValidationError("evidence entries must be unique")
    if not definition.score_enabled and state is not ResultState.CONTEXT:
        raise ScoringInputError(f"unscored indicator {indicator_id} must use context state")
    if definition.score_enabled and state is ResultState.CONTEXT:
        raise ScoringInputError(f"scored indicator {indicator_id} cannot use context state")

    maximum_confidence = min(item.confidence for item in evidence)
    resolved_confidence = maximum_confidence if confidence is None else confidence
    if resolved_confidence > maximum_confidence:
        raise EvidenceValidationError(
            "result confidence cannot exceed its least-confident evidence"
        )

    sources = [source for item in evidence for source in item.sources]
    return IndicatorResult(
        indicator_id=indicator_id,
        state=state,
        confidence=resolved_confidence,
        score=score,
        analysis_id=next(iter(analysis_ids)),
        evidence_ids=tuple(item.evidence_id for item in evidence),
        evidence_hashes=tuple(item.content_hash for item in evidence),
        measured_at=max(source.observed_at for source in sources),
        probe_ids=tuple(dict.fromkeys(source.probe_id for source in sources)),
        tool_versions=tuple(
            dict.fromkeys(f"{source.tool_name}@{source.tool_version}" for source in sources)
        ),
        measurement_method_version=";".join(
            dict.fromkeys(f"{item.method_id}@{item.method_version}" for item in evidence)
        ),
    )
