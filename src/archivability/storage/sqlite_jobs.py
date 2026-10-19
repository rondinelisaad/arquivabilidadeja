from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from archivability.browser.models import BrowserBudgets, BrowserProfile
from archivability.jobs.browser import (
    BrowserJob,
    claim_browser_job,
)
from archivability.jobs.collection import (
    AnalysisIdempotency,
    CollectionJob,
    CollectionLimits,
    claim_collection_job,
)
from archivability.jobs.models import AssessmentJob, AssessmentJobState, JobError
from archivability.jobs.state_machine import claim_assessment_job
from archivability.lifecycle.models import Analysis, AnalysisState
from archivability.storage.audit import AuditContext
from archivability.storage.errors import (
    ConcurrencyConflict,
    IdempotencyConflict,
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

    def add_analysis_with_collection_job(
        self,
        analysis: Analysis,
        job: CollectionJob,
        *,
        owner_user_id: str | None,
        idempotency: AnalysisIdempotency | None = None,
        audit: AuditContext = AuditContext(),
    ) -> tuple[Analysis, CollectionJob, bool]:
        if job.analysis_id != analysis.analysis_id:
            raise IntegrityViolation("collection job belongs to another analysis")
        if owner_user_id is not None and (
            not isinstance(owner_user_id, str)
            or not 1 <= len(owner_user_id) <= 256
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in owner_user_id
            )
        ):
            raise PersistenceError("analysis owner identifier is invalid")
        if owner_user_id is not None and owner_user_id != audit.user_id:
            raise PersistenceError("analysis owner must match the audited actor")
        if idempotency is not None and (
            owner_user_id is None or idempotency.owner_user_id != owner_user_id
        ):
            raise IntegrityViolation("idempotency owner does not match analysis owner")
        try:
            with self._transaction():
                if idempotency is not None:
                    existing = self._connection.execute(
                        """
                        SELECT request_hash, analysis_id
                        FROM analysis_idempotency_keys
                        WHERE owner_user_id = ? AND key_hash = ?
                        """,
                        (idempotency.owner_user_id, idempotency.key_hash),
                    ).fetchone()
                    if existing is not None:
                        if existing[0] != idempotency.request_hash:
                            raise IdempotencyConflict(
                                "idempotency key was used for another request"
                            )
                        stored_analysis = self.get_analysis(existing[1])
                        stored_job = self._get_collection_job_for_analysis(existing[1])
                        if stored_analysis is None or stored_job is None:
                            raise IntegrityViolation(
                                "idempotent analysis reference is incomplete"
                            )
                        self._write_audit(
                            resource="collection_job",
                            resource_id=stored_job.job_id,
                            result="success",
                            audit=audit,
                            action="job.http_collection_queue_enqueue",
                            after=self._collection_job_audit_state(stored_job),
                            extra={"outcome": "replayed"},
                        )
                        return stored_analysis, stored_job, False
                self._insert_analysis(analysis)
                if owner_user_id is not None:
                    self._insert_analysis_owner(analysis.analysis_id, owner_user_id)
                self._insert_collection_job(job)
                if idempotency is not None:
                    self._connection.execute(
                        """
                        INSERT INTO analysis_idempotency_keys
                            (owner_user_id, key_hash, request_hash, analysis_id, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            idempotency.owner_user_id,
                            idempotency.key_hash,
                            idempotency.request_hash,
                            analysis.analysis_id,
                            self._timestamp(),
                        ),
                    )
                self._write_audit(
                    resource="analysis",
                    resource_id=analysis.analysis_id,
                    result="success",
                    audit=audit,
                    action="data.create",
                    after={
                        "state": analysis.state.value,
                        "revision": analysis.revision,
                        "owner_bound": owner_user_id is not None,
                    },
                )
                if owner_user_id is not None:
                    self._write_audit(
                        resource="analysis_ownership",
                        resource_id=analysis.analysis_id,
                        result="success",
                        audit=audit,
                        action="permission.analysis_owner_assign",
                        after={"owner_bound": True},
                    )
                self._write_audit(
                    resource="collection_job",
                    resource_id=job.job_id,
                    result="success",
                    audit=audit,
                    action="job.http_collection_queue_enqueue",
                    after=self._collection_job_audit_state(job),
                    extra={"outcome": "created"},
                )
            return analysis, job, True
        except sqlite3.IntegrityError as exc:
            self._record_failure(
                "collection_job",
                job.job_id,
                audit,
                exc,
                action="job.http_collection_queue_enqueue",
            )
            self._raise_integrity(exc)
        except sqlite3.DatabaseError as exc:
            raise PersistenceError("database write failed") from exc

    def enqueue_collection_job(
        self,
        job: CollectionJob,
        *,
        audit: AuditContext = AuditContext(),
    ) -> tuple[CollectionJob, bool]:
        try:
            with self._transaction():
                existing = self._get_collection_job_for_analysis(job.analysis_id)
                if existing is None:
                    self._insert_collection_job(job)
                    stored, created = job, True
                else:
                    if (
                        existing.max_attempts != job.max_attempts
                        or existing.assessment_max_attempts
                        != job.assessment_max_attempts
                        or existing.limits != job.limits
                    ):
                        raise IntegrityViolation("collection job replay conflicts with existing job")
                    stored, created = existing, False
                self._write_audit(
                    resource="collection_job",
                    resource_id=stored.job_id,
                    result="success",
                    audit=audit,
                    action="job.http_collection_queue_enqueue",
                    after=self._collection_job_audit_state(stored),
                    extra={"outcome": "created" if created else "replayed"},
                )
            return stored, created
        except sqlite3.IntegrityError as exc:
            self._record_failure(
                "collection_job", job.job_id, audit, exc,
                action="job.http_collection_queue_enqueue",
            )
            self._raise_integrity(exc)
        except sqlite3.DatabaseError as exc:
            raise PersistenceError("database write failed") from exc

    def enqueue_browser_job(
        self,
        job: BrowserJob,
        *,
        audit: AuditContext = AuditContext(),
    ) -> tuple[BrowserJob, bool]:
        try:
            with self._transaction():
                existing = self._get_browser_job_for_attempt(job.attempt_id)
                if existing is None:
                    self._insert_browser_job(job)
                    stored, created = job, True
                else:
                    if (
                        existing.analysis_id != job.analysis_id
                        or existing.max_attempts != job.max_attempts
                        or existing.assessment_max_attempts
                        != job.assessment_max_attempts
                        or existing.budgets != job.budgets
                        or existing.profile != job.profile
                    ):
                        raise IntegrityViolation(
                            "browser job replay conflicts with existing job"
                        )
                    stored, created = existing, False
                self._write_audit(
                    resource="browser_job",
                    resource_id=stored.job_id,
                    result="success",
                    audit=audit,
                    action="job.browser_queue_enqueue",
                    after=self._browser_job_audit_state(stored),
                    extra={"outcome": "created" if created else "replayed"},
                )
            return stored, created
        except sqlite3.IntegrityError as exc:
            self._record_failure(
                "browser_job",
                job.job_id,
                audit,
                exc,
                action="job.browser_queue_enqueue",
            )
            self._raise_integrity(exc)
        except sqlite3.DatabaseError as exc:
            raise PersistenceError("database write failed") from exc

    def get_browser_job(self, job_id: str) -> BrowserJob | None:
        row = self._connection.execute(
            """
            SELECT document_json, analysis_id, attempt_id, state, revision,
                   attempt_count, max_attempts, available_at, claimed_by,
                   lease_expires_at, completed_at, error_code, created_at, updated_at
            FROM browser_jobs WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        return self._browser_job_from_row(row, expected_job_id=job_id) if row else None

    def claim_next_browser_job(
        self,
        *,
        worker_id: str,
        occurred_at: datetime,
        lease_expires_at: datetime,
        audit: AuditContext = AuditContext(),
    ) -> BrowserJob | None:
        try:
            with self._transaction():
                timestamp = self._job_timestamp(occurred_at)
                row = self._connection.execute(
                    """
                    SELECT job_id FROM browser_jobs
                    WHERE attempt_count < max_attempts
                      AND EXISTS (
                        SELECT 1 FROM analyses
                        WHERE analyses.analysis_id = browser_jobs.analysis_id
                          AND analyses.state = 'running'
                      )
                      AND ((state = 'pending' AND available_at <= ?)
                        OR (state = 'running' AND lease_expires_at <= ?))
                    ORDER BY available_at, created_at, job_id
                    LIMIT 1
                    """,
                    (timestamp, timestamp),
                ).fetchone()
                if row is None:
                    return None
                previous = self.get_browser_job(row[0])
                if previous is None:
                    raise IntegrityViolation("claimable browser job disappeared")
                current = claim_browser_job(
                    previous,
                    worker_id=worker_id,
                    occurred_at=occurred_at,
                    lease_expires_at=lease_expires_at,
                )
                self._update_browser_job(previous, current)
                self._write_audit(
                    resource="browser_job",
                    resource_id=current.job_id,
                    result="success",
                    audit=audit,
                    action="job.browser_queue_claim",
                    before=self._browser_job_audit_state(previous),
                    after=self._browser_job_audit_state(current),
                    extra={
                        "recovered_expired_lease": (
                            previous.state is AssessmentJobState.RUNNING
                        )
                    },
                )
                return current
        except sqlite3.IntegrityError as exc:
            self._record_failure(
                "browser_job",
                "browser_queue",
                audit,
                exc,
                action="job.browser_queue_claim",
            )
            self._raise_integrity(exc)
        except sqlite3.DatabaseError as exc:
            raise PersistenceError("database write failed") from exc

    def update_browser_job(
        self,
        previous: BrowserJob,
        current: BrowserJob,
        *,
        action: str,
        audit: AuditContext = AuditContext(),
    ) -> None:
        expected = {
            "job.browser_queue_succeeded": AssessmentJobState.SUCCEEDED,
            "job.browser_queue_retry": AssessmentJobState.PENDING,
            "job.browser_queue_failed": AssessmentJobState.FAILED,
        }
        if expected.get(action) is not current.state:
            raise IntegrityViolation("browser job action does not match state")
        try:
            with self._transaction():
                self._update_browser_job(previous, current)
                self._write_audit(
                    resource="browser_job",
                    resource_id=current.job_id,
                    result=(
                        "success"
                        if current.state is AssessmentJobState.SUCCEEDED
                        else "failure"
                    ),
                    audit=audit,
                    action=action,
                    before=self._browser_job_audit_state(previous),
                    after=self._browser_job_audit_state(current),
                    extra={"error_code": current.error_code},
                )
        except sqlite3.IntegrityError as exc:
            self._record_failure("browser_job", previous.job_id, audit, exc, action=action)
            self._raise_integrity(exc)
        except sqlite3.DatabaseError as exc:
            raise PersistenceError("database write failed") from exc

    def get_collection_job(self, job_id: str) -> CollectionJob | None:
        row = self._connection.execute(
            """
            SELECT document_json, analysis_id, state, revision, attempt_count,
                   max_attempts, available_at, claimed_by, lease_expires_at,
                   completed_at, error_code, created_at, updated_at
            FROM collection_jobs WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        return self._collection_job_from_row(row, expected_job_id=job_id) if row else None

    def get_latest_successful_http_observation(self, analysis_id: str):
        row = self._connection.execute(
            """
            SELECT o.observation_id
            FROM observations AS o
            JOIN attempts AS a
              ON a.attempt_id = json_extract(o.document_json, '$.attempt_id')
            WHERE o.analysis_id = ?
              AND json_extract(o.document_json, '$.kind') = 'http_metadata'
              AND json_extract(o.document_json, '$.error_code') IS NULL
              AND a.state = 'succeeded'
            ORDER BY a.sequence DESC,
                     json_extract(o.document_json, '$.observed_at') DESC,
                     o.observation_id DESC
            LIMIT 1
            """,
            (analysis_id,),
        ).fetchone()
        return self.get_observation(row[0]) if row is not None else None

    def claim_next_collection_job(
        self,
        *,
        worker_id: str,
        occurred_at: datetime,
        lease_expires_at: datetime,
        audit: AuditContext = AuditContext(),
    ) -> CollectionJob | None:
        try:
            with self._transaction():
                timestamp = self._job_timestamp(occurred_at)
                row = self._connection.execute(
                    """
                    SELECT job_id FROM collection_jobs
                    WHERE attempt_count < max_attempts
                      AND EXISTS (
                        SELECT 1 FROM analyses
                        WHERE analyses.analysis_id = collection_jobs.analysis_id
                          AND analyses.state IN ('requested', 'running')
                      )
                      AND ((state = 'pending' AND available_at <= ?)
                        OR (state = 'running' AND lease_expires_at <= ?))
                    ORDER BY available_at, created_at, job_id
                    LIMIT 1
                    """,
                    (timestamp, timestamp),
                ).fetchone()
                if row is None:
                    return None
                previous = self.get_collection_job(row[0])
                if previous is None:
                    raise IntegrityViolation("claimable collection job disappeared")
                current = claim_collection_job(
                    previous, worker_id, occurred_at, lease_expires_at
                )
                self._update_collection_job(previous, current)
                self._write_audit(
                    resource="collection_job",
                    resource_id=current.job_id,
                    result="success",
                    audit=audit,
                    action="job.http_collection_queue_claim",
                    before=self._collection_job_audit_state(previous),
                    after=self._collection_job_audit_state(current),
                    extra={"recovered_expired_lease": previous.state is AssessmentJobState.RUNNING},
                )
                return current
        except sqlite3.IntegrityError as exc:
            self._record_failure(
                "collection_job", "collection_queue", audit, exc,
                action="job.http_collection_queue_claim",
            )
            self._raise_integrity(exc)
        except sqlite3.DatabaseError as exc:
            raise PersistenceError("database write failed") from exc

    def update_collection_job(
        self,
        previous: CollectionJob,
        current: CollectionJob,
        *,
        action: str,
        audit: AuditContext = AuditContext(),
    ) -> None:
        expected = {
            "job.http_collection_queue_succeeded": AssessmentJobState.SUCCEEDED,
            "job.http_collection_queue_retry": AssessmentJobState.PENDING,
            "job.http_collection_queue_failed": AssessmentJobState.FAILED,
        }
        if expected.get(action) is not current.state:
            raise IntegrityViolation("collection job action does not match state")
        try:
            with self._transaction():
                self._update_collection_job(previous, current)
                self._write_audit(
                    resource="collection_job",
                    resource_id=current.job_id,
                    result="success" if current.state is AssessmentJobState.SUCCEEDED else "failure",
                    audit=audit,
                    action=action,
                    before=self._collection_job_audit_state(previous),
                    after=self._collection_job_audit_state(current),
                    extra={"error_code": current.error_code},
                )
        except sqlite3.IntegrityError as exc:
            self._record_failure("collection_job", previous.job_id, audit, exc, action=action)
            self._raise_integrity(exc)
        except sqlite3.DatabaseError as exc:
            raise PersistenceError("database write failed") from exc

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
                      AND EXISTS (
                        SELECT 1 FROM analyses
                        WHERE analyses.analysis_id = assessment_jobs.analysis_id
                          AND analyses.state = 'running'
                      )
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
                collection_job_ids = tuple(
                    row[0]
                    for row in self._connection.execute(
                        """
                        SELECT job_id FROM collection_jobs
                        WHERE analysis_id = ?
                        ORDER BY created_at, job_id
                        """,
                        (analysis_id,),
                    ).fetchall()
                )
                assessment_job_ids = tuple(
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
                browser_job_ids = tuple(
                    row[0]
                    for row in self._connection.execute(
                        """
                        SELECT job_id FROM browser_jobs
                        WHERE analysis_id = ?
                        ORDER BY created_at, job_id
                        """,
                        (analysis_id,),
                    ).fetchall()
                )
                jobs = (
                    *(self.get_collection_job(job_id) for job_id in collection_job_ids),
                    *(self.get_browser_job(job_id) for job_id in browser_job_ids),
                    *(self.get_assessment_job(job_id) for job_id in assessment_job_ids),
                )
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
                crawl_observations = tuple(
                    self.get_bounded_crawl_observation(attempt.attempt_id)
                    for attempt in attempts
                )
                browser_observations = tuple(
                    self.get_browser_render_observation(attempt.attempt_id)
                    for attempt in attempts
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
                        crawl_observations=tuple(
                            value for value in crawl_observations if value is not None
                        ),
                        browser_observations=tuple(
                            value
                            for value in browser_observations
                            if value is not None
                        ),
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
        crawl_count: int,
        browser_count: int,
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
            counts = (
                attempt_count,
                job_count,
                result_count,
                crawl_count,
                browser_count,
            )
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
                        "crawl_count": crawl_count,
                        "browser_count": browser_count,
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

    def list_owned_analysis_ids(
        self,
        owner_user_id: str,
        *,
        limit: int,
    ) -> tuple[str, ...]:
        if (
            not isinstance(owner_user_id, str)
            or not owner_user_id
            or len(owner_user_id) > 256
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise IntegrityViolation("analysis history query is invalid")
        try:
            rows = self._connection.execute(
                """
                SELECT analyses.analysis_id
                FROM analyses
                JOIN analysis_ownership
                  ON analysis_ownership.analysis_id = analyses.analysis_id
                WHERE analysis_ownership.owner_user_id = ?
                ORDER BY analyses.updated_at DESC, analyses.analysis_id DESC
                LIMIT ?
                """,
                (owner_user_id, limit),
            ).fetchall()
            return tuple(row[0] for row in rows)
        except sqlite3.DatabaseError as exc:
            raise PersistenceError("database read failed") from exc

    def record_analysis_history_access(
        self,
        *,
        operation: str,
        analysis_id: str | None,
        result: str,
        error_code: str | None,
        item_count: int,
        change_count: int,
        audit: AuditContext = AuditContext(),
    ) -> None:
        resource_id = analysis_id or "history"
        try:
            valid_error = {
                "history": "HISTORY_BUILD_FAILED",
                "site_history": "SITE_HISTORY_BUILD_FAILED",
                "comparison": "COMPARISON_BUILD_FAILED",
            }.get(operation)
            valid_failure_codes = {valid_error}
            if operation == "comparison":
                valid_failure_codes.add("COMPARISON_NOT_FOUND")
            valid = (
                operation in {"history", "site_history", "comparison"}
                and isinstance(item_count, int)
                and not isinstance(item_count, bool)
                and item_count >= 0
                and isinstance(change_count, int)
                and not isinstance(change_count, bool)
                and change_count >= 0
                and (
                    (result == "success" and error_code is None)
                    or (result == "failure" and error_code in valid_failure_codes)
                )
                and (operation in {"history", "site_history"})
                == (analysis_id is None)
            )
            if not valid:
                raise IntegrityViolation("analysis history audit event is inconsistent")
            with self._transaction():
                self._write_audit(
                    resource="analysis_history",
                    resource_id=resource_id,
                    result=result,
                    audit=audit,
                    action=f"access.analysis_{operation}",
                    extra={
                        "operation": operation,
                        "analysis_id": analysis_id,
                        "error_code": error_code,
                        "item_count": item_count,
                        "change_count": change_count,
                    },
                )
        except sqlite3.IntegrityError as exc:
            self._record_failure(
                "analysis_history",
                resource_id,
                audit,
                exc,
                action="access.analysis_history",
            )
            self._raise_integrity(exc)
        except (sqlite3.DatabaseError, PersistenceError) as exc:
            self._record_failure(
                "analysis_history",
                resource_id,
                audit,
                exc,
                action="access.analysis_history",
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
            if operation not in {
                "create",
                "batch_create",
                "read",
                "cancel",
                "export",
                "history",
                "site_history",
                "compare",
                "methodology_read",
                "openapi_read",
                "indicator_list",
                "indicator_read",
            }:
                raise IntegrityViolation("analysis API operation is invalid")
            if result == "success":
                valid = (
                    error_code is None
                    and (
                        analysis_id is not None
                        or operation
                        in {
                            "history",
                            "site_history",
                            "batch_create",
                            "methodology_read",
                            "openapi_read",
                            "indicator_list",
                            "indicator_read",
                        }
                    )
                    and status_code
                    == (202 if operation in {"create", "batch_create"} else 200)
                )
            elif result == "unauthorized":
                valid = status_code in {401, 403} and error_code in {
                    "AUTHENTICATION_REQUIRED",
                    "ACCESS_DENIED",
                }
            elif result == "failure":
                valid = (status_code, error_code) in {
                    (400, "INVALID_REQUEST"),
                    (409, "IDEMPOTENCY_CONFLICT"),
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
                route
                not in {
                    "analyses_collection",
                    "batches_collection",
                    "analysis_item",
                    "analysis_cancel",
                    "analysis_export",
                    "analysis_comparison",
                    "site_history",
                    "methodology",
                    "openapi",
                    "indicators_collection",
                    "indicator_item",
                    "unmatched",
                }
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

    def record_web_ui_event(
        self,
        *,
        request_id: str,
        route: str,
        result: str,
        status_code: int,
        error_code: str | None,
        audit: AuditContext = AuditContext(),
    ) -> None:
        resource_id = request_id
        try:
            valid = (
                route
                in {
                    "home",
                    "analysis",
                    "methodology",
                    "indicator_catalog",
                    "indicator",
                }
                and (
                    (
                        result == "success"
                        and status_code == 200
                        and error_code is None
                    )
                    or (
                        result == "failure"
                        and status_code == 405
                        and error_code == "METHOD_NOT_ALLOWED"
                    )
                )
            )
            if not valid:
                raise IntegrityViolation("web UI audit event is inconsistent")
            with self._transaction():
                self._write_audit(
                    resource="web_ui",
                    resource_id=request_id,
                    result=result,
                    audit=audit,
                    action="access.web_ui",
                    extra={
                        "route": route,
                        "status_code": status_code,
                        "error_code": error_code,
                    },
                )
        except sqlite3.IntegrityError as exc:
            self._record_failure(
                "web_ui", resource_id, audit, exc, action="access.web_ui"
            )
            self._raise_integrity(exc)
        except (sqlite3.DatabaseError, PersistenceError) as exc:
            self._record_failure(
                "web_ui", resource_id, audit, exc, action="access.web_ui"
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

    def _get_collection_job_for_analysis(
        self, analysis_id: str
    ) -> CollectionJob | None:
        row = self._connection.execute(
            """
            SELECT document_json, analysis_id, state, revision, attempt_count,
                   max_attempts, available_at, claimed_by, lease_expires_at,
                   completed_at, error_code, created_at, updated_at
            FROM collection_jobs WHERE analysis_id = ?
            """,
            (analysis_id,),
        ).fetchone()
        return self._collection_job_from_row(row) if row else None

    def _get_browser_job_for_attempt(self, attempt_id: str) -> BrowserJob | None:
        row = self._connection.execute(
            """
            SELECT document_json, analysis_id, attempt_id, state, revision,
                   attempt_count, max_attempts, available_at, claimed_by,
                   lease_expires_at, completed_at, error_code, created_at, updated_at
            FROM browser_jobs WHERE attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        return self._browser_job_from_row(row) if row else None

    def _insert_browser_job(self, value: BrowserJob) -> None:
        self._connection.execute(
            """
            INSERT INTO browser_jobs
                (job_id, analysis_id, attempt_id, state, attempt_count,
                 max_attempts, revision, available_at, claimed_by,
                 lease_expires_at, completed_at, error_code, document_json,
                 created_at, stored_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                value.job_id,
                value.analysis_id,
                value.attempt_id,
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
                self._timestamp(),
                self._job_timestamp(value.updated_at),
            ),
        )

    def _update_browser_job(self, previous: BrowserJob, current: BrowserJob) -> None:
        if (
            previous.job_id != current.job_id
            or previous.analysis_id != current.analysis_id
            or previous.attempt_id != current.attempt_id
            or previous.max_attempts != current.max_attempts
            or previous.assessment_max_attempts != current.assessment_max_attempts
            or previous.budgets != current.budgets
            or previous.profile != current.profile
            or previous.created_at != current.created_at
        ):
            raise IntegrityViolation("browser job identity cannot change")
        if current.revision != previous.revision + 1:
            raise ConcurrencyConflict("browser job revision must increase by one")
        cursor = self._connection.execute(
            """
            UPDATE browser_jobs
            SET state = ?, attempt_count = ?, revision = ?, available_at = ?,
                claimed_by = ?, lease_expires_at = ?, completed_at = ?,
                error_code = ?, document_json = ?, updated_at = ?
            WHERE job_id = ? AND revision = ? AND state = ? AND claimed_by IS ?
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
            raise ConcurrencyConflict("browser job was changed by another worker")

    def _browser_job_from_row(
        self, row: tuple[Any, ...], *, expected_job_id: str | None = None
    ) -> BrowserJob:
        try:
            document = json.loads(row[0])
            value = BrowserJob(
                job_id=document["job_id"],
                analysis_id=row[1],
                attempt_id=row[2],
                state=AssessmentJobState(row[3]),
                revision=row[4],
                attempt_count=row[5],
                max_attempts=row[6],
                assessment_max_attempts=document["assessment_max_attempts"],
                budgets=BrowserBudgets(**document["budgets"]),
                profile=BrowserProfile(**document["profile"]),
                available_at=_parse_datetime(row[7]),
                claimed_by=row[8],
                lease_expires_at=_parse_datetime(row[9]) if row[9] else None,
                completed_at=_parse_datetime(row[10]) if row[10] else None,
                error_code=row[11],
                created_at=_parse_datetime(row[12]),
                updated_at=_parse_datetime(row[13]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, JobError) as exc:
            raise IntegrityViolation("stored browser job failed validation") from exc
        if expected_job_id is not None and value.job_id != expected_job_id:
            raise IntegrityViolation("stored browser job identity is inconsistent")
        return value

    @staticmethod
    def _browser_job_audit_state(value: BrowserJob) -> dict[str, Any]:
        return {
            "analysis_id": value.analysis_id,
            "attempt_id": value.attempt_id,
            "state": value.state.value,
            "attempt_count": value.attempt_count,
            "max_attempts": value.max_attempts,
            "assessment_max_attempts": value.assessment_max_attempts,
            "profile_id": value.profile.profile_id,
            "revision": value.revision,
        }

    def _insert_collection_job(self, value: CollectionJob) -> None:
        self._connection.execute(
            """
            INSERT INTO collection_jobs
                (job_id, analysis_id, state, attempt_count, max_attempts,
                 revision, available_at, claimed_by, lease_expires_at,
                 completed_at, error_code, document_json, created_at,
                 stored_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                value.job_id,
                value.analysis_id,
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
                self._timestamp(),
                self._job_timestamp(value.updated_at),
            ),
        )

    def _update_collection_job(
        self, previous: CollectionJob, current: CollectionJob
    ) -> None:
        if (
            previous.job_id != current.job_id
            or previous.analysis_id != current.analysis_id
            or previous.max_attempts != current.max_attempts
            or previous.assessment_max_attempts
            != current.assessment_max_attempts
            or previous.limits != current.limits
            or previous.created_at != current.created_at
        ):
            raise IntegrityViolation("collection job identity cannot change")
        if current.revision != previous.revision + 1:
            raise ConcurrencyConflict("collection job revision must increase by one")
        cursor = self._connection.execute(
            """
            UPDATE collection_jobs
            SET state = ?, attempt_count = ?, revision = ?, available_at = ?,
                claimed_by = ?, lease_expires_at = ?, completed_at = ?,
                error_code = ?, document_json = ?, updated_at = ?
            WHERE job_id = ? AND revision = ? AND state = ? AND claimed_by IS ?
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
            raise ConcurrencyConflict("collection job was changed by another worker")

    def _collection_job_from_row(
        self, row: tuple[Any, ...], *, expected_job_id: str | None = None
    ) -> CollectionJob:
        try:
            document = json.loads(row[0])
            limits = document["limits"]
            value = CollectionJob(
                job_id=document["job_id"],
                analysis_id=row[1],
                state=AssessmentJobState(row[2]),
                revision=row[3],
                attempt_count=row[4],
                max_attempts=row[5],
                assessment_max_attempts=document["assessment_max_attempts"],
                limits=CollectionLimits(
                    timeout_seconds=limits["timeout_seconds"],
                    max_response_bytes=limits["max_response_bytes"],
                    max_redirects=limits["max_redirects"],
                ),
                available_at=_parse_datetime(row[6]),
                claimed_by=row[7],
                lease_expires_at=_parse_datetime(row[8]) if row[8] else None,
                completed_at=_parse_datetime(row[9]) if row[9] else None,
                error_code=row[10],
                created_at=_parse_datetime(row[11]),
                updated_at=_parse_datetime(row[12]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, JobError) as exc:
            raise IntegrityViolation("stored collection job failed validation") from exc
        if expected_job_id is not None and value.job_id != expected_job_id:
            raise IntegrityViolation("stored collection job identity is inconsistent")
        return value

    @staticmethod
    def _collection_job_audit_state(value: CollectionJob) -> dict[str, Any]:
        return {
            "analysis_id": value.analysis_id,
            "state": value.state.value,
            "attempt_count": value.attempt_count,
            "max_attempts": value.max_attempts,
            "assessment_max_attempts": value.assessment_max_attempts,
            "revision": value.revision,
        }

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
