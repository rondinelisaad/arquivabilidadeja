from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from archivability import (
    AnalysisOrchestrator,
    AnalysisState,
    AuditContext,
    BrowserQueueService,
    HttpAssessmentQueueService,
    HttpAssessmentWorker,
    HttpAssessmentWorkflow,
    HttpMetadataAssessmentService,
    Observation,
    ProbeExecutionService,
    ProbeRunner,
    SqliteAssessmentJobRepository,
    SsrfPolicy,
    apply_sqlite_migrations,
    load_methodology,
)
from archivability.jobs.collection import (
    HttpCollectionQueueService,
    HttpCollectionWorker,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 18, 14, 0, tzinfo=timezone.utc)
SUBJECT = "https://example.org/private?secret=must-not-be-logged"


class StaticResolver:
    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        del hostname, port
        return ("93.184.216.34",)


class SuccessfulProbe:
    probe_id = "http-metadata"
    tool_name = "test-http"
    tool_version = "1.0"

    def execute(self, context):
        request = context.request
        return (
            Observation.create(
                observation_id="observation-1",
                analysis_id=request.analysis_id,
                attempt_id=request.attempt_id,
                kind="http_metadata",
                subject_uri=request.subject_uri,
                observed_at=NOW,
                probe_id=self.probe_id,
                tool_name=self.tool_name,
                tool_version=self.tool_version,
                payload_schema_version="1.1",
                payload={
                    "status_code": 200,
                    "headers": {"content-length": ["4"]},
                    "response_bytes_observed": 4,
                    "response_byte_limit": 1024,
                    "response_truncated": False,
                    "redirect_count": 0,
                    "final_transport_secure": True,
                },
            ),
        )


class FailingProbe(SuccessfulProbe):
    def execute(self, context):
        del context
        raise RuntimeError("sensitive upstream failure")


class CountingProbe(SuccessfulProbe):
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, context):
        self.calls += 1
        return super().execute(context)


class FailingAssessmentQueue:
    def enqueue(self, observation_id: str, *, max_attempts: int, audit: AuditContext):
        del observation_id, max_attempts, audit
        raise RuntimeError("simulated queue interruption")


class CollectionQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        apply_sqlite_migrations(self.connection)
        event_ids = (f"event-{index}" for index in range(500))
        self.repository = SqliteAssessmentJobRepository(
            self.connection,
            clock=lambda: NOW,
            event_id_factory=event_ids.__next__,
        )
        attempt_ids = (f"attempt-{index}" for index in range(1, 10))
        self.orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: NOW,
            analysis_id_factory=lambda: "analysis-1",
            attempt_id_factory=attempt_ids.__next__,
        )
        self.methodology = load_methodology(ROOT / "methodology" / "v0.1.0")
        self.workflow = HttpAssessmentWorkflow(
            methodology=self.methodology,
            orchestrator=self.orchestrator,
            collection_queue=HttpCollectionQueueService(
                self.repository,
                clock=lambda: NOW,
                job_id_factory=lambda: "collection-1",
            ),
        )

    def tearDown(self) -> None:
        self.connection.close()

    def _worker(
        self,
        probe,
        *,
        clock=lambda: NOW,
        assessment_queue=None,
        browser_queue=None,
    ) -> HttpCollectionWorker:
        downstream = (
            {"browser_queue": browser_queue}
            if browser_queue is not None
            else {
                "assessment_queue": assessment_queue
                or HttpAssessmentQueueService(
                    repository=self.repository,
                    clock=clock,
                    job_id_factory=lambda: "assessment-1",
                )
            }
        )
        return HttpCollectionWorker(
            worker_id="collector-1",
            repository=self.repository,
            orchestrator=self.orchestrator,
            execution=ProbeExecutionService(
                runner=ProbeRunner(
                    policy=SsrfPolicy(),
                    resolver=StaticResolver(),
                    event_recorder=self.repository,
                    authorizer=self.repository,
                ),
                repository=self.repository,
                clock=clock,
            ),
            probe=probe,
            clock=clock,
            lease_duration=timedelta(minutes=1),
            retry_base_delay=timedelta(seconds=1),
            **downstream,
        )

    def test_collection_can_handoff_to_durable_browser_queue(self) -> None:
        self.workflow.start(subject_uri=SUBJECT)
        browser_queue = BrowserQueueService(
            self.repository,
            clock=lambda: NOW,
            job_id_factory=lambda: "browser-after-collection",
        )

        collected = self._worker(
            SuccessfulProbe(),
            browser_queue=browser_queue,
        ).run_once()

        self.assertEqual("succeeded", collected.status)
        self.assertEqual(
            ("pending", "attempt-1"),
            self.connection.execute(
                "SELECT state, attempt_id FROM browser_jobs WHERE analysis_id = ?",
                ("analysis-1",),
            ).fetchone(),
        )
        self.assertEqual(
            0,
            self.connection.execute("SELECT count(*) FROM assessment_jobs").fetchone()[0],
        )

    def test_submission_collection_and_derivation_are_separate_durable_steps(self) -> None:
        submitted = self.workflow.start(
            subject_uri=SUBJECT,
            owner_user_id="user-1",
            audit=AuditContext(user_id="user-1"),
        )
        self.assertEqual("collection_queued", submitted.status)
        self.assertEqual(0, self.connection.execute("SELECT count(*) FROM observations").fetchone()[0])

        collected = self._worker(SuccessfulProbe()).run_once(audit=AuditContext())
        self.assertEqual("succeeded", collected.status)
        self.assertEqual(1, self.connection.execute("SELECT count(*) FROM observations").fetchone()[0])
        assessment = self.connection.execute(
            "SELECT state FROM assessment_jobs WHERE analysis_id = ?", ("analysis-1",)
        ).fetchone()
        self.assertEqual(("pending",), assessment)

        assessed = HttpAssessmentWorker(
            worker_id="assessor-1",
            repository=self.repository,
            assessor=HttpMetadataAssessmentService(
                methodology=self.methodology,
                repository=self.repository,
            ),
            clock=lambda: NOW,
        ).run_once(audit=AuditContext())
        self.assertEqual("succeeded", assessed.status)
        self.assertEqual(AnalysisState.COMPLETED, self.repository.get_analysis("analysis-1").state)

        audit_json = json.dumps(
            self.connection.execute(
                "SELECT action, extra_json FROM audit_events ORDER BY event_id"
            ).fetchall()
        )
        self.assertIn("job.http_collection_queue_enqueue", audit_json)
        self.assertIn("job.http_collection_queue_claim", audit_json)
        self.assertIn("job.http_collection_queue_succeeded", audit_json)
        self.assertNotIn("example.org", audit_json)
        self.assertNotIn("must-not-be-logged", audit_json)

    def test_exhausted_collection_failure_finalizes_analysis(self) -> None:
        self.workflow.start(
            subject_uri=SUBJECT,
            analysis_max_attempts=1,
            assessment_max_attempts=1,
        )
        outcome = self._worker(FailingProbe()).run_once(audit=AuditContext())
        self.assertEqual("failed", outcome.status)
        self.assertEqual(AnalysisState.FAILED, self.repository.get_analysis("analysis-1").state)
        stored = self.repository.get_collection_job("collection-1")
        self.assertEqual("PROBE_FAILED", stored.error_code)

    def test_retry_reuses_observation_after_assessment_enqueue_interruption(self) -> None:
        self.workflow.start(subject_uri=SUBJECT)
        probe = CountingProbe()

        interrupted = self._worker(
            probe,
            assessment_queue=FailingAssessmentQueue(),
        ).run_once()
        self.assertEqual("retry_scheduled", interrupted.status)
        self.assertEqual(1, probe.calls)
        self.assertEqual(1, self.connection.execute("SELECT count(*) FROM observations").fetchone()[0])

        later = NOW + timedelta(seconds=2)
        resumed = self._worker(probe, clock=lambda: later).run_once()
        self.assertEqual("succeeded", resumed.status)
        self.assertEqual(1, probe.calls)
        self.assertEqual(1, self.connection.execute("SELECT count(*) FROM observations").fetchone()[0])
        self.assertEqual(
            ("pending",),
            self.connection.execute(
                "SELECT state FROM assessment_jobs WHERE analysis_id = ?",
                ("analysis-1",),
            ).fetchone(),
        )

    def test_expired_lease_closes_stale_attempt_before_recollection(self) -> None:
        self.workflow.start(subject_uri=SUBJECT)
        claimed = self.repository.claim_next_collection_job(
            worker_id="interrupted-collector",
            occurred_at=NOW,
            lease_expires_at=NOW + timedelta(seconds=1),
        )
        self.assertIsNotNone(claimed)
        stale = self.orchestrator.start_attempt("analysis-1")

        later = NOW + timedelta(seconds=2)
        outcome = self._worker(SuccessfulProbe(), clock=lambda: later).run_once()

        self.assertEqual("succeeded", outcome.status)
        attempts = self.repository.list_attempts("analysis-1")
        self.assertEqual(2, len(attempts))
        self.assertEqual(stale.attempt_id, attempts[0].attempt_id)
        self.assertEqual("failed", attempts[0].state.value)
        self.assertEqual("COLLECTION_LEASE_EXPIRED", attempts[0].failure_code)
        self.assertEqual("succeeded", attempts[1].state.value)


if __name__ == "__main__":
    unittest.main()
