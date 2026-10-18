from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from archivability import (
    AnalysisOrchestrator,
    AttemptState,
    BrowserExecutionService,
    BrowserIsolationAttestation,
    BrowserQueueService,
    BrowserRenderAggregate,
    BrowserRenderResult,
    BrowserWorker,
    HttpAssessmentQueueService,
    Observation,
    SqliteAssessmentJobRepository,
    apply_sqlite_migrations,
    load_methodology,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 19, 2, 0, tzinfo=timezone.utc)
SUBJECT = "https://example.org/private?token=never-audited"


class DynamicRenderer:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.calls = 0

    def render(self, request):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("sensitive browser failure")
        return BrowserRenderResult(
            analysis_id=request.analysis_id,
            attempt_id=request.attempt_id,
            engine_name="chromium",
            engine_version="140.0.1",
            started_at=request.requested_at,
            finished_at=request.requested_at + timedelta(seconds=1),
            isolation=BrowserIsolationAttestation(
                policy_id="browser-container-1",
                sandbox_enabled=True,
                running_as_root=False,
                read_only_root_filesystem=True,
                ephemeral_profile=True,
                host_mounts_present=False,
                secrets_available=False,
                docker_socket_available=False,
            ),
            aggregate=BrowserRenderAggregate(
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
                failed_request_count=0,
                blocked_request_count=0,
                transfer_bytes=1024,
                rendered_dom_bytes=512,
                console_event_count=0,
                elapsed_milliseconds=1000,
                termination_reason="complete",
            ),
        )


class FailingAssessmentQueue:
    def enqueue(self, observation_id, *, max_attempts, audit):
        del observation_id, max_attempts, audit
        raise RuntimeError("simulated assessment queue interruption")


class BrowserWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        apply_sqlite_migrations(self.connection)
        event_ids = (f"browser-worker-event-{index}" for index in range(200))
        self.repository = SqliteAssessmentJobRepository(
            self.connection,
            clock=lambda: NOW,
            event_id_factory=event_ids.__next__,
        )
        orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: NOW,
            analysis_id_factory=lambda: "analysis-browser-worker",
            attempt_id_factory=lambda: "attempt-browser-worker",
        )
        self.analysis = orchestrator.create_analysis(
            subject_uri=SUBJECT,
            methodology=load_methodology(ROOT / "methodology" / "v0.1.0"),
        )
        attempt = orchestrator.start_attempt(self.analysis.analysis_id)
        self.attempt = orchestrator.finish_attempt(
            self.analysis.analysis_id,
            attempt.attempt_id,
            target=AttemptState.SUCCEEDED,
        )
        self.http_observation = Observation.create(
            observation_id="http-observation-browser-worker",
            analysis_id=self.analysis.analysis_id,
            attempt_id=self.attempt.attempt_id,
            kind="http_metadata",
            subject_uri=SUBJECT,
            observed_at=NOW,
            probe_id="http-metadata",
            tool_name="test-http",
            tool_version="1.0",
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
        )
        self.repository.save_observation(self.http_observation)

    def tearDown(self) -> None:
        self.connection.close()

    def _enqueue(self, *, max_attempts: int = 2):
        return BrowserQueueService(
            self.repository,
            clock=lambda: NOW,
            job_id_factory=lambda: "browser-worker-job",
        ).enqueue(
            self.analysis.analysis_id,
            self.attempt.attempt_id,
            max_attempts=max_attempts,
        ).job

    def _worker(self, renderer, *, clock=lambda: NOW, assessment_queue=None):
        return BrowserWorker(
            worker_id="browser-worker-1",
            repository=self.repository,
            execution=BrowserExecutionService(
                renderer=renderer,
                repository=self.repository,
                observation_id_factory=lambda: "browser-observation-worker",
            ),
            assessment_queue=assessment_queue
            or HttpAssessmentQueueService(
                repository=self.repository,
                clock=clock,
                job_id_factory=lambda: "assessment-after-browser",
            ),
            clock=clock,
            lease_duration=timedelta(minutes=1),
            retry_base_delay=timedelta(seconds=1),
        )

    def test_success_persists_render_and_queues_http_assessment(self) -> None:
        job = self._enqueue()
        renderer = DynamicRenderer()

        outcome = self._worker(renderer).run_once()

        self.assertEqual("succeeded", outcome.status)
        self.assertEqual("succeeded", self.repository.get_browser_job(job.job_id).state)
        self.assertIsNotNone(
            self.repository.get_browser_render_observation(self.attempt.attempt_id)
        )
        assessment = self.connection.execute(
            "SELECT observation_id, state FROM assessment_jobs WHERE analysis_id = ?",
            (self.analysis.analysis_id,),
        ).fetchone()
        self.assertEqual((self.http_observation.observation_id, "pending"), assessment)
        serialized = json.dumps(
            self.connection.execute(
                "SELECT action, extra_json FROM audit_events ORDER BY event_id"
            ).fetchall()
        )
        self.assertNotIn("example.org", serialized)
        self.assertNotIn("never-audited", serialized)

    def test_retry_reuses_render_after_assessment_queue_interruption(self) -> None:
        self._enqueue()
        renderer = DynamicRenderer()
        interrupted = self._worker(
            renderer,
            assessment_queue=FailingAssessmentQueue(),
        ).run_once()
        self.assertEqual("retry_scheduled", interrupted.status)
        self.assertEqual("BROWSER_PIPELINE_FAILED", interrupted.job.error_code)
        self.assertEqual(1, renderer.calls)

        later = NOW + timedelta(seconds=2)
        resumed = self._worker(renderer, clock=lambda: later).run_once()

        self.assertEqual("succeeded", resumed.status)
        self.assertEqual(1, renderer.calls)
        self.assertEqual(
            (self.http_observation.observation_id,),
            self.connection.execute(
                "SELECT observation_id FROM assessment_jobs WHERE analysis_id = ?",
                (self.analysis.analysis_id,),
            ).fetchone(),
        )

    def test_terminal_render_failure_degrades_to_http_assessment(self) -> None:
        job = self._enqueue(max_attempts=1)
        outcome = self._worker(DynamicRenderer(failures=1)).run_once()

        self.assertEqual("failed", outcome.status)
        stored = self.repository.get_browser_job(job.job_id)
        self.assertEqual("failed", stored.state)
        self.assertEqual("BROWSER_RENDER_FAILED", stored.error_code)
        self.assertEqual(
            (self.http_observation.observation_id,),
            self.connection.execute(
                "SELECT observation_id FROM assessment_jobs WHERE analysis_id = ?",
                (self.analysis.analysis_id,),
            ).fetchone(),
        )


if __name__ == "__main__":
    unittest.main()
