from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import uuid4

from archivability.evidence.models import Observation
from archivability.jobs.models import AssessmentJobState, JobError
from archivability.lifecycle.models import Analysis, AnalysisState, AttemptState
from archivability.lifecycle.service import AnalysisOrchestrator
from archivability.probes.execution import ProbeExecutionService
from archivability.probes.models import ProbeRequest
from archivability.probes.ports import Probe
from archivability.storage.audit import AuditContext

Clock = Callable[[], datetime]
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class CollectionLimits:
    timeout_seconds: float = 10.0
    max_response_bytes: int = 5 * 1024 * 1024
    max_redirects: int = 3

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0.1 <= self.timeout_seconds <= 30
        ):
            raise JobError("collection timeout must be between 0.1 and 30 seconds")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or not 1 <= self.max_response_bytes <= 10 * 1024 * 1024
        ):
            raise JobError("collection response limit must be between 1 byte and 10 MiB")
        if (
            isinstance(self.max_redirects, bool)
            or not isinstance(self.max_redirects, int)
            or not 0 <= self.max_redirects <= 5
        ):
            raise JobError("collection redirect limit must be between 0 and 5")

    def to_dict(self) -> dict[str, int | float]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_response_bytes": self.max_response_bytes,
            "max_redirects": self.max_redirects,
        }


