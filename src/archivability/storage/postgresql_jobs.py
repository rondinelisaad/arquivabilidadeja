from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from archivability.jobs.models import AssessmentJob, AssessmentJobState, JobError
from archivability.jobs.state_machine import claim_assessment_job
from archivability.storage.audit import AuditContext
from archivability.storage.errors import (
    ConcurrencyConflict,
    IntegrityViolation,
    PersistenceError,
)
from archivability.storage.postgresql_evidence import PostgreSqlEvidenceRepository


_UPDATE_ACTIONS = frozenset(
    {
        "job.http_assessment_queue_succeeded",
        "job.http_assessment_queue_retry",
        "job.http_assessment_queue_failed",
    }
)
_ACTION_STATES = {
    "job.http_assessment_queue_succeeded": AssessmentJobState.SUCCEEDED,
    "job.http_assessment_queue_retry": AssessmentJobState.PENDING,
    "job.http_assessment_queue_failed": AssessmentJobState.FAILED,
}
_JOB_COLUMNS = """
    document_json, analysis_id, observation_id, state, revision,
    attempt_count, max_attempts, available_at, claimed_by,
    lease_expires_at, completed_at, error_code, created_at, updated_at
"""


class PostgreSqlAssessmentJobRepository(PostgreSqlEvidenceRepository):
    """PostgreSQL assessment queue using row locks, leases, and revisions."""

    def enqueue_assessment_job(
        self,
        job: AssessmentJob,
        *,
        audit: AuditContext = AuditContext(),
    ) -> tuple[AssessmentJob, bool]:
        resource_id = job.job_id
        try:
            if job.state is not AssessmentJobState.PENDING or job.attempt_count != 0:
                raise IntegrityViolation(
                    "only a new pending assessment job can be enqueued"
                )
            with self._transaction():
                cursor = self._connection.execute(
                    """
                    INSERT INTO archivability.assessment_jobs
                        (job_id, analysis_id, observation_id, state, attempt_count,
                         max_attempts, revision, available_at, claimed_by,
                         lease_expires_at, completed_at, error_code, document_json,
                         created_at, stored_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (observation_id) DO NOTHING
                    RETURNING job_id
                    """,
                    self._job_values(job),
                )
                created = cursor.fetchone() is not None
                stored = self._get_job_for_observation(job.observation_id)
                if stored is None:
                    raise IntegrityViolation("enqueued assessment job disappeared")
                if (
                    stored.analysis_id != job.analysis_id
                    or stored.max_attempts != job.max_attempts
                ):
                    raise IntegrityViolation(
                        "queued observation conflicts with its existing job"
                    )
                resource_id = stored.job_id
                self._write_audit(
                    resource="assessment_job",
                    resource_id=resource_id,
                    result="success",
                    audit=audit,
                    action="job.http_assessment_queue_enqueue",
                    after=self._job_audit_state(stored),
                    extra={"outcome": "created" if created else "replayed"},
                )
            return stored, created
        except psycopg.IntegrityError as exc:
            self._record_queue_failure(resource_id, audit, exc, "enqueue")
            self._raise_integrity(exc)
        except PersistenceError as exc:
            self._record_queue_failure(resource_id, audit, exc, "enqueue")
            raise

    def get_assessment_job(self, job_id: str) -> AssessmentJob | None:
        row = self._connection.execute(
            f"""
            SELECT {_JOB_COLUMNS}
            FROM archivability.assessment_jobs
            WHERE job_id = %s
            """,
            (job_id,),
        ).fetchone()
        return self._job_from_row(row, expected_job_id=job_id) if row else None

    def claim_next_assessment_job(
        self,
        *,
        worker_id: str,
        occurred_at: datetime,
        lease_expires_at: datetime,
        audit: AuditContext = AuditContext(),
    ) -> AssessmentJob | None:
        resource_id = "assessment_queue"
        try:
            with self._transaction():
                row = self._connection.execute(
                    f"""
                    SELECT {_JOB_COLUMNS}
                    FROM archivability.assessment_jobs
                    WHERE attempt_count < max_attempts
                      AND (
                        (state = 'pending' AND available_at <= %s)
                        OR (state = 'running' AND lease_expires_at <= %s)
                      )
                    ORDER BY available_at, created_at, job_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """,
                    (occurred_at, occurred_at),
                ).fetchone()
                if row is None:
                    return None
                previous = self._job_from_row(row)
                recovered = previous.state is AssessmentJobState.RUNNING
                current = claim_assessment_job(
                    previous,
                    worker_id=worker_id,
                    occurred_at=occurred_at,
                    lease_expires_at=lease_expires_at,
                )
                resource_id = current.job_id
                self._update_assessment_job(previous, current)
                self._write_audit(
                    resource="assessment_job",
                    resource_id=current.job_id,
                    result="success",
                    audit=audit,
                    action="job.http_assessment_queue_claim",
                    before=self._job_audit_state(previous),
                    after=self._job_audit_state(current),
                    extra={"recovered_expired_lease": recovered},
                )
                return current
        except psycopg.IntegrityError as exc:
            self._record_queue_failure(resource_id, audit, exc, "claim")
            self._raise_integrity(exc)
        except PersistenceError as exc:
            self._record_queue_failure(resource_id, audit, exc, "claim")
            raise

    def update_assessment_job(
        self,
        previous: AssessmentJob,
        current: AssessmentJob,
        *,
        action: str,
        audit: AuditContext = AuditContext(),
    ) -> None:
        if action not in _UPDATE_ACTIONS:
            raise IntegrityViolation("assessment job audit action is not allowed")
        try:
            if previous.state is not AssessmentJobState.RUNNING:
                raise IntegrityViolation(
                    "only a running assessment job can be completed"
                )
            if current.state not in {
                AssessmentJobState.PENDING,
                AssessmentJobState.SUCCEEDED,
                AssessmentJobState.FAILED,
            }:
                raise IntegrityViolation("assessment job target state is invalid")
            if _ACTION_STATES[action] is not current.state:
                raise IntegrityViolation(
                    "assessment job audit action does not match state"
                )
            with self._transaction():
                self._update_assessment_job(previous, current)
                self._write_audit(
                    resource="assessment_job",
                    resource_id=current.job_id,
                    result=(
                        "success"
                        if current.state is AssessmentJobState.SUCCEEDED
                        else "failure"
                    ),
                    audit=audit,
                    action=action,
                    before=self._job_audit_state(previous),
                    after=self._job_audit_state(current),
                    extra={"error_code": current.error_code},
                )
        except psycopg.IntegrityError as exc:
            self._record_failure(
                "assessment_job", previous.job_id, audit, exc, action=action
            )
            self._raise_integrity(exc)
        except PersistenceError as exc:
            self._record_failure(
                "assessment_job", previous.job_id, audit, exc, action=action
            )
            raise

    def record_http_workflow_event(
        self,
        *,
        analysis_id: str,
        attempt_id: str,
        job_id: str | None,
        result: str,
        status: str,
        failure_code: str | None,
        audit: AuditContext = AuditContext(),
    ) -> None:
        try:
            self._validate_workflow_event(
                result=result,
                status=status,
                job_id=job_id,
                failure_code=failure_code,
            )
            with self._transaction():
                attempt = self._connection.execute(
                    """
                    SELECT 1 FROM archivability.attempts
                    WHERE attempt_id = %s AND analysis_id = %s
                    """,
                    (attempt_id, analysis_id),
                ).fetchone()
                if attempt is None:
                    raise IntegrityViolation(
                        "workflow event does not reference a valid attempt"
                    )
                if job_id is not None:
                    job = self._connection.execute(
                        """
                        SELECT 1 FROM archivability.assessment_jobs
                        WHERE job_id = %s AND analysis_id = %s
                        """,
                        (job_id, analysis_id),
                    ).fetchone()
                    if job is None:
                        raise IntegrityViolation(
                            "workflow event does not reference a valid job"
                        )
                self._write_audit(
                    resource="analysis",
                    resource_id=analysis_id,
                    result=result,
                    audit=audit,
                    action="job.http_assessment_workflow",
                    extra={
                        "attempt_id": attempt_id,
                        "job_id": job_id,
                        "status": status,
                        "failure_code": failure_code,
                    },
                )
        except psycopg.IntegrityError as exc:
            self._record_failure(
                "analysis",
                analysis_id,
                audit,
                exc,
                action="job.http_assessment_workflow",
            )
            self._raise_integrity(exc)
        except PersistenceError as exc:
            self._record_failure(
                "analysis",
                analysis_id,
                audit,
                exc,
                action="job.http_assessment_workflow",
            )
            raise

    def _get_job_for_observation(self, observation_id: str) -> AssessmentJob | None:
        row = self._connection.execute(
            f"""
            SELECT {_JOB_COLUMNS}
            FROM archivability.assessment_jobs
            WHERE observation_id = %s
            """,
            (observation_id,),
        ).fetchone()
        return self._job_from_row(row) if row else None

    def _job_values(self, value: AssessmentJob) -> tuple[Any, ...]:
        return (
            value.job_id,
            value.analysis_id,
            value.observation_id,
            value.state.value,
            value.attempt_count,
            value.max_attempts,
            value.revision,
            self._job_timestamp(value.available_at),
            value.claimed_by,
            self._optional_job_timestamp(value.lease_expires_at),
            self._optional_job_timestamp(value.completed_at),
            value.error_code,
            Jsonb(value.to_dict()),
            self._job_timestamp(value.created_at),
            self._now(),
            self._job_timestamp(value.updated_at),
        )

    def _update_assessment_job(
        self, previous: AssessmentJob, current: AssessmentJob
    ) -> None:
        self._validate_job_identity(previous, current)
        if current.revision != previous.revision + 1:
            raise ConcurrencyConflict("assessment job revision must increase by one")
        cursor = self._connection.execute(
            """
            UPDATE archivability.assessment_jobs
            SET state = %s, attempt_count = %s, revision = %s, available_at = %s,
                claimed_by = %s, lease_expires_at = %s, completed_at = %s,
                error_code = %s, document_json = %s, updated_at = %s
            WHERE job_id = %s AND revision = %s AND state = %s
              AND claimed_by IS NOT DISTINCT FROM %s
            """,
            (
                current.state.value,
                current.attempt_count,
                current.revision,
                self._job_timestamp(current.available_at),
                current.claimed_by,
                self._optional_job_timestamp(current.lease_expires_at),
                self._optional_job_timestamp(current.completed_at),
                current.error_code,
                Jsonb(current.to_dict()),
                self._job_timestamp(current.updated_at),
                previous.job_id,
                previous.revision,
                previous.state.value,
                previous.claimed_by,
            ),
        )
        if cursor.rowcount != 1:
            raise ConcurrencyConflict(
                "assessment job was changed by another worker"
            )

    def _job_from_row(
        self,
        row: tuple[Any, ...],
        *,
        expected_job_id: str | None = None,
    ) -> AssessmentJob:
        try:
            document = row[0]
            value = AssessmentJob(
                job_id=document["job_id"],
                analysis_id=document["analysis_id"],
                observation_id=document["observation_id"],
                state=AssessmentJobState(document["state"]),
                attempt_count=document["attempt_count"],
                max_attempts=document["max_attempts"],
                available_at=datetime.fromisoformat(document["available_at"]),
                created_at=datetime.fromisoformat(document["created_at"]),
                updated_at=datetime.fromisoformat(document["updated_at"]),
                claimed_by=document["claimed_by"],
                lease_expires_at=(
                    datetime.fromisoformat(document["lease_expires_at"])
                    if document["lease_expires_at"] is not None
                    else None
                ),
                completed_at=(
                    datetime.fromisoformat(document["completed_at"])
                    if document["completed_at"] is not None
                    else None
                ),
                error_code=document["error_code"],
                revision=document["revision"],
            )
            expected_columns = (
                value.analysis_id,
                value.observation_id,
                value.state.value,
                value.revision,
                value.attempt_count,
                value.max_attempts,
                self._job_timestamp(value.available_at),
                value.claimed_by,
                self._optional_job_timestamp(value.lease_expires_at),
                self._optional_job_timestamp(value.completed_at),
                value.error_code,
                self._job_timestamp(value.created_at),
                self._job_timestamp(value.updated_at),
            )
            stored_columns = tuple(
                self._job_timestamp(item) if isinstance(item, datetime) else item
                for item in row[1:]
            )
            if (
                (expected_job_id is not None and value.job_id != expected_job_id)
                or expected_columns != stored_columns
            ):
                raise IntegrityViolation(
                    "assessment job document does not match columns"
                )
            return value
        except IntegrityViolation:
            raise
        except (JobError, KeyError, TypeError, ValueError) as exc:
            raise IntegrityViolation("stored assessment job failed validation") from exc

    @staticmethod
    def _validate_job_identity(
        previous: AssessmentJob, current: AssessmentJob
    ) -> None:
        if (
            previous.job_id != current.job_id
            or previous.analysis_id != current.analysis_id
            or previous.observation_id != current.observation_id
            or previous.max_attempts != current.max_attempts
            or previous.created_at != current.created_at
        ):
            raise IntegrityViolation("assessment job identity cannot change")

    @staticmethod
    def _job_audit_state(value: AssessmentJob) -> dict[str, Any]:
        return {
            "analysis_id": value.analysis_id,
            "state": value.state.value,
            "attempt_count": value.attempt_count,
            "max_attempts": value.max_attempts,
            "revision": value.revision,
            "error_code": value.error_code,
        }

    @staticmethod
    def _job_timestamp(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise PersistenceError("job timestamp must include a timezone")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _optional_job_timestamp(value: datetime | None) -> datetime | None:
        return (
            PostgreSqlAssessmentJobRepository._job_timestamp(value)
            if value is not None
            else None
        )

    def _record_queue_failure(
        self,
        resource_id: str,
        audit: AuditContext,
        error: Exception,
        operation: str,
    ) -> None:
        self._record_failure(
            "assessment_job",
            resource_id,
            audit,
            error,
            action=f"job.http_assessment_queue_{operation}",
        )

    @staticmethod
    def _validate_workflow_event(
        *,
        result: str,
        status: str,
        job_id: str | None,
        failure_code: str | None,
    ) -> None:
        if status == "assessment_queued":
            valid = result == "success" and job_id is not None and failure_code is None
        elif status in {"retry_available", "failed"}:
            valid = (
                result == "failure"
                and job_id is None
                and failure_code in {"PROBE_FAILED", "PERSISTENCE_FAILED"}
            )
        elif status == "queue_failed":
            valid = (
                result == "failure"
                and job_id is None
                and failure_code == "QUEUE_FAILED"
            )
        else:
            valid = False
        if not valid:
            raise IntegrityViolation("HTTP workflow audit event is inconsistent")
