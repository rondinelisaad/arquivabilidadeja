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
    AnalysisReportService,
    AuditContext,
    HttpAssessmentQueueService,
    HttpAssessmentWorker,
    HttpAssessmentWorkflow,
    HttpMetadataAssessmentService,
    ProbeExecutionService,
    ProbeRunner,
    ReportNotFoundError,
    SqliteAssessmentJobRepository,
    SsrfPolicy,
    apply_sqlite_migrations,
    load_methodology,
)
from tests.application.test_http_workflow import (  # noqa: E402
    StaticResolver,
    SuccessfulHttpProbe,
)


NOW = datetime(2026, 10, 7, 5, 0, tzinfo=timezone.utc)
SUBJECT = "https://example.org/private/path?token=must-not-be-logged"
METHODOLOGY_PATH = ROOT / "methodology" / "v0.1.0"


class AnalysisReportServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        apply_sqlite_migrations(self.connection)
        event_ids = iter(f"event-{index:03d}" for index in range(500))
        self.repository = SqliteAssessmentJobRepository(
            self.connection,
            clock=lambda: NOW,
            event_id_factory=lambda: next(event_ids),
        )
        self.methodology = load_methodology(METHODOLOGY_PATH)
        orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: NOW,
            analysis_id_factory=lambda: "analysis-report-1",
            attempt_id_factory=lambda: "attempt-report-1",
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
        self.queue = HttpAssessmentQueueService(
            repository=self.repository,
            clock=lambda: NOW,
            job_id_factory=lambda: "job-report-1",
        )
        workflow = HttpAssessmentWorkflow(
            methodology=self.methodology,
            repository=self.repository,
            orchestrator=orchestrator,
            probe_execution=execution,
            queue=self.queue,
            clock=lambda: NOW,
        )
        self.audit = AuditContext(user_id="user-opaque-1")
        self.workflow_outcome = workflow.start(
            subject_uri=SUBJECT,
            probe=SuccessfulHttpProbe(),
            audit=self.audit,
        )
        self.service = AnalysisReportService(self.repository)

    def tearDown(self) -> None:
        self.connection.close()

    def test_queued_report_exposes_progress_without_sensitive_target_parts(self) -> None:
        report = self.service.get_report("analysis-report-1", audit=self.audit)
        document = report.to_dict()

        self.assertEqual("https://example.org/", report.target_origin)
        self.assertEqual("assessment_queued", report.progress.phase)
        self.assertEqual(1, report.progress.attempts_used)
        self.assertEqual("succeeded", report.attempts[0].state)
        self.assertEqual("pending", report.jobs[0].state)
        self.assertEqual([], document["indicator_results"])
        serialized = json.dumps(document)
        self.assertNotIn("private/path", serialized)
        self.assertNotIn("must-not-be-logged", serialized)
        self.assertNotIn("subject_uri", serialized)

    def test_completed_report_contains_results_and_hash_only_provenance(self) -> None:
        assessor = HttpMetadataAssessmentService(
            methodology=self.methodology,
            repository=self.repository,
            clock=lambda: NOW,
        )
        worker = HttpAssessmentWorker(
            worker_id="report-worker-1",
            repository=self.repository,
            assessor=assessor,
            clock=lambda: NOW,
        )
        worker.run_once(audit=self.audit)

        report = self.service.get_report("analysis-report-1", audit=self.audit)
        document = report.to_dict()

        self.assertEqual("completed", report.state)
        self.assertEqual("completed", report.progress.phase)
        self.assertEqual(["D01", "R06"], [item.indicator_id for item in report.indicator_results])
        self.assertEqual(2, len(report.evidence))
        self.assertTrue(all(item.sources for item in report.evidence))
        serialized = json.dumps(document)
        self.assertNotIn("headers", serialized)
        self.assertNotIn("payload", serialized)
        self.assertNotIn("private/path", serialized)
        self.assertNotIn("must-not-be-logged", serialized)

    def test_access_is_audited_without_report_content(self) -> None:
        self.service.get_report("analysis-report-1", audit=self.audit)

        event = self.connection.execute(
            """
            SELECT result, resource, resource_id, extra_json
            FROM audit_events
            WHERE action = 'access.analysis_report'
            ORDER BY event_id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual(("success", "analysis_report", "analysis-report-1"), event[:3])
        extra = json.loads(event[3])
        self.assertEqual("running", extra["state"])
        self.assertEqual(1, extra["attempt_count"])
        serialized = json.dumps(event)
        self.assertNotIn("example.org", serialized)
        self.assertNotIn("must-not-be-logged", serialized)

    def test_missing_report_is_audited_and_raises_stable_error(self) -> None:
        with self.assertRaises(ReportNotFoundError):
            self.service.get_report("missing-analysis", audit=self.audit)

        event = self.connection.execute(
            """
            SELECT result, extra_json FROM audit_events
            WHERE action = 'access.analysis_report'
            ORDER BY event_id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual("failure", event[0])
        self.assertEqual("ANALYSIS_NOT_FOUND", json.loads(event[1])["error_code"])


if __name__ == "__main__":
    unittest.main()
