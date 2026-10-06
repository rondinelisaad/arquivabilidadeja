from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from archivability.jobs.models import AssessmentJob, AssessmentJobState, JobError
from archivability.jobs.state_machine import claim_assessment_job
from archivability.storage.audit import AuditContext
from archivability.storage.errors import (
    ConcurrencyConflict,
    IntegrityViolation,
    PersistenceError,
)
from archivability.storage.sqlite import (
    SqliteLifecycleRepository,
    _dump,
    _parse_datetime,
)


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


class SqliteAssessmentJobRepository(SqliteLifecycleRepository):
    """SQLite assessment queue with exclusive leases and optimistic revisions."""

    def enqueue_assessment_job(
        self,
        job: AssessmentJob,
        *,
        audit: AuditContext = AuditContext(),
    ) -> tuple[AssessmentJob, bool]:
        resource_id = job.job_id
        try:
            if job.state is not AssessmentJobState.PENDING or job.attempt_count != 0:
                raise IntegrityViolation("only a new pending assessment job can be enqueued")
            with self._transaction():
                existing = self._get_job_for_observation(job.observation_id)
                if existing is None:
                    self._insert_assessment_job(job)
                    stored = job
                    created = True
                else:
                    if (
                        existing.analysis_id != job.analysis_id
                        or existing.max_attempts != job.max_attempts
                    ):
                        raise IntegrityViolation(
                            "queued observation conflicts with its existing job"
                        )
                    stored = existing
                    created = False
                    resource_id = existing.job_id
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
        except sqlite3.IntegrityError as exc:
            self._record_failure(
                "assessment_job",
                resource_id,
                audit,
                exc,
                action="job.http_assessment_queue_enqueue",
            )
            self._raise_integrity(exc)
        except (sqlite3.DatabaseError, PersistenceError) as exc:
            self._record_failure(
                "assessment_job",
                resource_id,
                audit,
                exc,
                action="job.http_assessment_queue_enqueue",
            )
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError("database write failed") from exc

    def get_assessment_job(self, job_id: str) -> AssessmentJob | None:
        row = self._connection.execute(
            """
            SELECT document_json, analysis_id, observation_id, state, revision,
                   attempt_count, max_attempts, available_at, claimed_by,
                   lease_expires_at, completed_at, error_code, created_at, updated_at
            FROM assessment_jobs
            WHERE job_id = ?
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
                timestamp = self._job_timestamp(occurred_at)
                row = self._connection.execute(
                    """
                    SELECT job_id
                    FROM assessment_jobs
                    WHERE attempt_count < max_attempts
                      AND (
                        (state = 'pending' AND available_at <= ?)
                        OR (state = 'running' AND lease_expires_at <= ?)
                      )
                    ORDER BY available_at, created_at, job_id
                    LIMIT 1
                    """,
                    (timestamp, timestamp),
                ).fetchone()
                if row is None:
                    return None
                previous = self.get_assessment_job(row[0])
                if previous is None:
                    raise IntegrityViolation("claimable assessment job disappeared")
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
        except sqlite3.IntegrityError as exc:
            self._record_failure(
                "assessment_job",
                resource_id,
                audit,
                exc,
                action="job.http_assessment_queue_claim",
            )
            self._raise_integrity(exc)
        except (sqlite3.DatabaseError, PersistenceError) as exc:
            self._record_failure(
                "assessment_job",
                resource_id,
                audit,
                exc,
                action="job.http_assessment_queue_claim",
            )
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError("database write failed") from exc

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
        resource_id = previous.job_id
        try:
            if previous.state is not AssessmentJobState.RUNNING:
                raise IntegrityViolation("only a running assessment job can be completed")
            if current.state not in {
                AssessmentJobState.PENDING,
                AssessmentJobState.SUCCEEDED,
                AssessmentJobState.FAILED,
            }:
                raise IntegrityViolation("assessment job target state is invalid")
            if _ACTION_STATES[action] is not current.state:
                raise IntegrityViolation("assessment job audit action does not match state")
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
        except sqlite3.IntegrityError as exc:
            self._record_failure(
                "assessment_job", resource_id, audit, exc, action=action
            )
            self._raise_integrity(exc)
        except (sqlite3.DatabaseError, PersistenceError) as exc:
            self._record_failure(
                "assessment_job", resource_id, audit, exc, action=action
            )
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError("database write failed") from exc

    def _get_job_for_observation(self, observation_id: str) -> AssessmentJob | None:
        row = self._connection.execute(
            """
            SELECT document_json, analysis_id, observation_id, state, revision,
                   attempt_count, max_attempts, available_at, claimed_by,
                   lease_expires_at, completed_at, error_code, created_at, updated_at
            FROM assessment_jobs
            WHERE observation_id = ?
            """,
            (observation_id,),
        ).fetchone()
        return self._job_from_row(row) if row else None

    def _insert_assessment_job(self, value: AssessmentJob) -> None:
        timestamp = self._timestamp()
        self._connection.execute(
            """
            INSERT INTO assessment_jobs
                (job_id, analysis_id, observation_id, state, attempt_count,
                 max_attempts, revision, available_at, claimed_by,
                 lease_expires_at, completed_at, error_code, document_json,
                 created_at, stored_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
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
                _dump(value.to_dict()),
                self._job_timestamp(value.created_at),
                timestamp,
                self._job_timestamp(value.updated_at),
            ),
        )

    def _update_assessment_job(
        self, previous: AssessmentJob, current: AssessmentJob
    ) -> None:
        self._validate_job_identity(previous, current)
        if current.revision != previous.revision + 1:
            raise ConcurrencyConflict("assessment job revision must increase by one")
        cursor = self._connection.execute(
            """
            UPDATE assessment_jobs
            SET state = ?, attempt_count = ?, revision = ?, available_at = ?,
                claimed_by = ?, lease_expires_at = ?, completed_at = ?,
                error_code = ?, document_json = ?, updated_at = ?
            WHERE job_id = ? AND revision = ? AND state = ?
              AND claimed_by IS ?
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
                _dump(current.to_dict()),
                self._job_timestamp(current.updated_at),
                previous.job_id,
                previous.revision,
                previous.state.value,
                previous.claimed_by,
            ),
        )
        if cursor.rowcount != 1:
            raise ConcurrencyConflict("assessment job was changed by another worker")

    @staticmethod
    def _validate_job_identity(previous: AssessmentJob, current: AssessmentJob) -> None:
        if (
            previous.job_id != current.job_id
            or previous.analysis_id != current.analysis_id
            or previous.observation_id != current.observation_id
            or previous.max_attempts != current.max_attempts
            or previous.created_at != current.created_at
        ):
            raise IntegrityViolation("assessment job identity cannot change")

    def _job_from_row(
        self,
        row: tuple[Any, ...],
        *,
        expected_job_id: str | None = None,
    ) -> AssessmentJob:
        try:
            document = json.loads(row[0])
            value = AssessmentJob(
                job_id=document["job_id"],
                analysis_id=document["analysis_id"],
                observation_id=document["observation_id"],
                state=AssessmentJobState(document["state"]),
                attempt_count=document["attempt_count"],
                max_attempts=document["max_attempts"],
                available_at=_parse_datetime(document["available_at"]),
                created_at=_parse_datetime(document["created_at"]),
                updated_at=_parse_datetime(document["updated_at"]),
                claimed_by=document["claimed_by"],
                lease_expires_at=(
                    _parse_datetime(document["lease_expires_at"])
                    if document["lease_expires_at"] is not None
                    else None
                ),
                completed_at=(
                    _parse_datetime(document["completed_at"])
                    if document["completed_at"] is not None
                    else None
                ),
                error_code=document["error_code"],
                revision=document["revision"],
            )
            if (
                (expected_job_id is not None and value.job_id != expected_job_id)
                or value.analysis_id != row[1]
                or value.observation_id != row[2]
                or value.state.value != row[3]
                or value.revision != row[4]
                or value.attempt_count != row[5]
                or value.max_attempts != row[6]
                or self._job_timestamp(value.available_at) != row[7]
                or value.claimed_by != row[8]
                or self._optional_job_timestamp(value.lease_expires_at) != row[9]
                or self._optional_job_timestamp(value.completed_at) != row[10]
                or value.error_code != row[11]
                or self._job_timestamp(value.created_at) != row[12]
                or self._job_timestamp(value.updated_at) != row[13]
            ):
                raise IntegrityViolation("assessment job document does not match columns")
            return value
        except IntegrityViolation:
            raise
        except (JobError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IntegrityViolation("stored assessment job failed validation") from exc

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
    def _job_timestamp(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise PersistenceError("job timestamp must include a timezone")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _optional_job_timestamp(value: datetime | None) -> str | None:
        return SqliteAssessmentJobRepository._job_timestamp(value) if value else None
