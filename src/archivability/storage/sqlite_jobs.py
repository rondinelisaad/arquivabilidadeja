from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from archivability.jobs.models import AssessmentJob, AssessmentJobState, JobError
from archivability.jobs.state_machine import claim_assessment_job
from archivability.lifecycle.models import AnalysisState
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
        resource_id = analysis_id
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
                    SELECT 1 FROM attempts
                    WHERE attempt_id = ? AND analysis_id = ?
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
                        SELECT 1 FROM assessment_jobs
                        WHERE job_id = ? AND analysis_id = ?
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
        except sqlite3.IntegrityError as exc:
            self._record_failure(
                "analysis",
                resource_id,
                audit,
                exc,
                action="job.http_assessment_workflow",
            )
            self._raise_integrity(exc)
        except (sqlite3.DatabaseError, PersistenceError) as exc:
            self._record_failure(
                "analysis",
                resource_id,
                audit,
                exc,
                action="job.http_assessment_workflow",
            )
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError("database write failed") from exc

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

    def load_analysis_report_snapshot(self, analysis_id: str):
        if self._connection.in_transaction:
            raise PersistenceError("nested or externally managed transactions are unsupported")
        self._connection.execute("BEGIN")
        try:
            analysis = self.get_analysis(analysis_id)
            if analysis is None:
                snapshot = None
            else:
                attempts = self.list_attempts(analysis_id)
                job_ids = tuple(
                    row[0]
                    for row in self._connection.execute(
                        """
                        SELECT job_id FROM assessment_jobs
                        WHERE analysis_id = ?
                        ORDER BY created_at, job_id
                        """,
                        (analysis_id,),
                    ).fetchall()
                )
                jobs = tuple(self.get_assessment_job(job_id) for job_id in job_ids)
                evidence_ids = tuple(
                    row[0]
                    for row in self._connection.execute(
                        """
                        SELECT evidence_id FROM evidence
                        WHERE analysis_id = ?
                        ORDER BY indicator_id, evidence_id
                        """,
                        (analysis_id,),
                    ).fetchall()
                )
                evidence = tuple(self.get_evidence(value) for value in evidence_ids)
                indicator_ids = tuple(
                    row[0]
                    for row in self._connection.execute(
                        """
                        SELECT indicator_id FROM indicator_results
                        WHERE analysis_id = ?
                        ORDER BY indicator_id
                        """,
                        (analysis_id,),
                    ).fetchall()
                )
                results = tuple(
                    self.get_indicator_result(analysis_id, value)
                    for value in indicator_ids
                )
                if any(value is None for value in (*jobs, *evidence, *results)):
                    raise IntegrityViolation(
                        "analysis report relation disappeared during snapshot"
                    )
                from archivability.application.read_model import AnalysisReportSnapshot

                try:
                    snapshot = AnalysisReportSnapshot(
                        analysis=analysis,
                        attempts=attempts,
                        jobs=tuple(value for value in jobs if value is not None),
                        indicator_results=tuple(
                            value for value in results if value is not None
                        ),
                        evidence=tuple(value for value in evidence if value is not None),
                    )
                except ValueError as exc:
                    raise IntegrityViolation(
                        "analysis report snapshot failed integrity validation"
                    ) from exc
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()
            return snapshot

    def record_analysis_report_access(
        self,
        *,
        analysis_id: str,
        result: str,
        error_code: str | None,
        state: str | None,
        attempt_count: int,
        job_count: int,
        result_count: int,
        audit: AuditContext = AuditContext(),
    ) -> None:
        resource_id = analysis_id
        try:
            if result not in {"success", "failure"}:
                raise IntegrityViolation("analysis report result is invalid")
            if result == "success" and error_code is not None:
                raise IntegrityViolation("successful report access cannot have an error")
            if result == "failure" and error_code not in {
                "ANALYSIS_NOT_FOUND",
                "REPORT_BUILD_FAILED",
            }:
                raise IntegrityViolation("report failure code is invalid")
            if state is not None and state not in {item.value for item in AnalysisState}:
                raise IntegrityViolation("analysis report state is invalid")
            counts = (attempt_count, job_count, result_count)
            if any(not isinstance(value, int) or value < 0 for value in counts):
                raise IntegrityViolation("analysis report counts are invalid")
            with self._transaction():
                self._write_audit(
                    resource="analysis_report",
                    resource_id=analysis_id,
                    result=result,
                    audit=audit,
                    action="access.analysis_report",
                    extra={
                        "error_code": error_code,
                        "state": state,
                        "attempt_count": attempt_count,
                        "job_count": job_count,
                        "result_count": result_count,
                    },
                )
        except sqlite3.IntegrityError as exc:
            self._record_failure(
                "analysis_report",
                resource_id,
                audit,
                exc,
                action="access.analysis_report",
            )
            self._raise_integrity(exc)
        except (sqlite3.DatabaseError, PersistenceError) as exc:
            self._record_failure(
                "analysis_report",
                resource_id,
                audit,
                exc,
                action="access.analysis_report",
            )
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError("database write failed") from exc

    def record_analysis_api_event(
        self,
        *,
        request_id: str,
        operation: str,
        result: str,
        status_code: int,
        error_code: str | None,
        analysis_id: str | None,
        audit: AuditContext = AuditContext(),
    ) -> None:
        resource_id = request_id
        try:
            if operation not in {"create", "read"}:
                raise IntegrityViolation("analysis API operation is invalid")
            if result == "success":
                valid = (
                    error_code is None
                    and analysis_id is not None
                    and status_code == (202 if operation == "create" else 200)
                )
            elif result == "unauthorized":
                valid = status_code in {401, 403} and error_code in {
                    "AUTHENTICATION_REQUIRED",
                    "ACCESS_DENIED",
                }
            elif result == "failure":
                valid = (status_code, error_code) in {
                    (400, "INVALID_REQUEST"),
                    (404, "NOT_FOUND"),
                    (429, "RATE_LIMITED"),
                    (500, "INTERNAL_ERROR"),
                }
            else:
                valid = False
            if not valid:
                raise IntegrityViolation("analysis API audit event is inconsistent")
            if result == "unauthorized":
                action = "access.denied"
            elif error_code == "RATE_LIMITED":
                action = "access.rate_limited"
            else:
                action = f"access.analysis_api_{operation}"
            with self._transaction():
                self._write_audit(
                    resource="analysis_api",
                    resource_id=request_id,
                    result=result,
                    audit=audit,
                    action=action,
                    extra={
                        "operation": operation,
                        "status_code": status_code,
                        "error_code": error_code,
                        "analysis_id": analysis_id,
                    },
                )
        except sqlite3.IntegrityError as exc:
            self._record_failure(
                "analysis_api", resource_id, audit, exc, action="access.analysis_api"
            )
            self._raise_integrity(exc)
        except (sqlite3.DatabaseError, PersistenceError) as exc:
            self._record_failure(
                "analysis_api", resource_id, audit, exc, action="access.analysis_api"
            )
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError("database write failed") from exc

    def record_analysis_http_event(
        self,
        *,
        request_id: str,
        route: str,
        method: str,
        result: str,
        status_code: int,
        error_code: str,
        audit: AuditContext = AuditContext(),
    ) -> None:
        resource_id = request_id
        try:
            valid_errors = {
                (400, "INVALID_REQUEST"),
                (400, "INVALID_JSON"),
                (404, "NOT_FOUND"),
                (405, "METHOD_NOT_ALLOWED"),
                (413, "PAYLOAD_TOO_LARGE"),
                (415, "UNSUPPORTED_MEDIA_TYPE"),
                (500, "INTERNAL_ERROR"),
            }
            if (
                route not in {"analyses_collection", "analysis_item", "unmatched"}
                or method
                not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "OTHER"}
                or result != "failure"
                or (status_code, error_code) not in valid_errors
            ):
                raise IntegrityViolation("analysis HTTP audit event is inconsistent")
            with self._transaction():
                self._write_audit(
                    resource="analysis_http_adapter",
                    resource_id=request_id,
                    result=result,
                    audit=audit,
                    action="access.analysis_http_adapter",
                    extra={
                        "route": route,
                        "method": method,
                        "status_code": status_code,
                        "error_code": error_code,
                    },
                )
        except sqlite3.IntegrityError as exc:
            self._record_failure(
                "analysis_http_adapter",
                resource_id,
                audit,
                exc,
                action="access.analysis_http_adapter",
            )
            self._raise_integrity(exc)
        except (sqlite3.DatabaseError, PersistenceError) as exc:
            self._record_failure(
                "analysis_http_adapter",
                resource_id,
                audit,
                exc,
                action="access.analysis_http_adapter",
            )
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError("database write failed") from exc

    def record_bearer_authentication_event(
        self,
        *,
        request_id: str,
        result: str,
        error_code: str | None,
        audit: AuditContext = AuditContext(),
    ) -> None:
        resource_id = request_id
        try:
            valid = (result == "success" and error_code is None) or (
                result == "failure" and error_code == "INVALID_TOKEN"
            )
            if not valid:
                raise IntegrityViolation("bearer authentication event is inconsistent")
            with self._transaction():
                self._write_audit(
                    resource="authentication",
                    resource_id=request_id,
                    result=result,
                    audit=audit,
                    action="auth.bearer_token",
                    extra={"error_code": error_code},
                )
        except sqlite3.IntegrityError as exc:
            self._record_failure(
                "authentication",
                resource_id,
                audit,
                exc,
                action="auth.bearer_token",
            )
            self._raise_integrity(exc)
        except (sqlite3.DatabaseError, PersistenceError) as exc:
            self._record_failure(
                "authentication",
                resource_id,
                audit,
                exc,
                action="auth.bearer_token",
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
