from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from archivability import (  # noqa: E402
    AnalysisOrchestrator,
    AnalysisState,
    AttemptState,
    AuditContext,
    ConcurrencyConflict,
    HttpAssessmentQueueService,
    HttpAssessmentWorker,
    HttpMetadataAssessmentService,
    Observation,
    SqliteAssessmentJobRepository,
    apply_sqlite_migrations,
    load_methodology,
)
from archivability.jobs import succeed_assessment_job  # noqa: E402
from archivability.lifecycle import finish_attempt  # noqa: E402


T0 = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)
SUBJECT = "https://example.org/private?token=must-not-be-logged"
METHODOLOGY_PATH = ROOT / "methodology" / "v0.1.0"


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FailingAssessor:
    def assess(self, observation_id: str, *, audit: AuditContext):
        del observation_id, audit
        raise RuntimeError("token=must-not-be-logged")


def build_observation(analysis_id: str, attempt_id: str) -> Observation:
    return Observation.create(
        observation_id="observation-http-1",
        analysis_id=analysis_id,
        attempt_id=attempt_id,
        kind="http_metadata",
        subject_uri=SUBJECT,
        observed_at=T0 + timedelta(seconds=2),
        probe_id="http-metadata",
        tool_name="arquivabilidade-http",
        tool_version="0.1.0",
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


class AssessmentQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        apply_sqlite_migrations(self.connection)
        event_ids = iter(f"event-{index:03d}" for index in range(300))
        self.repository = SqliteAssessmentJobRepository(
            self.connection,
            clock=lambda: T0,
            event_id_factory=lambda: next(event_ids),
        )
        self.methodology = load_methodology(METHODOLOGY_PATH)
        lifecycle_times = iter((T0, T0 + timedelta(seconds=1)))
        orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: next(lifecycle_times),
            analysis_id_factory=lambda: "analysis-1",
            attempt_id_factory=lambda: "attempt-1",
        )
        analysis = orchestrator.create_analysis(
            subject_uri=SUBJECT,
            methodology=self.methodology,
        )
        attempt = orchestrator.start_attempt(analysis.analysis_id)
        self.observation = build_observation(analysis.analysis_id, attempt.attempt_id)
        succeeded = finish_attempt(
            attempt,
            target=AttemptState.SUCCEEDED,
            occurred_at=T0 + timedelta(seconds=2),
        )
        self.repository.complete_probe_attempt(
            attempt,
            succeeded,
            (self.observation,),
        )
        self.clock = MutableClock(T0 + timedelta(seconds=3))
        job_ids = iter(("job-1", "job-2", "job-3", "job-4"))
        self.queue = HttpAssessmentQueueService(
            repository=self.repository,
            clock=self.clock,
            job_id_factory=lambda: next(job_ids),
        )
        self.audit = AuditContext(user_id="user-opaque-1")

    def tearDown(self) -> None:
        self.connection.close()

    def test_enqueue_is_idempotent_and_does_not_audit_subject(self) -> None:
        first = self.queue.enqueue(self.observation.observation_id, audit=self.audit)
        second = self.queue.enqueue(self.observation.observation_id, audit=self.audit)

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.job, second.job)
        self.assertEqual(
            1,
            self.connection.execute("SELECT count(*) FROM assessment_jobs").fetchone()[0],
        )
        events = self.connection.execute(
            """
            SELECT action, result, after_json, extra_json
            FROM audit_events
            WHERE action = 'job.http_assessment_queue_enqueue'
            ORDER BY event_id
            """
        ).fetchall()
        self.assertEqual(2, len(events))
        self.assertEqual(
            ["created", "replayed"],
            [json.loads(row[3])["outcome"] for row in events],
        )
        serialized = json.dumps(events)
        self.assertNotIn("example.org", serialized)
        self.assertNotIn("must-not-be-logged", serialized)

    def test_claim_is_exclusive_and_expired_lease_is_recovered(self) -> None:
        self.queue.enqueue(self.observation.observation_id, audit=self.audit)
        first = self.repository.claim_next_assessment_job(
            worker_id="worker-1",
            occurred_at=self.clock.value,
            lease_expires_at=self.clock.value + timedelta(minutes=5),
            audit=self.audit,
        )
        blocked = self.repository.claim_next_assessment_job(
            worker_id="worker-2",
            occurred_at=self.clock.value + timedelta(minutes=1),
            lease_expires_at=self.clock.value + timedelta(minutes=6),
            audit=self.audit,
        )
        recovered = self.repository.claim_next_assessment_job(
            worker_id="worker-2",
            occurred_at=self.clock.value + timedelta(minutes=5),
            lease_expires_at=self.clock.value + timedelta(minutes=10),
            audit=self.audit,
        )

        self.assertEqual("worker-1", first.claimed_by)
        self.assertIsNone(blocked)
        self.assertEqual("worker-2", recovered.claimed_by)
        self.assertEqual(2, recovered.attempt_count)
        event = self.connection.execute(
            """
            SELECT extra_json FROM audit_events
            WHERE action = 'job.http_assessment_queue_claim'
            ORDER BY event_id DESC LIMIT 1
            """
        ).fetchone()
        self.assertTrue(json.loads(event[0])["recovered_expired_lease"])

        stale_completion = succeed_assessment_job(
            first,
            occurred_at=self.clock.value + timedelta(minutes=5),
        )
        with self.assertRaises(ConcurrencyConflict):
            self.repository.update_assessment_job(
                first,
                stale_completion,
                action="job.http_assessment_queue_succeeded",
                audit=self.audit,
            )

    def test_worker_retries_then_marks_job_failed_without_error_details(self) -> None:
        enqueued = self.queue.enqueue(
            self.observation.observation_id,
            max_attempts=2,
            audit=self.audit,
        )
        worker = HttpAssessmentWorker(
            worker_id="worker-1",
            repository=self.repository,
            assessor=FailingAssessor(),
            clock=self.clock,
            lease_duration=timedelta(minutes=1),
            retry_base_delay=timedelta(seconds=1),
        )

        first = worker.run_once(audit=self.audit)
        self.clock.value += timedelta(seconds=1)
        second = worker.run_once(audit=self.audit)

        self.assertEqual("retry_scheduled", first.status)
        self.assertEqual("failed", second.status)
        stored = self.repository.get_assessment_job(enqueued.job.job_id)
        self.assertEqual("failed", stored.state.value)
        self.assertEqual("ASSESSMENT_FAILED", stored.error_code)
        events = self.connection.execute(
            "SELECT action, result, extra_json FROM audit_events ORDER BY event_id"
        ).fetchall()
        serialized = json.dumps(events)
        self.assertIn("job.http_assessment_queue_retry", serialized)
        self.assertIn("job.http_assessment_queue_failed", serialized)
        self.assertNotIn("RuntimeError", serialized)
        self.assertNotIn("must-not-be-logged", serialized)

    def test_worker_runs_real_assessment_and_acknowledges_job(self) -> None:
        enqueued = self.queue.enqueue(self.observation.observation_id, audit=self.audit)
        assessor = HttpMetadataAssessmentService(
            methodology=self.methodology,
            repository=self.repository,
            clock=lambda: T0 + timedelta(seconds=4),
        )
        worker = HttpAssessmentWorker(
            worker_id="worker-1",
            repository=self.repository,
            assessor=assessor,
            clock=self.clock,
        )

        outcome = worker.run_once(audit=self.audit)

        self.assertEqual("succeeded", outcome.status)
        self.assertEqual(AnalysisState.COMPLETED, outcome.assessment.analysis.state)
        stored = self.repository.get_assessment_job(enqueued.job.job_id)
        self.assertEqual("succeeded", stored.state.value)
        self.assertIsNotNone(stored.completed_at)

    def test_job_rows_cannot_be_deleted(self) -> None:
        enqueued = self.queue.enqueue(self.observation.observation_id, audit=self.audit)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "DELETE FROM assessment_jobs WHERE job_id = ?",
                (enqueued.job.job_id,),
            )
        self.connection.rollback()


if __name__ == "__main__":
    unittest.main()
