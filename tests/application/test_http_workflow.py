from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
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
    HttpAssessmentQueueService,
    HttpAssessmentWorkflow,
    Observation,
    ProbeExecutionService,
    ProbeRunner,
    SqliteAssessmentJobRepository,
    SsrfPolicy,
    WorkflowError,
    apply_sqlite_migrations,
    load_methodology,
)


NOW = datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc)
SUBJECT = "https://example.org/private?token=must-not-be-logged"
PUBLIC_V4 = "93.184.216.34"
METHODOLOGY_PATH = ROOT / "methodology" / "v0.1.0"


class StaticResolver:
    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        del hostname, port
        return (PUBLIC_V4,)


class SuccessfulHttpProbe:
    probe_id = "http-metadata"
    tool_name = "workflow-test-probe"
    tool_version = "1.0.0"

    def execute(self, context):
        return (
            Observation.create(
                observation_id=f"observation-{context.request.attempt_id}",
                analysis_id=context.request.analysis_id,
                attempt_id=context.request.attempt_id,
                kind="http_metadata",
                subject_uri=context.request.subject_uri,
                observed_at=context.request.requested_at,
                probe_id=self.probe_id,
                tool_name=self.tool_name,
                tool_version=self.tool_version,
                payload_schema_version="1.1",
                payload={
                    "status_code": 200,
                    "headers": {"content-length": ["5"]},
                    "response_bytes_observed": 5,
                    "response_byte_limit": context.request.max_response_bytes,
                    "response_truncated": False,
                    "redirect_count": 0,
                    "final_transport_secure": True,
                },
            ),
        )


class FailingHttpProbe(SuccessfulHttpProbe):
    def execute(self, context):
        del context
        raise RuntimeError("token=must-not-be-logged")


class InvalidOutputProbe(SuccessfulHttpProbe):
    def execute(self, context):
        source = super().execute(context)[0]
        return (
            Observation.create(
                observation_id=source.observation_id,
                analysis_id=source.analysis_id,
                attempt_id=source.attempt_id,
                kind="incompatible",
                subject_uri=source.subject_uri,
                observed_at=source.observed_at,
                probe_id=source.probe_id,
                tool_name=source.tool_name,
                tool_version=source.tool_version,
                payload_schema_version="1.1",
                payload=source.payload,
            ),
        )


class WrongProbe(SuccessfulHttpProbe):
    probe_id = "other-probe"


class HttpAssessmentWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        apply_sqlite_migrations(self.connection)
        event_ids = iter(f"event-{index:03d}" for index in range(400))
        self.repository = SqliteAssessmentJobRepository(
            self.connection,
            clock=lambda: NOW,
            event_id_factory=lambda: next(event_ids),
        )
        self.methodology = load_methodology(METHODOLOGY_PATH)
        attempt_ids = iter(("attempt-1", "attempt-2", "attempt-3"))
        orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: NOW,
            analysis_id_factory=lambda: "analysis-1",
            attempt_id_factory=lambda: next(attempt_ids),
        )
        runner = ProbeRunner(
            policy=SsrfPolicy(),
            resolver=StaticResolver(),
            event_recorder=self.repository,
            authorizer=self.repository,
        )
        execution = ProbeExecutionService(
            runner=runner,
            repository=self.repository,
            clock=lambda: NOW,
        )
        queue = HttpAssessmentQueueService(
            repository=self.repository,
            clock=lambda: NOW,
            job_id_factory=lambda: "job-1",
        )
        self.workflow = HttpAssessmentWorkflow(
            methodology=self.methodology,
            repository=self.repository,
            orchestrator=orchestrator,
            probe_execution=execution,
            queue=queue,
            clock=lambda: NOW,
        )
        self.audit = AuditContext(user_id="user-opaque-1")

    def tearDown(self) -> None:
        self.connection.close()

    def test_success_coordinates_analysis_probe_and_durable_enqueue(self) -> None:
        outcome = self.workflow.start(
            subject_uri=SUBJECT,
            probe=SuccessfulHttpProbe(),
            audit=self.audit,
        )

        self.assertEqual("assessment_queued", outcome.status)
        self.assertEqual(AnalysisState.RUNNING, outcome.analysis.state)
        self.assertEqual(AttemptState.SUCCEEDED, outcome.execution.attempt.state)
        self.assertEqual("pending", outcome.enqueue.job.state.value)
        self.assertEqual(
            outcome.enqueue.job,
            self.repository.get_assessment_job(outcome.enqueue.job.job_id),
        )
        event = self.connection.execute(
            """
            SELECT result, extra_json FROM audit_events
            WHERE action = 'job.http_assessment_workflow'
            ORDER BY event_id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual("success", event[0])
        self.assertEqual("assessment_queued", json.loads(event[1])["status"])
        serialized = json.dumps(
            self.connection.execute("SELECT * FROM audit_events").fetchall()
        )
        self.assertNotIn("example.org", serialized)
        self.assertNotIn("must-not-be-logged", serialized)

    def test_probe_failure_exhausts_single_attempt_analysis(self) -> None:
        outcome = self.workflow.start(
            subject_uri=SUBJECT,
            probe=FailingHttpProbe(),
            analysis_max_attempts=1,
            audit=self.audit,
        )

        self.assertEqual("failed", outcome.status)
        self.assertEqual(AnalysisState.FAILED, outcome.analysis.state)
        self.assertEqual("PROBE_FAILED", outcome.execution.failure_code)
        self.assertEqual(
            0,
            self.connection.execute("SELECT count(*) FROM assessment_jobs").fetchone()[0],
        )

    def test_explicit_retry_can_enqueue_after_initial_probe_failure(self) -> None:
        first = self.workflow.start(
            subject_uri=SUBJECT,
            probe=FailingHttpProbe(),
            analysis_max_attempts=2,
            audit=self.audit,
        )
        second = self.workflow.retry(
            first.analysis.analysis_id,
            probe=SuccessfulHttpProbe(),
            audit=self.audit,
        )

        self.assertEqual("retry_available", first.status)
        self.assertEqual("assessment_queued", second.status)
        self.assertEqual(
            [AttemptState.FAILED, AttemptState.SUCCEEDED],
            [item.state for item in self.repository.list_attempts("analysis-1")],
        )

    def test_incompatible_probe_output_fails_before_observation_persistence(self) -> None:
        outcome = self.workflow.start(
            subject_uri=SUBJECT,
            probe=InvalidOutputProbe(),
            analysis_max_attempts=1,
            audit=self.audit,
        )

        self.assertEqual("failed", outcome.status)
        self.assertEqual(
            0,
            self.connection.execute("SELECT count(*) FROM observations").fetchone()[0],
        )
        self.assertEqual(
            0,
            self.connection.execute("SELECT count(*) FROM assessment_jobs").fetchone()[0],
        )

    def test_wrong_probe_is_rejected_before_creating_analysis(self) -> None:
        with self.assertRaises(WorkflowError):
            self.workflow.start(subject_uri=SUBJECT, probe=WrongProbe(), audit=self.audit)
        self.assertEqual(
            0,
            self.connection.execute("SELECT count(*) FROM analyses").fetchone()[0],
        )

    def test_retry_is_rejected_after_assessment_was_queued(self) -> None:
        queued = self.workflow.start(
            subject_uri=SUBJECT,
            probe=SuccessfulHttpProbe(),
            audit=self.audit,
        )

        with self.assertRaises(WorkflowError):
            self.workflow.retry(
                queued.analysis.analysis_id,
                probe=SuccessfulHttpProbe(),
                audit=self.audit,
            )
        self.assertEqual(1, len(self.repository.list_attempts("analysis-1")))


if __name__ == "__main__":
    unittest.main()
