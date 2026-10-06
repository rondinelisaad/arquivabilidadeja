from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from archivability.jobs.models import AssessmentJob, AssessmentJobState, JobError


def _transition_time(job: AssessmentJob, occurred_at: datetime) -> None:
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise JobError("job transition timestamp must include a timezone")
    if occurred_at < job.updated_at:
        raise JobError("job transition timestamp cannot move backwards")


def claim_assessment_job(
    job: AssessmentJob,
    *,
    worker_id: str,
    occurred_at: datetime,
    lease_expires_at: datetime,
) -> AssessmentJob:
    _transition_time(job, occurred_at)
    if lease_expires_at.tzinfo is None or lease_expires_at.utcoffset() is None:
        raise JobError("job lease timestamp must include a timezone")
    if lease_expires_at <= occurred_at:
        raise JobError("job lease must expire after claim time")
    eligible = job.state is AssessmentJobState.PENDING and job.available_at <= occurred_at
    recoverable = (
        job.state is AssessmentJobState.RUNNING
        and job.lease_expires_at is not None
        and job.lease_expires_at <= occurred_at
    )
    if not eligible and not recoverable:
        raise JobError("job is not available for claim")
    if job.attempt_count >= job.max_attempts:
        raise JobError("job attempt limit has been reached")
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


def succeed_assessment_job(
    job: AssessmentJob, *, occurred_at: datetime
) -> AssessmentJob:
    _transition_time(job, occurred_at)
    if job.state is not AssessmentJobState.RUNNING:
        raise JobError("only a running job can succeed")
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


def fail_assessment_job(
    job: AssessmentJob,
    *,
    occurred_at: datetime,
    error_code: str,
    retry_at: datetime,
) -> AssessmentJob:
    _transition_time(job, occurred_at)
    if job.state is not AssessmentJobState.RUNNING:
        raise JobError("only a running job can fail")
    if retry_at.tzinfo is None or retry_at.utcoffset() is None:
        raise JobError("job retry timestamp must include a timezone")
    if retry_at < occurred_at:
        raise JobError("job retry cannot be scheduled in the past")
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
