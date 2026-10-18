from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from archivability.browser.models import BrowserBudgets, BrowserProfile
from archivability.jobs.browser import BrowserJob, claim_browser_job
from archivability.jobs.collection import (
    CollectionJob,
    CollectionLimits,
    claim_collection_job,
)
from archivability.jobs.metrics import QueueMetricsSnapshot
from archivability.jobs.models import AssessmentJob, AssessmentJobState, JobError
from archivability.jobs.state_machine import claim_assessment_job
from archivability.lifecycle.models import Analysis, AnalysisState
from archivability.storage.audit import AuditContext
from archivability.storage.errors import (
    ConcurrencyConflict,
    IntegrityViolation,
    PersistenceError,
)
from archivability.storage.postgresql_evidence import PostgreSqlEvidenceRepository

if TYPE_CHECKING:
    from archivability.application.read_model import AnalysisReportSnapshot


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

    def add_analysis_with_collection_job(
        self,
        analysis: Analysis,
        job: CollectionJob,
        *,
        owner_user_id: str | None,
        audit: AuditContext = AuditContext(),
    ) -> None:
        if job.analysis_id != analysis.analysis_id:
            raise IntegrityViolation("collection job belongs to another analysis")
        self._validate_owner(owner_user_id, audit)
        try:
            with self._transaction():
                now = self._now()
                self._connection.execute(
                    """
                    INSERT INTO archivability.analyses
                        (analysis_id, state, revision, document_json,
                         stored_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        analysis.analysis_id,
                        analysis.state.value,
                        analysis.revision,
                        Jsonb(analysis.to_dict()),
                        now,
                        now,
                    ),
                )
                if owner_user_id is not None:
                    self._connection.execute(
                        """
                        INSERT INTO archivability.analysis_ownership
                            (analysis_id, owner_user_id, created_at)
                        VALUES (%s, %s, %s)
                        """,
                        (analysis.analysis_id, owner_user_id, now),
                    )
                self._connection.execute(
                    """
                    INSERT INTO archivability.collection_jobs
                        (job_id, analysis_id, state, attempt_count, max_attempts,
                         revision, available_at, claimed_by, lease_expires_at,
                         completed_at, error_code, document_json, created_at,
                         stored_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s)
                    """,
                    self._collection_job_values(job),
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
        except psycopg.IntegrityError as exc:
            self._record_failure(
                "collection_job",
                job.job_id,
                audit,
                exc,
                action="job.http_collection_queue_enqueue",
            )
            self._raise_integrity(exc)

    def enqueue_collection_job(
        self,
        job: CollectionJob,
        *,
        audit: AuditContext = AuditContext(),
    ) -> tuple[CollectionJob, bool]:
        try:
            with self._transaction():
                cursor = self._connection.execute(
                    """
                    INSERT INTO archivability.collection_jobs
                        (job_id, analysis_id, state, attempt_count, max_attempts,
                         revision, available_at, claimed_by, lease_expires_at,
                         completed_at, error_code, document_json, created_at,
                         stored_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s)
                    ON CONFLICT (analysis_id) DO NOTHING
                    RETURNING job_id
                    """,
                    self._collection_job_values(job),
                )
                created = cursor.fetchone() is not None
                stored = self._get_collection_job_for_analysis(job.analysis_id)
                if stored is None:
                    raise IntegrityViolation("enqueued collection job disappeared")
                if (
                    stored.max_attempts != job.max_attempts
                    or stored.assessment_max_attempts
                    != job.assessment_max_attempts
                    or stored.limits != job.limits
                ):
                    raise IntegrityViolation("collection job replay conflicts with existing job")
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
        except psycopg.IntegrityError as exc:
            self._record_failure(
                "collection_job",
                job.job_id,
                audit,
                exc,
                action="job.http_collection_queue_enqueue",
            )
            self._raise_integrity(exc)

    def enqueue_browser_job(
        self,
        job: BrowserJob,
        *,
        audit: AuditContext = AuditContext(),
    ) -> tuple[BrowserJob, bool]:
        try:
            with self._transaction():
                cursor = self._connection.execute(
                    """
                    INSERT INTO archivability.browser_jobs
                        (job_id, analysis_id, attempt_id, state, attempt_count,
                         max_attempts, revision, available_at, claimed_by,
                         lease_expires_at, completed_at, error_code, document_json,
                         created_at, stored_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (attempt_id) DO NOTHING
                    RETURNING job_id
                    """,
                    self._browser_job_values(job),
                )
                created = cursor.fetchone() is not None
                stored = self._get_browser_job_for_attempt(job.attempt_id)
                if stored is None:
                    raise IntegrityViolation("enqueued browser job disappeared")
                if (
                    stored.analysis_id != job.analysis_id
                    or stored.max_attempts != job.max_attempts
                    or stored.assessment_max_attempts != job.assessment_max_attempts
                    or stored.budgets != job.budgets
                    or stored.profile != job.profile
                ):
                    raise IntegrityViolation(
                        "browser job replay conflicts with existing job"
                    )
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
        except psycopg.IntegrityError as exc:
            self._record_failure(
                "browser_job",
                job.job_id,
                audit,
                exc,
                action="job.browser_queue_enqueue",
            )
            self._raise_integrity(exc)

    def get_browser_job(self, job_id: str) -> BrowserJob | None:
        row = self._connection.execute(
            """
            SELECT document_json, analysis_id, attempt_id, state, revision,
                   attempt_count, max_attempts, available_at, claimed_by,
                   lease_expires_at, completed_at, error_code, created_at, updated_at
            FROM archivability.browser_jobs WHERE job_id = %s
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
                row = self._connection.execute(
                    """
                    SELECT document_json, analysis_id, attempt_id, state, revision,
                           attempt_count, max_attempts, available_at, claimed_by,
                           lease_expires_at, completed_at, error_code, created_at, updated_at
                    FROM archivability.browser_jobs
                    WHERE attempt_count < max_attempts
                      AND ((state = 'pending' AND available_at <= %s)
                        OR (state = 'running' AND lease_expires_at <= %s))
                    ORDER BY available_at, created_at, job_id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                    """,
                    (occurred_at, occurred_at),
                ).fetchone()
                if row is None:
                    return None
                previous = self._browser_job_from_row(row)
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
        except psycopg.IntegrityError as exc:
            self._record_failure(
                "browser_job",
                "browser_queue",
                audit,
                exc,
                action="job.browser_queue_claim",
            )
            self._raise_integrity(exc)

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
        except psycopg.IntegrityError as exc:
            self._record_failure("browser_job", previous.job_id, audit, exc, action=action)
            self._raise_integrity(exc)

    def get_collection_job(self, job_id: str) -> CollectionJob | None:
        row = self._connection.execute(
            """
            SELECT document_json, analysis_id, state, revision, attempt_count,
                   max_attempts, available_at, claimed_by, lease_expires_at,
                   completed_at, error_code, created_at, updated_at
            FROM archivability.collection_jobs WHERE job_id = %s
            """,
            (job_id,),
        ).fetchone()
        return self._collection_job_from_row(row, expected_job_id=job_id) if row else None

    def get_latest_successful_http_observation(self, analysis_id: str):
        row = self._connection.execute(
            """
            SELECT o.observation_id
            FROM archivability.observations AS o
            JOIN archivability.attempts AS a
              ON a.attempt_id = o.document_json->>'attempt_id'
            WHERE o.analysis_id = %s
              AND o.document_json->>'kind' = 'http_metadata'
              AND o.document_json->>'error_code' IS NULL
              AND a.state = 'succeeded'
            ORDER BY a.sequence DESC,
                     o.document_json->>'observed_at' DESC,
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
                row = self._connection.execute(
                    """
                    SELECT document_json, analysis_id, state, revision, attempt_count,
                           max_attempts, available_at, claimed_by, lease_expires_at,
                           completed_at, error_code, created_at, updated_at
                    FROM archivability.collection_jobs
                    WHERE attempt_count < max_attempts
                      AND ((state = 'pending' AND available_at <= %s)
                        OR (state = 'running' AND lease_expires_at <= %s))
                    ORDER BY available_at, created_at, job_id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                    """,
                    (occurred_at, occurred_at),
                ).fetchone()
                if row is None:
                    return None
                previous = self._collection_job_from_row(row)
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
        except psycopg.IntegrityError as exc:
            self._record_failure(
                "collection_job",
                "collection_queue",
                audit,
                exc,
                action="job.http_collection_queue_claim",
            )
            self._raise_integrity(exc)

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
        except psycopg.IntegrityError as exc:
            self._record_failure("collection_job", previous.job_id, audit, exc, action=action)
            self._raise_integrity(exc)

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

    def load_analysis_report_snapshot(
        self, analysis_id: str
    ) -> AnalysisReportSnapshot | None:
        if self._connection.info.transaction_status is not TransactionStatus.IDLE:
            raise PersistenceError(
                "nested or externally managed transactions are unsupported"
            )
        try:
            with self._connection.transaction():
                self._connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                analysis = self.get_analysis(analysis_id)
                if analysis is None:
                    return None
                attempts = self.list_attempts(analysis_id)
                collection_job_ids = tuple(
                    row[0]
                    for row in self._connection.execute(
                        """
                        SELECT job_id FROM archivability.collection_jobs
                        WHERE analysis_id = %s
                        ORDER BY created_at, job_id
                        """,
                        (analysis_id,),
                    ).fetchall()
                )
                assessment_job_ids = tuple(
                    row[0]
                    for row in self._connection.execute(
                        """
                        SELECT job_id FROM archivability.assessment_jobs
                        WHERE analysis_id = %s
                        ORDER BY created_at, job_id
                        """,
                        (analysis_id,),
                    ).fetchall()
                )
                browser_job_ids = tuple(
                    row[0]
                    for row in self._connection.execute(
                        """
                        SELECT job_id FROM archivability.browser_jobs
                        WHERE analysis_id = %s
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
                        SELECT evidence_id FROM archivability.evidence
                        WHERE analysis_id = %s
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
                        SELECT indicator_id FROM archivability.indicator_results
                        WHERE analysis_id = %s
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
                    return AnalysisReportSnapshot(
                        analysis=analysis,
                        attempts=attempts,
                        jobs=tuple(value for value in jobs if value is not None),
                        indicator_results=tuple(
                            value for value in results if value is not None
                        ),
                        evidence=tuple(
                            value for value in evidence if value is not None
                        ),
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
        except psycopg.Error as exc:
            raise PersistenceError("PostgreSQL report snapshot failed") from exc

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
        try:
            if result not in {"success", "failure"}:
                raise IntegrityViolation("analysis report result is invalid")
            if result == "success" and error_code is not None:
                raise IntegrityViolation(
                    "successful report access cannot have an error"
                )
            if result == "failure" and error_code not in {
                "ANALYSIS_NOT_FOUND",
                "REPORT_BUILD_FAILED",
            }:
                raise IntegrityViolation("report failure code is invalid")
            if state is not None and state not in {
                item.value for item in AnalysisState
            }:
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
        except psycopg.IntegrityError as exc:
            self._record_boundary_failure(
                "analysis_report", analysis_id, audit, exc, "access.analysis_report"
            )
            self._raise_integrity(exc)
        except PersistenceError as exc:
            self._record_boundary_failure(
                "analysis_report", analysis_id, audit, exc, "access.analysis_report"
            )
            raise

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
        try:
            if operation not in {"create", "read", "export"}:
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
                raise IntegrityViolation(
                    "analysis API audit event is inconsistent"
                )
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
        except psycopg.IntegrityError as exc:
            self._record_boundary_failure(
                "analysis_api", request_id, audit, exc, "access.analysis_api"
            )
            self._raise_integrity(exc)
        except PersistenceError as exc:
            self._record_boundary_failure(
                "analysis_api", request_id, audit, exc, "access.analysis_api"
            )
            raise

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
                route not in {
                    "analyses_collection",
                    "analysis_item",
                    "analysis_export",
                    "unmatched",
                }
                or method
                not in {
                    "GET",
                    "POST",
                    "PUT",
                    "PATCH",
                    "DELETE",
                    "HEAD",
                    "OPTIONS",
                    "OTHER",
                }
                or result != "failure"
                or (status_code, error_code) not in valid_errors
            ):
                raise IntegrityViolation(
                    "analysis HTTP audit event is inconsistent"
                )
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
        except psycopg.IntegrityError as exc:
            self._record_boundary_failure(
                "analysis_http_adapter",
                request_id,
                audit,
                exc,
                "access.analysis_http_adapter",
            )
            self._raise_integrity(exc)
        except PersistenceError as exc:
            self._record_boundary_failure(
                "analysis_http_adapter",
                request_id,
                audit,
                exc,
                "access.analysis_http_adapter",
            )
            raise

    def record_bearer_authentication_event(
        self,
        *,
        request_id: str,
        result: str,
        error_code: str | None,
        audit: AuditContext = AuditContext(),
    ) -> None:
        try:
            valid = (result == "success" and error_code is None) or (
                result == "failure" and error_code == "INVALID_TOKEN"
            )
            if not valid:
                raise IntegrityViolation(
                    "bearer authentication event is inconsistent"
                )
            with self._transaction():
                self._write_audit(
                    resource="authentication",
                    resource_id=request_id,
                    result=result,
                    audit=audit,
                    action="auth.bearer_token",
                    extra={"error_code": error_code},
                )
        except psycopg.IntegrityError as exc:
            self._record_boundary_failure(
                "authentication", request_id, audit, exc, "auth.bearer_token"
            )
            self._raise_integrity(exc)
        except PersistenceError as exc:
            self._record_boundary_failure(
                "authentication", request_id, audit, exc, "auth.bearer_token"
            )
            raise

    def record_application_lifecycle_event(
        self,
        *,
        instance_id: str,
        status: str,
        audit: AuditContext = AuditContext(),
    ) -> None:
        try:
            if status not in {"started", "stopped"}:
                raise IntegrityViolation(
                    "application lifecycle status is invalid"
                )
            with self._transaction():
                self._write_audit(
                    resource="application_instance",
                    resource_id=instance_id,
                    result="success",
                    audit=audit,
                    action="system.application_lifecycle",
                    extra={"status": status},
                )
        except psycopg.IntegrityError as exc:
            self._record_boundary_failure(
                "application_instance",
                instance_id,
                audit,
                exc,
                "system.application_lifecycle",
            )
            self._raise_integrity(exc)
        except PersistenceError as exc:
            self._record_boundary_failure(
                "application_instance",
                instance_id,
                audit,
                exc,
                "system.application_lifecycle",
            )
            raise

    def get_queue_metrics(
        self,
        *,
        audit: AuditContext = AuditContext(),
    ) -> QueueMetricsSnapshot:
        try:
            with self._transaction():
                row = self._connection.execute(
                    """
                    SELECT
                        count(*) FILTER (WHERE state = 'pending'),
                        count(*) FILTER (WHERE state = 'running'),
                        count(*) FILTER (WHERE state = 'succeeded'),
                        count(*) FILTER (WHERE state = 'failed'),
                        count(*) FILTER (
                            WHERE state = 'pending'
                              AND available_at <= CURRENT_TIMESTAMP
                        ),
                        count(*) FILTER (
                            WHERE state = 'running'
                              AND lease_expires_at <= CURRENT_TIMESTAMP
                        ),
                        GREATEST(
                            0,
                            COALESCE(
                                EXTRACT(EPOCH FROM (
                                    CURRENT_TIMESTAMP
                                    - min(created_at) FILTER (WHERE state = 'pending')
                                )),
                                0
                            )
                        )
                    FROM (
                        SELECT state, available_at, lease_expires_at, created_at
                        FROM archivability.collection_jobs
                        UNION ALL
                        SELECT state, available_at, lease_expires_at, created_at
                        FROM archivability.assessment_jobs
                    ) AS pipeline_jobs
                    """
                ).fetchone()
                if row is None:
                    raise IntegrityViolation("queue metrics query returned no row")
                try:
                    snapshot = QueueMetricsSnapshot(
                        pending=int(row[0]),
                        running=int(row[1]),
                        succeeded=int(row[2]),
                        failed=int(row[3]),
                        available=int(row[4]),
                        expired_leases=int(row[5]),
                        oldest_pending_seconds=float(row[6]),
                    )
                except (TypeError, ValueError) as exc:
                    raise IntegrityViolation(
                        "stored queue metrics failed validation"
                    ) from exc
                self._write_audit(
                    resource="assessment_queue_metrics",
                    resource_id="queue",
                    result="success",
                    audit=audit,
                    action="access.assessment_queue_metrics",
                    extra={
                        "pending": snapshot.pending,
                        "running": snapshot.running,
                        "available": snapshot.available,
                        "expired_leases": snapshot.expired_leases,
                    },
                )
                return snapshot
        except psycopg.IntegrityError as exc:
            self._record_boundary_failure(
                "assessment_queue_metrics",
                "queue",
                audit,
                exc,
                "access.assessment_queue_metrics",
            )
            self._raise_integrity(exc)
        except PersistenceError as exc:
            self._record_boundary_failure(
                "assessment_queue_metrics",
                "queue",
                audit,
                exc,
                "access.assessment_queue_metrics",
            )
            raise

    def record_assessment_worker_lifecycle_event(
        self,
        *,
        worker_id: str,
        status: str,
        processed_count: int,
        error_code: str | None = None,
        audit: AuditContext = AuditContext(),
    ) -> None:
        try:
            valid_status = status in {"started", "stopped", "failed"}
            valid_error = (status == "failed") == (error_code is not None)
            if (
                not valid_status
                or not valid_error
                or not isinstance(processed_count, int)
                or isinstance(processed_count, bool)
                or processed_count < 0
            ):
                raise IntegrityViolation(
                    "assessment worker lifecycle event is inconsistent"
                )
            with self._transaction():
                self._write_audit(
                    resource="assessment_worker",
                    resource_id=worker_id,
                    result="failure" if status == "failed" else "success",
                    audit=audit,
                    action="system.assessment_worker_lifecycle",
                    extra={
                        "status": status,
                        "processed_count": processed_count,
                        "error_code": error_code,
                    },
                )
        except psycopg.IntegrityError as exc:
            self._record_boundary_failure(
                "assessment_worker",
                worker_id,
                audit,
                exc,
                "system.assessment_worker_lifecycle",
            )
            self._raise_integrity(exc)
        except PersistenceError as exc:
            self._record_boundary_failure(
                "assessment_worker",
                worker_id,
                audit,
                exc,
                "system.assessment_worker_lifecycle",
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

    def _get_collection_job_for_analysis(
        self, analysis_id: str
    ) -> CollectionJob | None:
        row = self._connection.execute(
            """
            SELECT document_json, analysis_id, state, revision, attempt_count,
                   max_attempts, available_at, claimed_by, lease_expires_at,
                   completed_at, error_code, created_at, updated_at
            FROM archivability.collection_jobs WHERE analysis_id = %s
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
            FROM archivability.browser_jobs WHERE attempt_id = %s
            """,
            (attempt_id,),
        ).fetchone()
        return self._browser_job_from_row(row) if row else None

    def _browser_job_values(self, value: BrowserJob) -> tuple[Any, ...]:
        now = datetime.now(timezone.utc)
        return (
            value.job_id,
            value.analysis_id,
            value.attempt_id,
            value.state.value,
            value.attempt_count,
            value.max_attempts,
            value.revision,
            value.available_at,
            value.claimed_by,
            value.lease_expires_at,
            value.completed_at,
            value.error_code,
            Jsonb(value.to_dict()),
            value.created_at,
            now,
            value.updated_at,
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
            UPDATE archivability.browser_jobs
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
                current.available_at,
                current.claimed_by,
                current.lease_expires_at,
                current.completed_at,
                current.error_code,
                Jsonb(current.to_dict()),
                current.updated_at,
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
            document = row[0]
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
                available_at=row[7],
                claimed_by=row[8],
                lease_expires_at=row[9],
                completed_at=row[10],
                error_code=row[11],
                created_at=row[12],
                updated_at=row[13],
            )
        except (KeyError, TypeError, ValueError, JobError) as exc:
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

    def _collection_job_values(self, value: CollectionJob) -> tuple[Any, ...]:
        now = datetime.now(timezone.utc)
        return (
            value.job_id,
            value.analysis_id,
            value.state.value,
            value.attempt_count,
            value.max_attempts,
            value.revision,
            value.available_at,
            value.claimed_by,
            value.lease_expires_at,
            value.completed_at,
            value.error_code,
            Jsonb(value.to_dict()),
            value.created_at,
            now,
            value.updated_at,
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
            UPDATE archivability.collection_jobs
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
                current.available_at,
                current.claimed_by,
                current.lease_expires_at,
                current.completed_at,
                current.error_code,
                Jsonb(current.to_dict()),
                current.updated_at,
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
            document = row[0]
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
                available_at=row[6],
                claimed_by=row[7],
                lease_expires_at=row[8],
                completed_at=row[9],
                error_code=row[10],
                created_at=row[11],
                updated_at=row[12],
            )
        except (KeyError, TypeError, ValueError, JobError) as exc:
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

    def _record_boundary_failure(
        self,
        resource: str,
        resource_id: str,
        audit: AuditContext,
        error: Exception,
        action: str,
    ) -> None:
        self._record_failure(
            resource,
            resource_id,
            audit,
            error,
            action=action,
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
