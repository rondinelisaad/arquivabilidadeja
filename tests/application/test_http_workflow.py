from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from archivability import (
    AnalysisOrchestrator,
    AnalysisState,
    AuditContext,
    HttpAssessmentWorkflow,
    PersistenceError,
    ProbeLimits,
    SqliteAssessmentJobRepository,
    WorkflowError,
    apply_sqlite_migrations,
    load_methodology,
)
from archivability.jobs.collection import HttpCollectionQueueService

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
SUBJECT = "https://example.org/private?token=must-not-be-logged"


class MustNotRunProbe:
    probe_id = "http-metadata"
    tool_name = "test"
    tool_version = "1"

    def execute(self, context):
        del context
        raise AssertionError("the API request path must not execute network probes")


class SuccessfulHttpProbe(MustNotRunProbe):
    pass


class StaticResolver:
    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        del hostname, port
        return ("93.184.216.34",)


class HttpAssessmentWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        apply_sqlite_migrations(self.connection)
        self.repository = SqliteAssessmentJobRepository(
            self.connection,
            clock=lambda: NOW,
            event_id_factory=(f"event-{index}" for index in range(100)).__next__,
        )
        orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: NOW,
            analysis_id_factory=lambda: "analysis-1",
            attempt_id_factory=lambda: "attempt-unused",
        )
        self.workflow = HttpAssessmentWorkflow(
            methodology=load_methodology(ROOT / "methodology" / "v0.1.0"),
            orchestrator=orchestrator,
            collection_queue=HttpCollectionQueueService(
                self.repository,
                clock=lambda: NOW,
                job_id_factory=lambda: "collection-1",
            ),
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_submission_only_persists_and_queues_collection(self) -> None:
        outcome = self.workflow.start(
            subject_uri=SUBJECT,
            probe=MustNotRunProbe(),
            owner_user_id="user-opaque",
            audit=AuditContext(user_id="user-opaque", ip_address="192.0.2.10"),
        )

        self.assertEqual("collection_queued", outcome.status)
        self.assertEqual(AnalysisState.REQUESTED, outcome.analysis.state)
        self.assertEqual("pending", outcome.collection.job.state.value)
        self.assertEqual((), self.repository.list_attempts("analysis-1"))
        self.assertEqual(0, self.connection.execute("SELECT count(*) FROM observations").fetchone()[0])
        serialized = " ".join(
            row[0] or ""
            for row in self.connection.execute(
                "SELECT extra_json FROM audit_events ORDER BY event_id"
            ).fetchall()
        )
        self.assertNotIn("example.org", serialized)
        self.assertNotIn("must-not-be-logged", serialized)

    def test_invalid_limits_fail_before_creating_analysis(self) -> None:
        with self.assertRaises((WorkflowError, ValueError)):
            self.workflow.start(
                subject_uri=SUBJECT,
                limits=ProbeLimits(timeout_seconds=0.01),
            )
        self.assertEqual(0, self.connection.execute("SELECT count(*) FROM analyses").fetchone()[0])

    def test_collection_and_assessment_retry_budgets_are_distinct(self) -> None:
        outcome = self.workflow.start(
            subject_uri=SUBJECT,
            analysis_max_attempts=1,
            assessment_max_attempts=4,
        )

        self.assertEqual(1, outcome.collection.job.max_attempts)
        self.assertEqual(4, outcome.collection.job.assessment_max_attempts)

    def test_retries_are_owned_by_the_durable_worker(self) -> None:
        with self.assertRaises(WorkflowError):
            self.workflow.retry("analysis-1")

    def test_collection_jobs_cannot_be_deleted(self) -> None:
        self.workflow.start(subject_uri=SUBJECT)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM collection_jobs")

    def test_collection_insert_failure_rolls_back_new_analysis(self) -> None:
        self.workflow.start(subject_uri=SUBJECT)
        second = HttpAssessmentWorkflow(
            methodology=load_methodology(ROOT / "methodology" / "v0.1.0"),
            orchestrator=AnalysisOrchestrator(
                self.repository,
                clock=lambda: NOW,
                analysis_id_factory=lambda: "analysis-2",
            ),
            collection_queue=HttpCollectionQueueService(
                self.repository,
                clock=lambda: NOW,
                job_id_factory=lambda: "collection-1",
            ),
        )

        with self.assertRaises(PersistenceError):
            second.start(subject_uri="https://example.net/")

        self.assertIsNone(self.repository.get_analysis("analysis-2"))
        self.assertEqual(
            1,
            self.connection.execute("SELECT count(*) FROM analyses").fetchone()[0],
        )


if __name__ == "__main__":
    unittest.main()