@dataclass(frozen=True, slots=True)
class CollectionJob:
    job_id: str
    analysis_id: str
    state: AssessmentJobState
    attempt_count: int
    max_attempts: int
    assessment_max_attempts: int
    limits: CollectionLimits
    available_at: datetime
    created_at: datetime
    updated_at: datetime
    claimed_by: str | None = None
    lease_expires_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: str | None = None
    revision: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not self.job_id or len(self.job_id) > 256:
            raise JobError("collection job ID is invalid")
        if (
            not isinstance(self.analysis_id, str)
            or not self.analysis_id
            or len(self.analysis_id) > 256
        ):
            raise JobError("collection analysis ID is invalid")
        if not isinstance(self.state, AssessmentJobState):
            raise JobError("collection job state is invalid")
        if not isinstance(self.limits, CollectionLimits):
            raise JobError("collection limits are invalid")
        if not isinstance(self.max_attempts, int) or not 1 <= self.max_attempts <= 10:
            raise JobError("collection max attempts must be between 1 and 10")
        if (
            not isinstance(self.assessment_max_attempts, int)
            or not 1 <= self.assessment_max_attempts <= 10
        ):
            raise JobError("assessment max attempts must be between 1 and 10")
        if not isinstance(self.attempt_count, int) or not 0 <= self.attempt_count <= self.max_attempts:
            raise JobError("collection attempt count is invalid")
        if not isinstance(self.revision, int) or self.revision < 0:
            raise JobError("collection revision is invalid")
        for value in (
            self.available_at,
            self.created_at,
            self.updated_at,
            self.lease_expires_at,
            self.completed_at,
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise JobError("collection timestamps must include a timezone")
        if self.available_at < self.created_at or self.updated_at < self.created_at:
            raise JobError("collection timestamps are inconsistent")
        if self.error_code is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", self.error_code):
            raise JobError("collection error code is invalid")
        if self.state is AssessmentJobState.PENDING:
            if self.claimed_by is not None or self.lease_expires_at is not None or self.completed_at is not None:
                raise JobError("pending collection job has terminal fields")
        elif self.state is AssessmentJobState.RUNNING:
            if (
                self.claimed_by is None
                or not _WORKER_ID.fullmatch(self.claimed_by)
                or self.lease_expires_at is None
                or self.lease_expires_at <= self.updated_at
                or self.completed_at is not None
                or self.error_code is not None
            ):
                raise JobError("running collection job is inconsistent")
        elif self.state is AssessmentJobState.SUCCEEDED:
            if self.claimed_by is not None or self.lease_expires_at is not None or self.completed_at is None or self.error_code is not None:
                raise JobError("successful collection job is inconsistent")
        elif self.state is AssessmentJobState.FAILED:
            if (
                self.claimed_by is not None
                or self.lease_expires_at is not None
                or self.completed_at is None
                or self.error_code is None
                or self.attempt_count != self.max_attempts
            ):
                raise JobError("failed collection job is inconsistent")

    @classmethod
    def create(
        cls,
        *,
        job_id: str,
        analysis_id: str,
        limits: CollectionLimits,
        created_at: datetime,
        max_attempts: int,
        assessment_max_attempts: int,
    ) -> CollectionJob:
        return cls(
            job_id=job_id,
            analysis_id=analysis_id,
            state=AssessmentJobState.PENDING,
            attempt_count=0,
            max_attempts=max_attempts,
            assessment_max_attempts=assessment_max_attempts,
            limits=limits,
            available_at=created_at,
            created_at=created_at,
            updated_at=created_at,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "analysis_id": self.analysis_id,
            "state": self.state.value,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "assessment_max_attempts": self.assessment_max_attempts,
            "limits": self.limits.to_dict(),
            "available_at": self.available_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "claimed_by": self.claimed_by,
            "lease_expires_at": self.lease_expires_at.isoformat() if self.lease_expires_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error_code": self.error_code,
            "revision": self.revision,
        }


class CollectionJobRepository(Protocol):
    def get_analysis(self, analysis_id: str) -> Analysis | None: ...

    def list_attempts(self, analysis_id: str): ...

    def get_latest_successful_http_observation(
        self, analysis_id: str
    ) -> Observation | None: ...

    def enqueue_collection_job(
        self, job: CollectionJob, *, audit: AuditContext
    ) -> tuple[CollectionJob, bool]: ...

    def add_analysis_with_collection_job(
        self,
        analysis: Analysis,
        job: CollectionJob,
        *,
        owner_user_id: str | None,
        audit: AuditContext,
    ) -> None: ...

    def claim_next_collection_job(
        self,
        *,
        worker_id: str,
        occurred_at: datetime,
        lease_expires_at: datetime,
        audit: AuditContext,
    ) -> CollectionJob | None: ...

    def update_collection_job(
        self,
        previous: CollectionJob,
        current: CollectionJob,
        *,
        action: str,
        audit: AuditContext,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class CollectionEnqueueOutcome:
    job: CollectionJob
    created: bool


@dataclass(frozen=True, slots=True)
class CollectionWorkerOutcome:
    status: str
    job: CollectionJob | None

    def __post_init__(self) -> None:
        if self.status not in {"idle", "succeeded", "retry_scheduled", "failed"}:
            raise JobError("collection worker status is invalid")
        if (self.status == "idle") != (self.job is None):
            raise JobError("collection worker outcome is inconsistent")


class HttpCollectionQueueService:
    def __init__(
        self,
        repository: CollectionJobRepository,
        *,
        clock: Clock = lambda: datetime.now(timezone.utc),
        job_id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._job_id_factory = job_id_factory

    def enqueue(
        self,
        analysis_id: str,
        *,
        limits: CollectionLimits = CollectionLimits(),
        max_attempts: int = 3,
        assessment_max_attempts: int = 3,
        audit: AuditContext = AuditContext(),
    ) -> CollectionEnqueueOutcome:
        analysis = self._repository.get_analysis(analysis_id)
        if analysis is None or analysis.state is not AnalysisState.REQUESTED:
            raise JobError("only a requested analysis can be queued for collection")
        job = CollectionJob.create(
            job_id=self._job_id_factory(),
            analysis_id=analysis_id,
            limits=limits,
            created_at=self._now(),
            max_attempts=max_attempts,
            assessment_max_attempts=assessment_max_attempts,
        )
        stored, created = self._repository.enqueue_collection_job(job, audit=audit)
        return CollectionEnqueueOutcome(stored, created)

    def enqueue_new_analysis(
        self,
        analysis: Analysis,
        *,
        owner_user_id: str | None = None,
        limits: CollectionLimits = CollectionLimits(),
        max_attempts: int = 3,
        assessment_max_attempts: int = 3,
        audit: AuditContext = AuditContext(),
    ) -> CollectionEnqueueOutcome:
        if analysis.state is not AnalysisState.REQUESTED:
            raise JobError("only a requested analysis can be initially queued")
        job = CollectionJob.create(
            job_id=self._job_id_factory(),
            analysis_id=analysis.analysis_id,
            limits=limits,
            created_at=self._now(),
            max_attempts=max_attempts,
            assessment_max_attempts=assessment_max_attempts,
        )
        self._repository.add_analysis_with_collection_job(
            analysis,
            job,
            owner_user_id=owner_user_id,
            audit=audit,
        )
        return CollectionEnqueueOutcome(job, True)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise JobError("collection queue clock must include a timezone")
        return value


class AssessmentEnqueuer(Protocol):
    def enqueue(self, observation_id: str, *, max_attempts: int, audit: AuditContext): ...


class HttpCollectionWorker:
    def __init__(
        self,
        *,
        worker_id: str,
        repository: CollectionJobRepository,
        orchestrator: AnalysisOrchestrator,
        execution: ProbeExecutionService,
        probe: Probe,
        assessment_queue: AssessmentEnqueuer,
        clock: Clock = lambda: datetime.now(timezone.utc),
        lease_duration: timedelta = timedelta(minutes=5),
        retry_base_delay: timedelta = timedelta(seconds=30),
    ) -> None:
        if not isinstance(worker_id, str) or not _WORKER_ID.fullmatch(worker_id):
            raise JobError("collection worker ID is invalid")
        self._worker_id = worker_id
        self._repository = repository
        self._orchestrator = orchestrator
        self._execution = execution
        self._probe = probe
        self._assessment_queue = assessment_queue
        self._clock = clock
        self._lease_duration = lease_duration
        self._retry_base_delay = retry_base_delay

    def run_once(self, *, audit: AuditContext = AuditContext()) -> CollectionWorkerOutcome:
        claimed_at = self._now()
        job = self._repository.claim_next_collection_job(
            worker_id=self._worker_id,
            occurred_at=claimed_at,
            lease_expires_at=claimed_at + self._lease_duration,
            audit=audit,
        )
        if job is None:
            return CollectionWorkerOutcome("idle", None)
        analysis = self._repository.get_analysis(job.analysis_id)
        if analysis is None or analysis.state not in {AnalysisState.REQUESTED, AnalysisState.RUNNING}:
            return self._fail(job, audit, "COLLECTION_STATE_INVALID")
        try:
            persisted = self._repository.get_latest_successful_http_observation(
                job.analysis_id
            )
            if persisted is not None:
                self._assessment_queue.enqueue(
                    persisted.observation_id,
                    max_attempts=job.assessment_max_attempts,
                    audit=audit,
                )
                return self._succeed(job, audit)

            # An expired collection lease can leave its lifecycle attempt running.
            # Close it before retrying so the analysis state machine remains valid.
            for stale_attempt in self._repository.list_attempts(job.analysis_id):
                if stale_attempt.state is AttemptState.RUNNING:
                    self._orchestrator.finish_attempt(
                        job.analysis_id,
                        stale_attempt.attempt_id,
                        target=AttemptState.FAILED,
                        failure_code="COLLECTION_LEASE_EXPIRED",
                        audit=audit,
                    )
            attempt = self._orchestrator.start_attempt(job.analysis_id, audit=audit)
            limits = job.limits
            outcome = self._execution.execute(
                ProbeRequest(
                    analysis_id=analysis.analysis_id,
                    attempt_id=attempt.attempt_id,
                    subject_uri=analysis.subject_uri,
                    requested_at=self._now(),
                    timeout_seconds=limits.timeout_seconds,
                    max_response_bytes=limits.max_response_bytes,
                    max_redirects=limits.max_redirects,
                ),
                self._probe,
                audit=audit,
            )
            if not outcome.succeeded:
                return self._fail(job, audit, outcome.failure_code or "COLLECTION_FAILED")
            self._assessment_queue.enqueue(
                outcome.observations[0].observation_id,
                max_attempts=job.assessment_max_attempts,
                audit=audit,
            )
        except Exception:
            return self._fail(job, audit, "COLLECTION_FAILED")
        return self._succeed(job, audit)

    def _succeed(
        self, job: CollectionJob, audit: AuditContext
    ) -> CollectionWorkerOutcome:
        succeeded = _succeed(job, self._now())
        self._repository.update_collection_job(
            job,
            succeeded,
            action="job.http_collection_queue_succeeded",
            audit=audit,
        )
        return CollectionWorkerOutcome("succeeded", succeeded)

    def _fail(
        self, job: CollectionJob, audit: AuditContext, error_code: str
    ) -> CollectionWorkerOutcome:
        now = self._now()
        delay = self._retry_base_delay * (2 ** max(0, job.attempt_count - 1))
        updated = _fail(job, now, error_code, now + min(delay, timedelta(hours=1)))
        terminal = updated.state is AssessmentJobState.FAILED
        self._repository.update_collection_job(
            job,
            updated,
            action=(
                "job.http_collection_queue_failed"
                if terminal
                else "job.http_collection_queue_retry"
            ),
            audit=audit,
        )
        if terminal:
            analysis = self._repository.get_analysis(job.analysis_id)
            attempts = self._repository.list_attempts(job.analysis_id)
            if (
                analysis is not None
                and analysis.state is AnalysisState.RUNNING
                and attempts
                and all(item.state is AttemptState.FAILED for item in attempts)
            ):
                self._orchestrator.finalize_analysis(
                    job.analysis_id,
                    target=AnalysisState.FAILED,
                    audit=audit,
                )
        return CollectionWorkerOutcome("failed" if terminal else "retry_scheduled", updated)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise JobError("collection worker clock must include a timezone")
        return value


def _claim(
    job: CollectionJob, worker_id: str, occurred_at: datetime, lease_expires_at: datetime
) -> CollectionJob:
    eligible = job.state is AssessmentJobState.PENDING and job.available_at <= occurred_at
    recoverable = (
        job.state is AssessmentJobState.RUNNING
        and job.lease_expires_at is not None
        and job.lease_expires_at <= occurred_at
    )
    if not (eligible or recoverable) or job.attempt_count >= job.max_attempts:
        raise JobError("collection job is not claimable")
    return replace(
        job,
        state=AssessmentJobState.RUNNING,
        attempt_count=job.attempt_count + 1,
        updated_at=occurred_at,
        claimed_by=worker_id,
        lease_expires_at=lease_expires_at,
        completed_at=None,
        error_code=None,
        revision=job.revision + 1,
    )


def _succeed(job: CollectionJob, occurred_at: datetime) -> CollectionJob:
    if job.state is not AssessmentJobState.RUNNING:
        raise JobError("only a running collection job can succeed")
    return replace(
        job,
        state=AssessmentJobState.SUCCEEDED,
        updated_at=occurred_at,
        claimed_by=None,
        lease_expires_at=None,
        completed_at=occurred_at,
        error_code=None,
        revision=job.revision + 1,
    )


def _fail(
    job: CollectionJob,
    occurred_at: datetime,
    error_code: str,
    retry_at: datetime,
) -> CollectionJob:
    if job.state is not AssessmentJobState.RUNNING:
        raise JobError("only a running collection job can fail")
    exhausted = job.attempt_count >= job.max_attempts
    return replace(
        job,
        state=AssessmentJobState.FAILED if exhausted else AssessmentJobState.PENDING,
        available_at=occurred_at if exhausted else retry_at,
        updated_at=occurred_at,
        claimed_by=None,
        lease_expires_at=None,
        completed_at=occurred_at if exhausted else None,
        error_code=error_code,
        revision=job.revision + 1,
    )


claim_collection_job = _claim
