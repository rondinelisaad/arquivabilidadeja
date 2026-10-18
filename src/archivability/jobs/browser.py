from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from archivability.browser.models import BrowserBudgets, BrowserProfile
from archivability.jobs.models import AssessmentJobState, JobError
from archivability.lifecycle.models import (
    Analysis,
    AnalysisState,
    Attempt,
    AttemptState,
)
from archivability.storage.audit import AuditContext

Clock = Callable[[], datetime]
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_DEFAULT_BUDGETS = BrowserBudgets()
_DEFAULT_PROFILE = BrowserProfile()
_DEFAULT_AUDIT = AuditContext()


@dataclass(frozen=True, slots=True)
class BrowserJob:
    job_id: str
    analysis_id: str
    attempt_id: str
    state: AssessmentJobState
    attempt_count: int
    max_attempts: int
    assessment_max_attempts: int
    budgets: BrowserBudgets
    profile: BrowserProfile
    available_at: datetime
    created_at: datetime
    updated_at: datetime
    claimed_by: str | None = None
    lease_expires_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: str | None = None
    revision: int = 0

    def __post_init__(self) -> None:
        for field in ("job_id", "analysis_id", "attempt_id"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value or len(value) > 256:
                raise JobError(f"browser {field} is invalid")
        if not isinstance(self.state, AssessmentJobState):
            raise JobError("browser job state is invalid")
        if not isinstance(self.budgets, BrowserBudgets) or not isinstance(
            self.profile, BrowserProfile
        ):
            raise JobError("browser job contracts are invalid")
        for field in ("max_attempts", "assessment_max_attempts"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
                raise JobError(f"browser {field} must be between 1 and 10")
        if not 0 <= self.attempt_count <= self.max_attempts:
            raise JobError("browser attempt count is invalid")
        if not isinstance(self.revision, int) or self.revision < 0:
            raise JobError("browser revision is invalid")
        for field in (
            "available_at",
            "created_at",
            "updated_at",
            "lease_expires_at",
            "completed_at",
        ):
            value = getattr(self, field)
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise JobError("browser job timestamps must include a timezone")
        if self.available_at < self.created_at or self.updated_at < self.created_at:
            raise JobError("browser job timestamps are inconsistent")
        if self.error_code is not None and not re.fullmatch(
            r"[A-Z][A-Z0-9_]{0,63}", self.error_code
        ):
            raise JobError("browser error code is invalid")
        if self.state is AssessmentJobState.PENDING:
            valid = (
                self.claimed_by is None
                and self.lease_expires_at is None
                and self.completed_at is None
            )
        elif self.state is AssessmentJobState.RUNNING:
            valid = (
                self.claimed_by is not None
                and _WORKER_ID.fullmatch(self.claimed_by) is not None
                and self.lease_expires_at is not None
                and self.lease_expires_at > self.updated_at
                and self.completed_at is None
                and self.error_code is None
            )
        elif self.state is AssessmentJobState.SUCCEEDED:
            valid = (
                self.claimed_by is None
                and self.lease_expires_at is None
                and self.completed_at is not None
                and self.error_code is None
            )
        else:
            valid = (
                self.claimed_by is None
                and self.lease_expires_at is None
                and self.completed_at is not None
                and self.error_code is not None
                and self.attempt_count == self.max_attempts
            )
        if not valid:
            raise JobError("browser job state fields are inconsistent")

    @classmethod
    def create(
        cls,
        *,
        job_id: str,
        analysis_id: str,
        attempt_id: str,
        created_at: datetime,
        budgets: BrowserBudgets = _DEFAULT_BUDGETS,
        profile: BrowserProfile = _DEFAULT_PROFILE,
        max_attempts: int = 3,
        assessment_max_attempts: int = 3,
    ) -> BrowserJob:
        return cls(
            job_id=job_id,
            analysis_id=analysis_id,
            attempt_id=attempt_id,
            state=AssessmentJobState.PENDING,
            attempt_count=0,
            max_attempts=max_attempts,
            assessment_max_attempts=assessment_max_attempts,
            budgets=budgets,
            profile=profile,
            available_at=created_at,
            created_at=created_at,
            updated_at=created_at,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "analysis_id": self.analysis_id,
            "attempt_id": self.attempt_id,
            "state": self.state.value,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "assessment_max_attempts": self.assessment_max_attempts,
            "budgets": self.budgets.to_dict(),
            "profile": self.profile.to_dict(),
            "available_at": self.available_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "claimed_by": self.claimed_by,
            "lease_expires_at": (
                self.lease_expires_at.isoformat() if self.lease_expires_at else None
            ),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error_code": self.error_code,
            "revision": self.revision,
        }


class BrowserJobRepository(Protocol):
    def get_analysis(self, analysis_id: str) -> Analysis | None: ...
    def get_attempt(self, attempt_id: str) -> Attempt | None: ...
    def enqueue_browser_job(
        self, job: BrowserJob, *, audit: AuditContext
    ) -> tuple[BrowserJob, bool]: ...


@dataclass(frozen=True, slots=True)
class BrowserEnqueueOutcome:
    job: BrowserJob
    created: bool


class BrowserQueueService:
    def __init__(
        self,
        repository: BrowserJobRepository,
        *,
        clock: Clock = lambda: datetime.now(UTC),
        job_id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._job_id_factory = job_id_factory

    def enqueue(
        self,
        analysis_id: str,
        attempt_id: str,
        *,
        budgets: BrowserBudgets = _DEFAULT_BUDGETS,
        profile: BrowserProfile = _DEFAULT_PROFILE,
        max_attempts: int = 3,
        assessment_max_attempts: int = 3,
        audit: AuditContext = _DEFAULT_AUDIT,
    ) -> BrowserEnqueueOutcome:
        analysis = self._repository.get_analysis(analysis_id)
        attempt = self._repository.get_attempt(attempt_id)
        if analysis is None or analysis.state is not AnalysisState.RUNNING:
            raise JobError("browser queue requires a running analysis")
        if (
            attempt is None
            or attempt.analysis_id != analysis_id
            or attempt.attempt_id not in analysis.attempt_ids
            or attempt.state is not AttemptState.SUCCEEDED
        ):
            raise JobError("browser queue requires a registered successful HTTP attempt")
        job = BrowserJob.create(
            job_id=self._job_id_factory(),
            analysis_id=analysis_id,
            attempt_id=attempt_id,
            created_at=self._now(),
            budgets=budgets,
            profile=profile,
            max_attempts=max_attempts,
            assessment_max_attempts=assessment_max_attempts,
        )
        stored, created = self._repository.enqueue_browser_job(job, audit=audit)
        return BrowserEnqueueOutcome(stored, created)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise JobError("browser queue clock must include a timezone")
        return value


def claim_browser_job(
    job: BrowserJob,
    *,
    worker_id: str,
    occurred_at: datetime,
    lease_expires_at: datetime,
) -> BrowserJob:
    eligible = job.state is AssessmentJobState.PENDING and job.available_at <= occurred_at
    recoverable = (
        job.state is AssessmentJobState.RUNNING
        and job.lease_expires_at is not None
        and job.lease_expires_at <= occurred_at
    )
    if (
        not _WORKER_ID.fullmatch(worker_id)
        or not (eligible or recoverable)
        or job.attempt_count >= job.max_attempts
        or lease_expires_at <= occurred_at
    ):
        raise JobError("browser job is not claimable")
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


def succeed_browser_job(job: BrowserJob, *, occurred_at: datetime) -> BrowserJob:
    if job.state is not AssessmentJobState.RUNNING:
        raise JobError("only a running browser job can succeed")
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


def fail_browser_job(
    job: BrowserJob,
    *,
    occurred_at: datetime,
    error_code: str,
    retry_at: datetime,
) -> BrowserJob:
    if job.state is not AssessmentJobState.RUNNING:
        raise JobError("only a running browser job can fail")
    exhausted = job.attempt_count >= job.max_attempts
    return replace(
        job,
        state=(AssessmentJobState.FAILED if exhausted else AssessmentJobState.PENDING),
        available_at=occurred_at if exhausted else retry_at,
        updated_at=occurred_at,
        claimed_by=None,
        lease_expires_at=None,
        completed_at=occurred_at if exhausted else None,
        error_code=error_code,
        revision=job.revision + 1,
    )
