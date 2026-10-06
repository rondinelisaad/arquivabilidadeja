from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from archivability.lifecycle.models import (
    Analysis,
    AnalysisState,
    Attempt,
    AttemptState,
    LifecycleError,
)
from archivability.lifecycle.ports import LifecycleRepository
from archivability.lifecycle.state_machine import (
    finalize_analysis,
    finish_attempt,
    start_attempt,
)
from archivability.methodology.models import MethodologyConfig
from archivability.storage.audit import AuditContext


class AnalysisOrchestrator:
    """Coordinates lifecycle state only; it performs no network collection."""

    def __init__(
        self,
        repository: LifecycleRepository,
        *,
        clock: Callable[[], datetime] | None = None,
        analysis_id_factory: Callable[[], str] | None = None,
        attempt_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._analysis_id_factory = analysis_id_factory or (lambda: str(uuid4()))
        self._attempt_id_factory = attempt_id_factory or (lambda: str(uuid4()))

    def create_analysis(
        self,
        *,
        subject_uri: str,
        methodology: MethodologyConfig,
        max_attempts: int = 3,
        owner_user_id: str | None = None,
        audit: AuditContext = AuditContext(),
    ) -> Analysis:
        now = self._now()
        analysis = Analysis.create(
            analysis_id=self._analysis_id_factory(),
            subject_uri=subject_uri,
            methodology_id=methodology.id,
            methodology_version=methodology.version,
            created_at=now,
            max_attempts=max_attempts,
        )
        self._repository.add_analysis(
            analysis,
            owner_user_id=owner_user_id,
            audit=audit,
        )
        return analysis

    def start_attempt(
        self, analysis_id: str, *, audit: AuditContext = AuditContext()
    ) -> Attempt:
        analysis = self._require_analysis(analysis_id)
        if any(
            item.state is AttemptState.RUNNING
            for item in self._repository.list_attempts(analysis_id)
        ):
            raise LifecycleError("analysis already has a running attempt")
        updated, attempt = start_attempt(
            analysis,
            attempt_id=self._attempt_id_factory(),
            occurred_at=self._now(),
        )
        self._repository.add_attempt(analysis, updated, attempt, audit=audit)
        return attempt

    def finish_attempt(
        self,
        analysis_id: str,
        attempt_id: str,
        *,
        target: AttemptState,
        failure_code: str | None = None,
        audit: AuditContext = AuditContext(),
    ) -> Attempt:
        analysis = self._require_analysis(analysis_id)
        if analysis.state is not AnalysisState.RUNNING:
            raise LifecycleError("attempts can finish only while analysis is running")
        attempt = self._require_attempt(attempt_id)
        if attempt.analysis_id != analysis_id or attempt_id not in analysis.attempt_ids:
            raise LifecycleError("attempt is not registered by the analysis")
        updated = finish_attempt(
            attempt,
            target=target,
            occurred_at=self._now(),
            failure_code=failure_code,
        )
        self._repository.update_attempt(attempt, updated, audit=audit)
        return updated

    def finalize_analysis(
        self,
        analysis_id: str,
        *,
        target: AnalysisState,
        audit: AuditContext = AuditContext(),
    ) -> Analysis:
        analysis = self._require_analysis(analysis_id)
        attempts = self._repository.list_attempts(analysis_id)
        updated = finalize_analysis(
            analysis,
            attempts,
            target=target,
            occurred_at=self._now(),
        )
        self._repository.update_analysis(analysis, updated, audit=audit)
        return updated

    def _require_analysis(self, analysis_id: str) -> Analysis:
        value = self._repository.get_analysis(analysis_id)
        if value is None:
            raise LifecycleError("analysis was not found")
        return value

    def _require_attempt(self, attempt_id: str) -> Attempt:
        value = self._repository.get_attempt(attempt_id)
        if value is None:
            raise LifecycleError("attempt was not found")
        return value

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise LifecycleError("orchestrator clock must return a timezone-aware datetime")
        return value
