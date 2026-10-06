from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from archivability.evidence.models import Observation
from archivability.jobs.service import EnqueueOutcome, HttpAssessmentQueueService
from archivability.lifecycle.models import (
    Analysis,
    AnalysisState,
    Attempt,
    AttemptState,
    LifecycleError,
)
from archivability.lifecycle.service import AnalysisOrchestrator
from archivability.methodology.models import MethodologyConfig
from archivability.probes.execution import ProbeExecutionOutcome, ProbeExecutionService
from archivability.probes.models import ProbeContext, ProbeRequest, ProbeValidationError
from archivability.probes.ports import Probe
from archivability.storage.audit import AuditContext


Clock = Callable[[], datetime]


class WorkflowError(ValueError):
    """Raised when an explicit HTTP assessment workflow request is invalid."""


@dataclass(frozen=True, slots=True)
class ProbeLimits:
    timeout_seconds: float = 10.0
    max_response_bytes: int = 5 * 1024 * 1024
    max_redirects: int = 3

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0.1 <= self.timeout_seconds <= 30
        ):
            raise WorkflowError("timeout_seconds must be between 0.1 and 30")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or not 1 <= self.max_response_bytes <= 10 * 1024 * 1024
        ):
            raise WorkflowError("max_response_bytes must be between 1 byte and 10 MiB")
        if (
            isinstance(self.max_redirects, bool)
            or not isinstance(self.max_redirects, int)
            or not 0 <= self.max_redirects <= 5
        ):
            raise WorkflowError("max_redirects must be between 0 and 5")


