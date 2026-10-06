from __future__ import annotations

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
    AttemptState,
    AuditContext,
    ConcurrencyConflict,
    HttpAssessmentQueueService,
    Observation,
    PostgreSqlAssessmentJobRepository,
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

    def _successful_observation(self) -> Observation:
        identifier = uuid4().hex
        orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: T0,
            analysis_id_factory=lambda: f"analysis-{identifier}",
            attempt_id_factory=lambda: f"attempt-{identifier}",
        )
        analysis = orchestrator.create_analysis(
            subject_uri="https://example.org/",
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
            payload_schema_version="1.0",
            payload={"status_code": 200},
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
