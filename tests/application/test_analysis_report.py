from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from archivability import (  # noqa: E402
    AnalysisOrchestrator,
    AnalysisReportService,
    AuditContext,
    BrowserBudgets,
    BrowserIsolationAttestation,
    BrowserProfile,
    BrowserQueueService,
    BrowserRenderAggregate,
    HttpAssessmentQueueService,
    HttpAssessmentWorker,
    HttpAssessmentWorkflow,
    HttpCollectionQueueService,
    HttpCollectionWorker,
    HttpMetadataAssessmentService,
    Observation,
    ProbeExecutionService,
    ProbeRunner,
    ReportError,
    ReportNotFoundError,
    SqliteAssessmentJobRepository,
    SsrfPolicy,
    apply_sqlite_migrations,
    load_methodology,
)

NOW = datetime(2026, 10, 7, 5, 0, tzinfo=timezone.utc)
SUBJECT = "https://example.org/private/path?token=must-not-be-logged"
METHODOLOGY_PATH = ROOT / "methodology" / "v0.1.0"


class StaticResolver:
    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        del hostname, port
        return ("93.184.216.34",)


class SuccessfulHttpProbe:
    probe_id = "http-metadata"
    tool_name = "test"
    tool_version = "1"

    def execute(self, context):
        request = context.request
        return (
            Observation.create(
                observation_id="observation-report-1",
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
                    "response_byte_limit": 5 * 1024 * 1024,
                    "response_truncated": False,
                    "redirect_count": 0,
                    "final_transport_secure": True,
                },
            ),
        )


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
        self.execution = ProbeExecutionService(
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
            orchestrator=orchestrator,
            collection_queue=HttpCollectionQueueService(
                self.repository,
                clock=lambda: NOW,
                job_id_factory=lambda: "collection-report-1",
            ),
        )
        self.orchestrator = orchestrator
        self.audit = AuditContext(user_id="user-opaque-1")
        self.workflow_outcome = workflow.start(
            subject_uri=SUBJECT,
            probe=SuccessfulHttpProbe(),
            owner_user_id="user-opaque-1",
            audit=self.audit,
        )
        self.service = AnalysisReportService(self.repository, self.methodology)

    def _run_collection(self) -> None:
        HttpCollectionWorker(
            worker_id="collector-report-1",
            repository=self.repository,
            orchestrator=self.orchestrator,
            execution=self.execution,
            probe=SuccessfulHttpProbe(),
            assessment_queue=self.queue,
            clock=lambda: NOW,
        ).run_once(audit=self.audit)

    def _complete_analysis(self):
        self._run_collection()
        assessor = HttpMetadataAssessmentService(
            methodology=self.methodology,
            repository=self.repository,
            clock=lambda: NOW,
        )
        HttpAssessmentWorker(
            worker_id="report-worker-1",
            repository=self.repository,
            assessor=assessor,
            clock=lambda: NOW,
        ).run_once(audit=self.audit)
        return self.service.get_report("analysis-report-1", audit=self.audit)

    def _save_crawl_observation(self, *, include_unexpected: bool = False) -> Observation:
        attempt = self.repository.list_attempts("analysis-report-1")[0]
        payload: dict[str, object] = {
            "limits": {
                "max_depth": 2,
                "max_urls": 10,
                "max_response_bytes": 1024,
                "max_total_bytes": 4096,
                "max_total_elapsed_milliseconds": 5000,
                "max_links_per_document": 100,
            },
            "robots": {
                "disposition": "allow",
                "matched_rule": "allow",
                "matched_octets": 1,
                "matched_group_count": 1,
                "applicable_rule_count": 1,
                "policy_id": "rfc9309-respect-1",
                "parser_version": "rfc9309-1",
                "product_token": "arquivabilidade-ja",
                "source_state": "parsed",
            },
            "summary": {
                "discovered_references": 4,
                "discovered_unique_internal": 3,
                "enqueued": 3,
                "visited": 3,
                "successful": 2,
                "broken_internal": 1,
                "edge_count": 3,
                "maximum_depth_reached": 1,
                "response_bytes": 512,
                "elapsed_milliseconds": 42.5,
                "status_classes": {"2xx": 2, "4xx": 1},
                "failures": {},
                "discarded": {"external_origin": 1},
                "termination_reason": "complete",
            },
        }
        if include_unexpected:
            payload["unexpected"] = SUBJECT
        observation = Observation.create(
            observation_id="crawl-report-1",
            analysis_id=attempt.analysis_id,
            attempt_id=attempt.attempt_id,
            kind="bounded_crawl",
            subject_uri=SUBJECT,
            observed_at=NOW,
            probe_id="http-metadata",
            tool_name="test",
            tool_version="1",
            payload_schema_version="1.0",
            payload=payload,
        )
        self.repository.save_observation(observation, audit=self.audit)
        return observation

    def _save_browser_observation(self) -> Observation:
        attempt = self.repository.list_attempts("analysis-report-1")[0]
        observation = Observation.create(
            observation_id="browser-report-1",
            analysis_id=attempt.analysis_id,
            attempt_id=attempt.attempt_id,
            kind="browser_render",
            subject_uri=SUBJECT,
            observed_at=NOW,
            probe_id="browser-render",
            tool_name="chromium",
            tool_version="140.0.1",
            payload_schema_version="1.0",
            payload={
                "budgets": BrowserBudgets().to_dict(),
                "profile": BrowserProfile().to_dict(),
                "isolation": BrowserIsolationAttestation(
                    policy_id="browser-container-1",
                    sandbox_enabled=True,
                    running_as_root=False,
                    read_only_root_filesystem=True,
                    ephemeral_profile=True,
                    host_mounts_present=False,
                    secrets_available=False,
                    docker_socket_available=False,
                ).to_dict(),
                "aggregate": BrowserRenderAggregate(
                    raw_dom_hash="a" * 64,
                    rendered_dom_hash="b" * 64,
                    raw_url_set_hash="c" * 64,
                    rendered_url_set_hash="d" * 64,
                    raw_url_count=1,
                    rendered_url_count=2,
                    additional_url_count=1,
                    request_count=3,
                    internal_request_count=2,
                    external_request_count=1,
                    api_request_count=1,
                    external_api_request_count=1,
                    failed_request_count=0,
                    blocked_request_count=0,
                    transfer_bytes=1024,
                    raw_dom_bytes=256,
                    rendered_dom_bytes=512,
                    console_event_count=0,
                    elapsed_milliseconds=1000,
                    termination_reason="complete",
                ).to_dict(),
            },
        )
        self.repository.save_observation(observation, audit=self.audit)
        return observation

    def tearDown(self) -> None:
        self.connection.close()

    def test_queued_report_exposes_progress_without_sensitive_target_parts(self) -> None:
        report = self.service.get_report("analysis-report-1", audit=self.audit)
        document = report.to_dict()

        self.assertEqual("https://example.org/", report.target_origin)
        self.assertEqual("collection_queued", report.progress.phase)
        self.assertEqual(0, report.progress.attempts_used)
        self.assertEqual((), report.attempts)
        self.assertEqual("collection", report.jobs[0].stage)
        self.assertEqual("pending", report.jobs[0].state)
        self.assertEqual("1.0", document["report_schema_version"])
        self.assertIsNone(report.assessment)
        self.assertEqual([], document["indicator_results"])
        serialized = json.dumps(document)
        self.assertNotIn("private/path", serialized)
        self.assertNotIn("must-not-be-logged", serialized)
        self.assertNotIn("subject_uri", serialized)

    def test_history_lists_only_owned_sanitized_summaries(self) -> None:
        history = self.service.list_history("user-opaque-1", audit=self.audit)
        other = self.service.list_history("another-user", audit=self.audit)

        self.assertEqual(1, len(history))
        self.assertEqual("analysis-report-1", history[0].analysis_id)
        self.assertEqual("https://example.org/", history[0].target_origin)
        self.assertIsNone(history[0].overall_state)
        self.assertEqual((), other)
        serialized = json.dumps([item.to_dict() for item in history])
        self.assertNotIn("private/path", serialized)
        self.assertNotIn("must-not-be-logged", serialized)
        event = self.connection.execute(
            """
            SELECT action, extra_json FROM audit_events
            WHERE action = 'access.analysis_history'
            ORDER BY event_id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual("access.analysis_history", event[0])
        self.assertEqual(0, json.loads(event[1])["item_count"])

    def test_completed_report_contains_results_and_hash_only_provenance(self) -> None:
        report = self._complete_analysis()
        document = report.to_dict()

        self.assertEqual("completed", report.state)
        self.assertEqual("completed", report.progress.phase)
        self.assertEqual(
            ["C01", "C05", "D01", "D07", "O05", "R06"],
            [item.indicator_id for item in report.indicator_results],
        )
        self.assertEqual(6, len(report.evidence))
        self.assertTrue(all(item.sources for item in report.evidence))
        self.assertIsNotNone(report.assessment)
        assert report.assessment is not None
        self.assertEqual("partially_assessable", report.assessment.overall_state.value)
        self.assertIn("discoverability", report.assessment.dimensions)
        self.assertLess(report.assessment.dimensions["discoverability"].coverage, 1)
        self.assertTrue(
            all(item.state.value == "inactive" for item in report.assessment.gates.values())
        )
        self.assertIsNone(report.assessment.legacy_clear_plus)
        self.assertEqual(3, len(report.limitations))
        comparison = self.service._compare(report, report)
        self.assertEqual("no_observed_change", comparison.classification)
        self.assertEqual((), comparison.indicator_changes)
        serialized = json.dumps(document)
        self.assertNotIn("headers", serialized)
        self.assertNotIn('"payload":', serialized)
        self.assertNotIn("private/path", serialized)
        self.assertNotIn("must-not-be-logged", serialized)

    def test_completed_export_matches_versioned_golden_snapshot(self) -> None:
        report = self._complete_analysis()
        canonical = json.dumps(
            report.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        self.assertEqual("1.0", report.report_schema_version)
        self.assertEqual(
            "64c6baa7731260017a41d4b244d0de4a7d3c4db808b55bb6403dcec5fbbb0e54",
            hashlib.sha256(canonical).hexdigest(),
        )

    def test_comparison_requires_compatible_earlier_baseline(self) -> None:
        self._complete_analysis()

        with self.assertRaises(ReportNotFoundError):
            self.service.compare_with_previous(
                "analysis-report-1", "user-opaque-1", audit=self.audit
            )

        event = self.connection.execute(
            """
            SELECT action, result, extra_json FROM audit_events
            WHERE action = 'access.analysis_comparison'
            ORDER BY event_id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual(("access.analysis_comparison", "failure"), event[:2])
        self.assertEqual("COMPARISON_NOT_FOUND", json.loads(event[2])["error_code"])

    def test_comparison_uses_latest_compatible_owned_baseline(self) -> None:
        self._complete_analysis()
        current = self.repository.load_analysis_report_snapshot("analysis-report-1")
        assert current is not None
        baseline = replace(
            current,
            analysis=replace(
                current.analysis,
                created_at=NOW - timedelta(days=1),
                updated_at=NOW - timedelta(days=1),
                started_at=NOW - timedelta(days=1),
                finished_at=NOW - timedelta(days=1),
            ),
        )
        underlying = self.repository

        class ComparisonRepository:
            def list_owned_analysis_ids(self, owner_user_id, *, limit):
                del owner_user_id, limit
                return ("analysis-report-1", "baseline-key")

            def load_analysis_report_snapshot(self, analysis_id):
                return baseline if analysis_id == "baseline-key" else current

            def record_analysis_history_access(self, **event):
                return underlying.record_analysis_history_access(**event)

        comparison = AnalysisReportService(
            ComparisonRepository(),  # type: ignore[arg-type]
            self.methodology,
        ).compare_with_previous(
            "analysis-report-1", "user-opaque-1", audit=self.audit
        )

        self.assertEqual("no_observed_change", comparison.classification)
        self.assertEqual((), comparison.indicator_changes)
        event = self.connection.execute(
            """
            SELECT result, extra_json FROM audit_events
            WHERE action = 'access.analysis_comparison'
            ORDER BY event_id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual("success", event[0])
        self.assertEqual(2, json.loads(event[1])["item_count"])

    def test_report_exposes_browser_queue_stage(self) -> None:
        self._run_collection()
        attempt = self.repository.list_attempts("analysis-report-1")[0]
        BrowserQueueService(
            self.repository,
            clock=lambda: NOW,
            job_id_factory=lambda: "browser-job-report-1",
        ).enqueue("analysis-report-1", attempt.attempt_id, audit=self.audit)

        report = self.service.get_report("analysis-report-1", audit=self.audit)

        self.assertEqual("browser_queued", report.progress.phase)
        stages = {item.job_id: item.stage for item in report.jobs}
        self.assertEqual("browser", stages["browser-job-report-1"])

    def test_report_rejects_unexpected_crawl_fields_without_audit_leakage(self) -> None:
        self._run_collection()
        self._save_crawl_observation(include_unexpected=True)

        with self.assertRaisesRegex(
            ReportError, "crawl observation failed report validation"
        ):
            self.service.get_report("analysis-report-1", audit=self.audit)

        event = self.connection.execute(
            """
            SELECT result, extra_json FROM audit_events
            WHERE action = 'access.analysis_report'
            ORDER BY event_id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual("failure", event[0])
        self.assertEqual("REPORT_BUILD_FAILED", json.loads(event[1])["error_code"])
        self.assertNotIn("must-not-be-logged", json.dumps(event))

    def test_report_exposes_only_validated_aggregate_crawl_data(self) -> None:
        self._run_collection()
        observation = self._save_crawl_observation()

        report = self.service.get_report("analysis-report-1", audit=self.audit)
        document = report.to_dict()

        self.assertEqual(1, len(report.crawl_runs))
        crawl = document["crawl_runs"][0]
        self.assertEqual(observation.content_hash, crawl["content_hash"])
        self.assertEqual("attempt-report-1", crawl["attempt_id"])
        self.assertEqual(3, crawl["summary"]["visited"])
        self.assertEqual("rfc9309-respect-1", crawl["robots"]["policy_id"])
        serialized = json.dumps(crawl)
        self.assertNotIn("private/path", serialized)
        self.assertNotIn("must-not-be-logged", serialized)
        self.assertNotIn("subject_uri", serialized)
        self.assertNotIn('"payload":', serialized)
        audit_extra = self.connection.execute(
            """
            SELECT extra_json FROM audit_events
            WHERE action = 'access.analysis_report'
            ORDER BY event_id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual(1, json.loads(audit_extra[0])["crawl_count"])

    def test_report_exposes_sanitized_browser_aggregate(self) -> None:
        self._run_collection()
        observation = self._save_browser_observation()

        report = self.service.get_report("analysis-report-1", audit=self.audit)
        document = report.to_dict()

        self.assertEqual(1, len(report.browser_runs))
        browser = document["browser_runs"][0]
        self.assertEqual(observation.content_hash, browser["content_hash"])
        self.assertEqual("chromium-baseline-1", browser["profile_id"])
        self.assertEqual(3, browser["summary"]["request_count"])
        serialized = json.dumps(browser)
        self.assertNotIn("raw_dom_hash", serialized)
        self.assertNotIn("rendered_dom_hash", serialized)
        self.assertNotIn("url_set_hash", serialized)
        self.assertNotIn("isolation", serialized)
        self.assertNotIn("private/path", serialized)
        self.assertNotIn("must-not-be-logged", serialized)
        audit_extra = self.connection.execute(
            """
            SELECT extra_json FROM audit_events
            WHERE action = 'access.analysis_report'
            ORDER BY event_id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual(1, json.loads(audit_extra[0])["browser_count"])

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
        self.assertEqual("requested", extra["state"])
        self.assertEqual(0, extra["attempt_count"])
        self.assertEqual(0, extra["crawl_count"])
        self.assertEqual(0, extra["browser_count"])
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
