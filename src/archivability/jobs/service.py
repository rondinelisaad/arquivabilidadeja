from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import uuid4

from archivability.evidence.assessment import HttpAssessmentOutcome
from archivability.jobs.models import AssessmentJob, AssessmentJobState, JobError
from archivability.jobs.ports import AssessmentJobRepository
from archivability.jobs.state_machine import fail_assessment_job, succeed_assessment_job
from archivability.lifecycle.models import AttemptState
from archivability.storage.audit import AuditContext


Clock = Callable[[], datetime]


class HttpAssessor(Protocol):
    def assess(
        self, observation_id: str, *, audit: AuditContext
    ) -> HttpAssessmentOutcome: ...


@dataclass(frozen=True, slots=True)
class EnqueueOutcome:
    job: AssessmentJob
    created: bool


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    status: str
    job: AssessmentJob | None
    assessment: HttpAssessmentOutcome | None = None

    def __post_init__(self) -> None:
        if self.status not in {"idle", "succeeded", "retry_scheduled", "failed"}:
            raise JobError("worker outcome status is invalid")
        if self.status == "idle" and (self.job is not None or self.assessment is not None):
            raise JobError("idle worker outcome cannot contain a job")
        if self.status != "idle" and self.job is None:
            raise JobError("non-idle worker outcome requires a job")
        if self.status == "succeeded" and self.assessment is None:
            raise JobError("successful worker outcome requires an assessment")


class HttpAssessmentQueueService:
    def __init__(
        self,
        *,
        repository: AssessmentJobRepository,
        clock: Clock = lambda: datetime.now(timezone.utc),
        job_id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._job_id_factory = job_id_factory

    def enqueue(
        self,
        observation_id: str,
        *,
        max_attempts: int = 3,
        audit: AuditContext = AuditContext(),
    ) -> EnqueueOutcome:
        observation = self._repository.get_observation(observation_id)
        if observation is None:
            raise JobError("queued observation was not found")
        analysis = self._repository.get_analysis(observation.analysis_id)
        attempt = self._repository.get_attempt(observation.attempt_id)
        if analysis is None or attempt is None:
            raise JobError("queued observation has no lifecycle state")
        if (
            attempt.analysis_id != analysis.analysis_id
            or attempt.attempt_id not in analysis.attempt_ids
            or attempt.state is not AttemptState.SUCCEEDED
        ):
            raise JobError("only an observation from a successful attempt can be queued")
        job = AssessmentJob.create(
            job_id=self._job_id_factory(),
            analysis_id=analysis.analysis_id,
            observation_id=observation.observation_id,
            created_at=self._now(),
            max_attempts=max_attempts,
        )
        stored, created = self._repository.enqueue_assessment_job(job, audit=audit)
        return EnqueueOutcome(job=stored, created=created)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise JobError("queue clock must return a timezone-aware datetime")
        return value


class HttpAssessmentWorker:
    def __init__(
        self,
        *,
        worker_id: str,
        repository: AssessmentJobRepository,
        assessor: HttpAssessor,
        clock: Clock = lambda: datetime.now(timezone.utc),
        lease_duration: timedelta = timedelta(minutes=5),
        retry_base_delay: timedelta = timedelta(seconds=30),
    ) -> None:
        if not worker_id or len(worker_id) > 128:
            raise JobError("worker_id must be between 1 and 128 characters")
        if not timedelta(seconds=1) <= lease_duration <= timedelta(minutes=15):
            raise JobError("lease duration must be between 1 second and 15 minutes")
        if not timedelta(seconds=1) <= retry_base_delay <= timedelta(minutes=15):
            raise JobError("retry delay must be between 1 second and 15 minutes")
        self._worker_id = worker_id
        self._repository = repository
        self._assessor = assessor
        self._clock = clock
        self._lease_duration = lease_duration
        self._retry_base_delay = retry_base_delay

    def run_once(self, *, audit: AuditContext = AuditContext()) -> WorkerOutcome:
        claimed_at = self._now()
        job = self._repository.claim_next_assessment_job(
            worker_id=self._worker_id,
            occurred_at=claimed_at,
            lease_expires_at=claimed_at + self._lease_duration,
            audit=audit,
        )
        if job is None:
            return WorkerOutcome(status="idle", job=None)
        try:
            assessment = self._assessor.assess(job.observation_id, audit=audit)
        except Exception:
            return self._handle_failure(job, audit)

        completed = succeed_assessment_job(job, occurred_at=self._now())
        self._repository.update_assessment_job(
            job,
            completed,
            action="job.http_assessment_queue_succeeded",
            audit=audit,
        )
        return WorkerOutcome(status="succeeded", job=completed, assessment=assessment)

    def _handle_failure(
        self, job: AssessmentJob, audit: AuditContext
    ) -> WorkerOutcome:
        occurred_at = self._now()
        delay = self._retry_base_delay * (2 ** (job.attempt_count - 1))
        retry_at = occurred_at + min(delay, timedelta(hours=1))
        updated = fail_assessment_job(
            job,
            occurred_at=occurred_at,
            error_code="ASSESSMENT_FAILED",
            retry_at=retry_at,
        )
        action = (
            "job.http_assessment_queue_failed"
            if updated.state is AssessmentJobState.FAILED
            else "job.http_assessment_queue_retry"
        )
        self._repository.update_assessment_job(
            job,
            updated,
            action=action,
            audit=audit,
        )
        status = "failed" if updated.state is AssessmentJobState.FAILED else "retry_scheduled"
        return WorkerOutcome(status=status, job=updated)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise JobError("worker clock must return a timezone-aware datetime")
        return value
