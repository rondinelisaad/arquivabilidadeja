from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from archivability.evidence.http_derivation import (
    HttpMetadataDerivation,
    derive_http_metadata_indicators,
)
from archivability.evidence.models import Evidence, Observation
from archivability.lifecycle.models import (
    ANALYSIS_TERMINAL_STATES,
    Analysis,
    AnalysisState,
    Attempt,
    AttemptState,
    LifecycleError,
)
from archivability.lifecycle.state_machine import finalize_analysis
from archivability.methodology.models import IndicatorResult, MethodologyConfig
from archivability.storage.audit import AuditContext


Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class DerivationPersistenceResult:
    outcome: str
    evidence_count: int
    indicator_result_count: int

    def __post_init__(self) -> None:
        if self.outcome not in {"created", "replayed"}:
            raise LifecycleError("derivation persistence outcome is invalid")
        if self.evidence_count < 1 or self.indicator_result_count < 1:
            raise LifecycleError("derivation persistence counts must be positive")


class HttpAssessmentRepository(Protocol):
    def get_analysis(self, analysis_id: str) -> Analysis | None: ...

    def get_attempt(self, attempt_id: str) -> Attempt | None: ...

    def list_attempts(self, analysis_id: str) -> tuple[Attempt, ...]: ...

    def get_observation(self, observation_id: str) -> Observation | None: ...

    def complete_http_assessment(
        self,
        previous_analysis: Analysis,
        current_analysis: Analysis | None,
        evidence: tuple[Evidence, ...],
        indicator_results: tuple[IndicatorResult, ...],
        *,
        audit: AuditContext,
    ) -> DerivationPersistenceResult: ...


@dataclass(frozen=True, slots=True)
class HttpAssessmentOutcome:
    analysis: Analysis
    derivation: HttpMetadataDerivation
    persistence: DerivationPersistenceResult


class HttpMetadataAssessmentService:
    """Derives HTTP indicators and atomically closes their analysis."""

    def __init__(
        self,
        *,
        methodology: MethodologyConfig,
        repository: HttpAssessmentRepository,
        clock: Clock = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._methodology = methodology
        self._repository = repository
        self._clock = clock

    def assess(
        self,
        observation_id: str,
        *,
        audit: AuditContext = AuditContext(),
    ) -> HttpAssessmentOutcome:
        observation = self._repository.get_observation(observation_id)
        if observation is None:
            raise LifecycleError("HTTP assessment observation was not found")
        analysis = self._repository.get_analysis(observation.analysis_id)
        if analysis is None:
            raise LifecycleError("HTTP assessment analysis was not found")
        self._validate_methodology(analysis)
        if observation.subject_uri != analysis.subject_uri:
            raise LifecycleError("observation subject does not match the analysis")

        attempts = self._repository.list_attempts(analysis.analysis_id)
        source_attempt = self._repository.get_attempt(observation.attempt_id)
        if (
            source_attempt is None
            or source_attempt.analysis_id != analysis.analysis_id
            or source_attempt.attempt_id not in analysis.attempt_ids
            or source_attempt.state is not AttemptState.SUCCEEDED
        ):
            raise LifecycleError("derivation requires a registered successful attempt")

        target = self._terminal_state(attempts)
        derivation = derive_http_metadata_indicators(self._methodology, observation)
        if analysis.state is AnalysisState.RUNNING:
            completed = finalize_analysis(
                analysis,
                attempts,
                target=target,
                occurred_at=self._now(),
            )
        elif analysis.state in ANALYSIS_TERMINAL_STATES:
            if analysis.state is not target:
                raise LifecycleError("terminal analysis does not match its attempt outcomes")
            completed = None
        else:
            raise LifecycleError("HTTP assessment requires a running or completed analysis")

        persistence = self._repository.complete_http_assessment(
            analysis,
            completed,
            derivation.evidence,
            derivation.indicator_results,
            audit=audit,
        )
        return HttpAssessmentOutcome(
            analysis=completed or analysis,
            derivation=derivation,
            persistence=persistence,
        )

    def _validate_methodology(self, analysis: Analysis) -> None:
        if (
            analysis.methodology_id != self._methodology.id
            or analysis.methodology_version != self._methodology.version
        ):
            raise LifecycleError("analysis methodology does not match the assessment service")

    @staticmethod
    def _terminal_state(attempts: tuple[Attempt, ...]) -> AnalysisState:
        if not attempts or any(item.state is AttemptState.RUNNING for item in attempts):
            raise LifecycleError("all attempts must be terminal before HTTP assessment")
        states = {item.state for item in attempts}
        if AttemptState.CANCELLED in states:
            return AnalysisState.CANCELLED
        if states == {AttemptState.SUCCEEDED}:
            return AnalysisState.COMPLETED
        if AttemptState.SUCCEEDED in states and AttemptState.FAILED in states:
            return AnalysisState.PARTIALLY_COMPLETED
        raise LifecycleError("HTTP assessment requires at least one successful attempt")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise LifecycleError("assessment clock must return a timezone-aware datetime")
        return value
