from __future__ import annotations

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
from archivability.lifecycle import Analysis, AttemptState  # noqa: E402
from archivability.lifecycle.state_machine import (  # noqa: E402
    finish_attempt,
    start_attempt,
)
from archivability.storage import AuditContext  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