class HttpWorkflowRepository(Protocol):
    def get_analysis(self, analysis_id: str) -> Analysis | None: ...

    def list_attempts(self, analysis_id: str) -> tuple[Attempt, ...]: ...

    def record_http_workflow_event(
        self,
        *,
        analysis_id: str,
        attempt_id: str,
        job_id: str | None,
        result: str,
        status: str,
        failure_code: str | None,
        audit: AuditContext,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class HttpWorkflowOutcome:
    status: str
    analysis: Analysis
    execution: ProbeExecutionOutcome
    enqueue: EnqueueOutcome | None = None

    def __post_init__(self) -> None:
        if self.status not in {"assessment_queued", "retry_available", "failed"}:
            raise WorkflowError("HTTP workflow outcome status is invalid")
        if self.status == "assessment_queued":
            if (
                not self.execution.succeeded
                or self.enqueue is None
                or self.analysis.state is not AnalysisState.RUNNING
                or self.enqueue.job.analysis_id != self.analysis.analysis_id
            ):
                raise WorkflowError("queued workflow requires a successful probe and job")
        elif self.execution.succeeded or self.enqueue is not None:
            raise WorkflowError("failed workflow cannot expose an assessment job")
        if self.status == "failed" and self.analysis.state is not AnalysisState.FAILED:
            raise WorkflowError("failed workflow requires a failed analysis")
        if (
            self.status == "retry_available"
            and self.analysis.state is not AnalysisState.RUNNING
        ):
            raise WorkflowError("retryable workflow requires a running analysis")
        if self.execution.attempt.analysis_id != self.analysis.analysis_id:
            raise WorkflowError("workflow outcome identities do not match")


class _ValidatedHttpMetadataProbe:
    def __init__(self, delegate: Probe) -> None:
        self._delegate = delegate
        self.probe_id = delegate.probe_id
        self.tool_name = delegate.tool_name
        self.tool_version = delegate.tool_version

    def execute(self, context: ProbeContext) -> tuple[Observation, ...]:
        observations = self._delegate.execute(context)
        if not isinstance(observations, tuple) or len(observations) != 1:
            raise ProbeValidationError(
                "HTTP assessment probe must return exactly one observation"
            )
        observation = observations[0]
        if (
            observation.kind != "http_metadata"
            or observation.payload_schema_version != "1.1"
        ):
            raise ProbeValidationError(
                "HTTP assessment probe returned an incompatible observation"
            )
        return observations


class HttpAssessmentWorkflow:
    """Explicitly coordinates lifecycle, one HTTP probe, and durable enqueue."""

    def __init__(
        self,
        *,
        methodology: MethodologyConfig,
        repository: HttpWorkflowRepository,
        orchestrator: AnalysisOrchestrator,
        probe_execution: ProbeExecutionService,
        queue: HttpAssessmentQueueService,
        clock: Clock = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._methodology = methodology
        self._repository = repository
        self._orchestrator = orchestrator
        self._probe_execution = probe_execution
        self._queue = queue
        self._clock = clock

    def start(
        self,
        *,
        subject_uri: str,
        probe: Probe,
        analysis_max_attempts: int = 3,
        assessment_max_attempts: int = 3,
        limits: ProbeLimits = ProbeLimits(),
        audit: AuditContext = AuditContext(),
    ) -> HttpWorkflowOutcome:
        self._validate_request(probe, assessment_max_attempts, limits)
        analysis = self._orchestrator.create_analysis(
            subject_uri=subject_uri,
            methodology=self._methodology,
            max_attempts=analysis_max_attempts,
            audit=audit,
        )
        return self._execute(
            analysis,
            probe,
            assessment_max_attempts=assessment_max_attempts,
            limits=limits,
            audit=audit,
        )

    def retry(
        self,
        analysis_id: str,
        *,
        probe: Probe,
        assessment_max_attempts: int = 3,
        limits: ProbeLimits = ProbeLimits(),
        audit: AuditContext = AuditContext(),
    ) -> HttpWorkflowOutcome:
        self._validate_request(probe, assessment_max_attempts, limits)
        analysis = self._repository.get_analysis(analysis_id)
        if analysis is None:
            raise WorkflowError("HTTP workflow analysis was not found")
        if analysis.state is not AnalysisState.RUNNING:
            raise WorkflowError("only a running analysis can be retried")
        if (
            analysis.methodology_id != self._methodology.id
            or analysis.methodology_version != self._methodology.version
        ):
            raise WorkflowError("analysis methodology does not match the workflow")
        attempts = self._repository.list_attempts(analysis_id)
        if not attempts or attempts[-1].state is not AttemptState.FAILED:
            raise WorkflowError("retry requires the latest attempt to have failed")
        if any(item.state is AttemptState.SUCCEEDED for item in attempts):
            raise WorkflowError("analysis with a successful attempt cannot be retried")
        return self._execute(
            analysis,
            probe,
            assessment_max_attempts=assessment_max_attempts,
            limits=limits,
            audit=audit,
        )

    def _execute(
        self,
        analysis: Analysis,
        probe: Probe,
        *,
        assessment_max_attempts: int,
        limits: ProbeLimits,
        audit: AuditContext,
    ) -> HttpWorkflowOutcome:
        attempt = self._orchestrator.start_attempt(analysis.analysis_id, audit=audit)
        request = ProbeRequest(
            analysis_id=analysis.analysis_id,
            attempt_id=attempt.attempt_id,
            subject_uri=analysis.subject_uri,
            requested_at=self._now(),
            timeout_seconds=limits.timeout_seconds,
            max_response_bytes=limits.max_response_bytes,
            max_redirects=limits.max_redirects,
        )
        execution = self._probe_execution.execute(
            request,
            _ValidatedHttpMetadataProbe(probe),
            audit=audit,
        )
        if not execution.succeeded:
            return self._failed_execution(analysis.analysis_id, execution, audit)

        try:
            enqueue = self._queue.enqueue(
                execution.observations[0].observation_id,
                max_attempts=assessment_max_attempts,
                audit=audit,
            )
        except Exception:
            self._repository.record_http_workflow_event(
                analysis_id=analysis.analysis_id,
                attempt_id=execution.attempt.attempt_id,
                job_id=None,
                result="failure",
                status="queue_failed",
                failure_code="QUEUE_FAILED",
                audit=audit,
            )
            raise

        current = self._require_analysis(analysis.analysis_id)
        self._repository.record_http_workflow_event(
            analysis_id=current.analysis_id,
            attempt_id=execution.attempt.attempt_id,
            job_id=enqueue.job.job_id,
            result="success",
            status="assessment_queued",
            failure_code=None,
            audit=audit,
        )
        return HttpWorkflowOutcome(
            status="assessment_queued",
            analysis=current,
            execution=execution,
            enqueue=enqueue,
        )

    def _failed_execution(
        self,
        analysis_id: str,
        execution: ProbeExecutionOutcome,
        audit: AuditContext,
    ) -> HttpWorkflowOutcome:
        current = self._require_analysis(analysis_id)
        attempts = self._repository.list_attempts(analysis_id)
        exhausted = len(attempts) >= current.max_attempts
        all_failed = all(item.state is AttemptState.FAILED for item in attempts)
        if exhausted and all_failed:
            current = self._orchestrator.finalize_analysis(
                analysis_id,
                target=AnalysisState.FAILED,
                audit=audit,
            )
            status = "failed"
        else:
            status = "retry_available"
        self._repository.record_http_workflow_event(
            analysis_id=analysis_id,
            attempt_id=execution.attempt.attempt_id,
            job_id=None,
            result="failure",
            status=status,
            failure_code=execution.failure_code,
            audit=audit,
        )
        return HttpWorkflowOutcome(
            status=status,
            analysis=current,
            execution=execution,
        )

    @staticmethod
    def _validate_request(
        probe: Probe,
        assessment_max_attempts: int,
        limits: ProbeLimits,
    ) -> None:
        if not isinstance(limits, ProbeLimits):
            raise WorkflowError("limits must be a ProbeLimits value")
        if getattr(probe, "probe_id", None) != "http-metadata":
            raise WorkflowError("HTTP workflow requires the http-metadata probe")
        for field in ("tool_name", "tool_version"):
            value = getattr(probe, field, None)
            if not isinstance(value, str) or not value or len(value) > 128:
                raise WorkflowError(f"probe {field} must contain between 1 and 128 characters")
        if (
            isinstance(assessment_max_attempts, bool)
            or not isinstance(assessment_max_attempts, int)
            or not 1 <= assessment_max_attempts <= 10
        ):
            raise WorkflowError("assessment_max_attempts must be between 1 and 10")

    def _require_analysis(self, analysis_id: str) -> Analysis:
        value = self._repository.get_analysis(analysis_id)
        if value is None:
            raise LifecycleError("workflow analysis disappeared")
        return value

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise WorkflowError("workflow clock must return a timezone-aware datetime")
        return value
