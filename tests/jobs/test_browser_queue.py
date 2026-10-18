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

from archivability.browser import BrowserBudgets, BrowserProfile  # noqa: E402
from archivability.jobs import (  # noqa: E402
    AssessmentJobState,
    BrowserJob,
    BrowserQueueService,
    JobError,
    claim_browser_job,
    fail_browser_job,
    succeed_browser_job,
)
from archivability.lifecycle import (  # noqa: E402
    Analysis,
    AnalysisOrchestrator,
    AttemptState,
)
from archivability.lifecycle.state_machine import (  # noqa: E402
    finish_attempt,
    start_attempt,
)
from archivability.methodology import load_methodology  # noqa: E402
from archivability.storage import (  # noqa: E402
    AuditContext,
    IntegrityViolation,
    SqliteAssessmentJobRepository,
    apply_sqlite_migrations,
)

NOW = datetime(2026, 10, 19, 1, 0, tzinfo=timezone.utc)


class MemoryBrowserRepository:
    def __init__(self, analysis, attempt) -> None:
        self.analysis = analysis
        self.attempt = attempt
        self.job = None

    def get_analysis(self, analysis_id: str):
        return self.analysis if analysis_id == self.analysis.analysis_id else None

    def get_attempt(self, attempt_id: str):
        return self.attempt if attempt_id == self.attempt.attempt_id else None

    def enqueue_browser_job(self, job, *, audit: AuditContext):
        del audit
        if self.job is None:
            self.job = job
            return job, True
        return self.job, False


def running_analysis_with_successful_attempt():
    analysis = Analysis.create(
        analysis_id="analysis-browser-queue",
        subject_uri="https://example.org/",
        methodology_id="archivability-core",
        methodology_version="0.1.0",
        created_at=NOW,
        max_attempts=3,
    )
    analysis, attempt = start_attempt(
        analysis,
        attempt_id="attempt-browser-queue",
        occurred_at=NOW,
    )
    attempt = finish_attempt(
        attempt,
        target=AttemptState.SUCCEEDED,
        occurred_at=NOW + timedelta(seconds=1),
    )
    return analysis, attempt


class BrowserQueueTests(unittest.TestCase):
    def test_enqueue_is_idempotent_at_repository_boundary(self) -> None:
        analysis, attempt = running_analysis_with_successful_attempt()
        repository = MemoryBrowserRepository(analysis, attempt)
        identifiers = iter(("browser-job-1", "browser-job-replay"))
        queue = BrowserQueueService(
            repository,
            clock=lambda: NOW + timedelta(seconds=2),
            job_id_factory=lambda: next(identifiers),
        )

        first = queue.enqueue(analysis.analysis_id, attempt.attempt_id)
        second = queue.enqueue(analysis.analysis_id, attempt.attempt_id)

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.job, second.job)
        self.assertEqual("browser-job-1", second.job.job_id)

    def test_state_machine_claims_retries_and_finishes(self) -> None:
        job = BrowserJob.create(
            job_id="browser-job-1",
            analysis_id="analysis-1",
            attempt_id="attempt-1",
            created_at=NOW,
            budgets=BrowserBudgets(),
            profile=BrowserProfile(),
            max_attempts=2,
        )
        running = claim_browser_job(
            job,
            worker_id="browser-worker-1",
            occurred_at=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
        )
        retry = fail_browser_job(
            running,
            occurred_at=NOW + timedelta(seconds=1),
            error_code="BROWSER_RENDER_FAILED",
            retry_at=NOW + timedelta(seconds=31),
        )
        reclaimed = claim_browser_job(
            retry,
            worker_id="browser-worker-2",
            occurred_at=NOW + timedelta(seconds=31),
            lease_expires_at=NOW + timedelta(minutes=2),
        )
        succeeded = succeed_browser_job(
            reclaimed,
            occurred_at=NOW + timedelta(seconds=32),
        )

        self.assertEqual(AssessmentJobState.PENDING, retry.state)
        self.assertEqual(2, reclaimed.attempt_count)
        self.assertEqual(AssessmentJobState.SUCCEEDED, succeeded.state)

    def test_exhausted_job_is_terminal_and_unclaimable(self) -> None:
        job = BrowserJob.create(
            job_id="browser-job-1",
            analysis_id="analysis-1",
            attempt_id="attempt-1",
            created_at=NOW,
            max_attempts=1,
        )
        running = claim_browser_job(
            job,
            worker_id="browser-worker-1",
            occurred_at=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
        )
        failed = fail_browser_job(
            running,
            occurred_at=NOW + timedelta(seconds=1),
            error_code="BROWSER_RENDER_FAILED",
            retry_at=NOW + timedelta(seconds=31),
        )

        self.assertEqual(AssessmentJobState.FAILED, failed.state)
        with self.assertRaisesRegex(JobError, "not claimable"):
            claim_browser_job(
                failed,
                worker_id="browser-worker-2",
                occurred_at=NOW + timedelta(minutes=2),
                lease_expires_at=NOW + timedelta(minutes=3),
            )

    def test_enqueue_rejects_non_successful_attempt(self) -> None:
        analysis, attempt = start_attempt(
            Analysis.create(
                analysis_id="analysis-browser-queue",
                subject_uri="https://example.org/",
                methodology_id="archivability-core",
                methodology_version="0.1.0",
                created_at=NOW,
            ),
            attempt_id="attempt-browser-queue",
            occurred_at=NOW,
        )
        repository = MemoryBrowserRepository(analysis, attempt)
        queue = BrowserQueueService(repository, clock=lambda: NOW)

        with self.assertRaisesRegex(JobError, "successful HTTP attempt"):
            queue.enqueue(analysis.analysis_id, attempt.attempt_id)


class DurableBrowserQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        apply_sqlite_migrations(self.connection)
        event_ids = (f"event-browser-{index}" for index in range(100))
        self.repository = SqliteAssessmentJobRepository(
            self.connection,
            clock=lambda: NOW,
            event_id_factory=event_ids.__next__,
        )
        self.orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: NOW,
            analysis_id_factory=lambda: "analysis-browser-durable",
            attempt_id_factory=lambda: "attempt-browser-durable",
        )
        self.analysis = self.orchestrator.create_analysis(
            subject_uri="https://example.org/private?token=not-for-audit",
            methodology=load_methodology(ROOT / "methodology" / "v0.1.0"),
        )
        attempt = self.orchestrator.start_attempt(self.analysis.analysis_id)
        self.attempt = self.orchestrator.finish_attempt(
            self.analysis.analysis_id,
            attempt.attempt_id,
            target=AttemptState.SUCCEEDED,
        )
        self.queue = BrowserQueueService(
            self.repository,
            clock=lambda: NOW + timedelta(seconds=1),
            job_id_factory=lambda: "browser-job-durable",
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_round_trip_idempotency_leases_and_audit_are_sanitized(self) -> None:
        audit = AuditContext(user_id="browser-worker-user")
        first = self.queue.enqueue(
            self.analysis.analysis_id,
            self.attempt.attempt_id,
            max_attempts=2,
            audit=audit,
        )
        replay = self.queue.enqueue(
            self.analysis.analysis_id,
            self.attempt.attempt_id,
            max_attempts=2,
            audit=audit,
        )

        self.assertTrue(first.created)
        self.assertFalse(replay.created)
        self.assertEqual(first.job, replay.job)
        claimed = self.repository.claim_next_browser_job(
            worker_id="browser-worker-1",
            occurred_at=NOW + timedelta(seconds=2),
            lease_expires_at=NOW + timedelta(minutes=1),
            audit=audit,
        )
        assert claimed is not None
        retry = fail_browser_job(
            claimed,
            occurred_at=NOW + timedelta(seconds=3),
            error_code="BROWSER_RENDER_FAILED",
            retry_at=NOW + timedelta(seconds=4),
        )
        self.repository.update_browser_job(
            claimed,
            retry,
            action="job.browser_queue_retry",
            audit=audit,
        )
        reclaimed = self.repository.claim_next_browser_job(
            worker_id="browser-worker-2",
            occurred_at=NOW + timedelta(seconds=4),
            lease_expires_at=NOW + timedelta(minutes=2),
            audit=audit,
        )
        assert reclaimed is not None
        succeeded = succeed_browser_job(
            reclaimed,
            occurred_at=NOW + timedelta(seconds=5),
        )
        self.repository.update_browser_job(
            reclaimed,
            succeeded,
            action="job.browser_queue_succeeded",
            audit=audit,
        )

        self.assertEqual(succeeded, self.repository.get_browser_job(first.job.job_id))
        events = self.connection.execute(
            """
            SELECT action, before_json, after_json, extra_json
            FROM audit_events WHERE resource = 'browser_job'
            ORDER BY event_id
            """
        ).fetchall()
        serialized = json.dumps(events)
        self.assertIn("job.browser_queue_enqueue", serialized)
        self.assertIn("job.browser_queue_claim", serialized)
        self.assertIn("job.browser_queue_succeeded", serialized)
        self.assertNotIn("example.org", serialized)
        self.assertNotIn("not-for-audit", serialized)

    def test_replay_with_changed_contract_is_rejected(self) -> None:
        self.queue.enqueue(self.analysis.analysis_id, self.attempt.attempt_id)

        with self.assertRaisesRegex(
            IntegrityViolation, "replay conflicts with existing job"
        ):
            self.queue.enqueue(
                self.analysis.analysis_id,
                self.attempt.attempt_id,
                budgets=BrowserBudgets(max_requests=10),
            )


if __name__ == "__main__":
    unittest.main()
