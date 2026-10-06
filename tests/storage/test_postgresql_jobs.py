from __future__ import annotations

import json
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import psycopg  # noqa: E402

from archivability import (  # noqa: E402
    AnalysisOrchestrator,
    AnalysisReportService,
    AttemptState,
    AuditContext,
    ConcurrencyConflict,
    HttpAssessmentQueueService,
    HttpMetadataAssessmentService,
    IntegrityViolation,
    Observation,
    PostgreSqlAssessmentJobRepository,
    ReportNotFoundError,
    apply_postgresql_migrations,
    load_methodology,
)
from archivability.jobs import (  # noqa: E402
    fail_assessment_job,
    succeed_assessment_job,
)
from archivability.lifecycle import finish_attempt  # noqa: E402


POSTGRES_DSN = os.environ.get("ARCHIVABILITY_TEST_POSTGRES_DSN")
POSTGRES_RUNTIME_DSN = os.environ.get(
    "ARCHIVABILITY_TEST_POSTGRES_RUNTIME_DSN", POSTGRES_DSN
)
T0 = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


@unittest.skipUnless(POSTGRES_DSN, "PostgreSQL integration DSN is not configured")
class PostgreSqlAssessmentJobIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert POSTGRES_DSN is not None
        with psycopg.connect(POSTGRES_DSN) as migration_connection:
            apply_postgresql_migrations(migration_connection)

    def setUp(self) -> None:
        assert POSTGRES_RUNTIME_DSN is not None
        self.connection = psycopg.connect(POSTGRES_RUNTIME_DSN, autocommit=True)
        self.repository = self._repository(self.connection)
        self.audit = AuditContext(user_id=f"worker-user-{uuid4().hex}")

    def tearDown(self) -> None:
        self.connection.close()

    def test_enqueue_is_idempotent(self) -> None:
        observation = self._successful_observation()
        queue = HttpAssessmentQueueService(
            repository=self.repository,
            clock=lambda: T0 + timedelta(seconds=3),
            job_id_factory=lambda: f"job-{uuid4().hex}",
        )

        first = queue.enqueue(observation.observation_id, audit=self.audit)
        second = queue.enqueue(observation.observation_id, audit=self.audit)

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.job, second.job)
        outcomes = self.connection.execute(
            """
            SELECT extra_json->>'outcome'
            FROM archivability.audit_events
            WHERE action = 'job.http_assessment_queue_enqueue'
              AND resource_id = %s
            ORDER BY timestamp, event_id
            """,
            (first.job.job_id,),
        ).fetchall()
        self.assertEqual([("created",), ("replayed",)], outcomes)

        claimed = self.repository.claim_next_assessment_job(
            worker_id="worker-idempotency",
            occurred_at=T0 + timedelta(seconds=4),
            lease_expires_at=T0 + timedelta(minutes=1),
        )
        assert claimed is not None
        self.repository.update_assessment_job(
            claimed,
            succeed_assessment_job(
                claimed, occurred_at=T0 + timedelta(seconds=5)
            ),
            action="job.http_assessment_queue_succeeded",
        )

    def test_expired_lease_is_reclaimed_by_another_worker(self) -> None:
        observation = self._successful_observation()
        queue = HttpAssessmentQueueService(
            repository=self.repository,
            clock=lambda: T0 + timedelta(seconds=3),
            job_id_factory=lambda: f"job-{uuid4().hex}",
        )
        enqueued = queue.enqueue(observation.observation_id, max_attempts=2).job
        first = self.repository.claim_next_assessment_job(
            worker_id="worker-lease-owner",
            occurred_at=T0 + timedelta(seconds=4),
            lease_expires_at=T0 + timedelta(seconds=5),
        )
        assert first is not None

        reclaimed = self.repository.claim_next_assessment_job(
            worker_id="worker-recovery",
            occurred_at=T0 + timedelta(seconds=6),
            lease_expires_at=T0 + timedelta(minutes=1),
        )

        assert reclaimed is not None
        self.assertEqual(enqueued.job_id, reclaimed.job_id)
        self.assertEqual("worker-recovery", reclaimed.claimed_by)
        self.assertEqual(2, reclaimed.attempt_count)
        self.repository.update_assessment_job(
            reclaimed,
            succeed_assessment_job(
                reclaimed, occurred_at=T0 + timedelta(seconds=7)
            ),
            action="job.http_assessment_queue_succeeded",
        )

    def test_concurrent_workers_claim_distinct_jobs(self) -> None:
        jobs = []
        for _ in range(2):
            observation = self._successful_observation()
            queue = HttpAssessmentQueueService(
                repository=self.repository,
                clock=lambda: T0 + timedelta(seconds=3),
                job_id_factory=lambda: f"job-{uuid4().hex}",
            )
            jobs.append(queue.enqueue(observation.observation_id).job)

        def claim(worker_id: str):
            assert POSTGRES_RUNTIME_DSN is not None
            with psycopg.connect(POSTGRES_RUNTIME_DSN, autocommit=True) as connection:
                repository = self._repository(connection)
                return repository.claim_next_assessment_job(
                    worker_id=worker_id,
                    occurred_at=T0 + timedelta(seconds=4),
                    lease_expires_at=T0 + timedelta(minutes=5),
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            claimed = list(executor.map(claim, ("worker-1", "worker-2")))

        self.assertTrue(all(job is not None for job in claimed))
        self.assertEqual(2, len({job.job_id for job in claimed if job is not None}))
        self.assertEqual({job.job_id for job in jobs}, {job.job_id for job in claimed})
        for job in claimed:
            assert job is not None
            self.repository.update_assessment_job(
                job,
                succeed_assessment_job(
                    job, occurred_at=T0 + timedelta(seconds=5)
                ),
                action="job.http_assessment_queue_succeeded",
            )

    def test_retry_recovery_and_ack_use_optimistic_revisions(self) -> None:
        observation = self._successful_observation()
        queue = HttpAssessmentQueueService(
            repository=self.repository,
            clock=lambda: T0 + timedelta(seconds=3),
            job_id_factory=lambda: f"job-{uuid4().hex}",
        )
        enqueued = queue.enqueue(
            observation.observation_id, max_attempts=2, audit=self.audit
        ).job
        claimed = self.repository.claim_next_assessment_job(
            worker_id="worker-1",
            occurred_at=T0 + timedelta(seconds=4),
            lease_expires_at=T0 + timedelta(minutes=1),
            audit=self.audit,
        )
        assert claimed is not None
        retry = fail_assessment_job(
            claimed,
            occurred_at=T0 + timedelta(seconds=5),
            error_code="ASSESSMENT_FAILED",
            retry_at=T0 + timedelta(seconds=6),
        )
        self.repository.update_assessment_job(
            claimed,
            retry,
            action="job.http_assessment_queue_retry",
            audit=self.audit,
        )
        with self.assertRaises(ConcurrencyConflict):
            self.repository.update_assessment_job(
                claimed,
                retry,
                action="job.http_assessment_queue_retry",
                audit=self.audit,
            )
        reclaimed = self.repository.claim_next_assessment_job(
            worker_id="worker-2",
            occurred_at=T0 + timedelta(seconds=6),
            lease_expires_at=T0 + timedelta(minutes=1),
            audit=self.audit,
        )
        assert reclaimed is not None
        succeeded = succeed_assessment_job(
            reclaimed, occurred_at=T0 + timedelta(seconds=7)
        )
        self.repository.update_assessment_job(
            reclaimed,
            succeeded,
            action="job.http_assessment_queue_succeeded",
            audit=self.audit,
        )

        stored = self.repository.get_assessment_job(enqueued.job_id)
        self.assertEqual(succeeded, stored)
        self.assertEqual(2, stored.attempt_count)

    def test_report_snapshot_and_access_audit_are_sanitized(self) -> None:
        observation = self._successful_observation()
        queue = HttpAssessmentQueueService(
            repository=self.repository,
            clock=lambda: T0 + timedelta(seconds=3),
            job_id_factory=lambda: f"job-{uuid4().hex}",
        )
        queued = queue.enqueue(observation.observation_id, audit=self.audit).job
        service = AnalysisReportService(self.repository)

        report = service.get_report(observation.analysis_id, audit=self.audit)

        self.assertEqual("https://example.org/", report.target_origin)
        self.assertEqual("assessment_queued", report.progress.phase)
        self.assertEqual((queued.job_id,), tuple(item.job_id for item in report.jobs))
        event = self.connection.execute(
            """
            SELECT result, extra_json
            FROM archivability.audit_events
            WHERE action = 'access.analysis_report' AND resource_id = %s
            ORDER BY timestamp DESC, event_id DESC
            LIMIT 1
            """,
            (observation.analysis_id,),
        ).fetchone()
        assert event is not None
        self.assertEqual("success", event[0])
        self.assertEqual(1, event[1]["job_count"])
        serialized = json.dumps(event[1])
        self.assertNotIn("example.org", serialized)
        self.assertNotIn("private/path", serialized)
        self.assertNotIn("subject_uri", serialized)

        claimed = self.repository.claim_next_assessment_job(
            worker_id="worker-report",
            occurred_at=T0 + timedelta(seconds=4),
            lease_expires_at=T0 + timedelta(minutes=1),
        )
        assert claimed is not None
        assessor = HttpMetadataAssessmentService(
            methodology=load_methodology(ROOT / "methodology" / "v0.1.0"),
            repository=self.repository,
            clock=lambda: T0 + timedelta(seconds=5),
        )
        assessor.assess(observation.observation_id, audit=self.audit)
        self.repository.update_assessment_job(
            claimed,
            succeed_assessment_job(
                claimed, occurred_at=T0 + timedelta(seconds=6)
            ),
            action="job.http_assessment_queue_succeeded",
        )
        completed = service.get_report(observation.analysis_id, audit=self.audit)
        self.assertEqual("completed", completed.state)
        self.assertEqual(2, len(completed.indicator_results))
        self.assertEqual(2, len(completed.evidence))

        with self.assertRaises(ReportNotFoundError):
            service.get_report(f"missing-{uuid4().hex}", audit=self.audit)

    def test_queue_metrics_are_aggregated_and_access_is_audited(self) -> None:
        snapshot = self.repository.get_queue_metrics(audit=self.audit)

        self.assertGreaterEqual(snapshot.pending, 0)
        self.assertGreaterEqual(snapshot.oldest_pending_seconds, 0)
        event = self.connection.execute(
            """
            SELECT result, resource_id, extra_json
            FROM archivability.audit_events
            WHERE action = 'access.assessment_queue_metrics'
              AND user_id = %s
            ORDER BY timestamp DESC, event_id DESC
            LIMIT 1
            """,
            (self.audit.user_id,),
        ).fetchone()
        assert event is not None
        self.assertEqual(("success", "queue"), event[:2])
        self.assertEqual(snapshot.pending, event[2]["pending"])
        self.assertNotIn("analysis_id", event[2])

    def test_web_boundary_events_are_validated_and_sanitized(self) -> None:
        suffix = uuid4().hex
        api_request_id = f"request-api-{suffix}"
        http_request_id = f"request-http-{suffix}"
        auth_request_id = f"request-auth-{suffix}"
        self.repository.record_analysis_api_event(
            request_id=api_request_id,
            operation="create",
            result="success",
            status_code=202,
            error_code=None,
            analysis_id="analysis-opaque-1",
            audit=self.audit,
        )
        self.repository.record_analysis_http_event(
            request_id=http_request_id,
            route="unmatched",
            method="GET",
            result="failure",
            status_code=404,
            error_code="NOT_FOUND",
            audit=self.audit,
        )
        self.repository.record_bearer_authentication_event(
            request_id=auth_request_id,
            result="failure",
            error_code="INVALID_TOKEN",
            audit=self.audit,
        )

        events = self.connection.execute(
            """
            SELECT action, resource_id, result, extra_json
            FROM archivability.audit_events
            WHERE resource_id IN (%s, %s, %s)
            ORDER BY resource_id
            """,
            (api_request_id, http_request_id, auth_request_id),
        ).fetchall()
        self.assertEqual(3, len(events))
        serialized = json.dumps(events, default=str)
        self.assertNotIn("authorization", serialized.lower())
        self.assertNotIn("https://", serialized)

        invalid_request_id = f"request-invalid-{suffix}"
        with self.assertRaises(IntegrityViolation):
            self.repository.record_analysis_http_event(
                request_id=invalid_request_id,
                route="/private/path",
                method="GET",
                result="failure",
                status_code=404,
                error_code="NOT_FOUND",
                audit=self.audit,
            )
        failure = self.connection.execute(
            """
            SELECT action, result, extra_json
            FROM archivability.audit_events
            WHERE resource_id = %s
            """,
            (invalid_request_id,),
        ).fetchone()
        assert failure is not None
        self.assertEqual(("access.analysis_http_adapter", "failure"), failure[:2])
        self.assertNotIn("private/path", json.dumps(failure[2]))

    def _successful_observation(self) -> Observation:
        identifier = uuid4().hex
        orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: T0,
            analysis_id_factory=lambda: f"analysis-{identifier}",
            attempt_id_factory=lambda: f"attempt-{identifier}",
        )
        analysis = orchestrator.create_analysis(
            subject_uri="https://example.org/private/path?token=not-for-audit",
            methodology=load_methodology(ROOT / "methodology" / "v0.1.0"),
        )
        attempt = orchestrator.start_attempt(analysis.analysis_id)
        observation = Observation.create(
            observation_id=f"observation-{identifier}",
            analysis_id=analysis.analysis_id,
            attempt_id=attempt.attempt_id,
            kind="http_metadata",
            subject_uri=analysis.subject_uri,
            observed_at=T0,
            probe_id="http-metadata",
            tool_name="integration-probe",
            tool_version="1.0.0",
            payload_schema_version="1.1",
            payload={
                "status_code": 200,
                "headers": {"content-length": ["5"]},
                "response_bytes_observed": 5,
                "response_byte_limit": 1024,
                "response_truncated": False,
                "redirect_count": 0,
                "final_transport_secure": True,
            },
        )
        succeeded = finish_attempt(
            attempt,
            target=AttemptState.SUCCEEDED,
            occurred_at=T0 + timedelta(seconds=1),
        )
        self.repository.complete_probe_attempt(attempt, succeeded, (observation,))
        return observation

    @staticmethod
    def _repository(connection) -> PostgreSqlAssessmentJobRepository:
        prefix = uuid4().hex
        sequence = iter(range(1000))
        return PostgreSqlAssessmentJobRepository(
            connection,
            clock=lambda: T0,
            event_id_factory=lambda: f"{prefix}-{next(sequence)}",
        )


if __name__ == "__main__":
    unittest.main()
