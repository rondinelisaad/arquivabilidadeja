from __future__ import annotations

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

from archivability.browser import (  # noqa: E402
    BrowserContractError,
    BrowserExecutionService,
    BrowserIsolationAttestation,
    BrowserRenderAggregate,
    BrowserRenderRequest,
    BrowserRenderResult,
)
from archivability.lifecycle import AnalysisOrchestrator, AttemptState  # noqa: E402
from archivability.methodology import load_methodology  # noqa: E402
from archivability.storage import (  # noqa: E402
    AuditContext,
    SqliteLifecycleRepository,
    apply_sqlite_migrations,
)

NOW = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)
SUBJECT = "https://example.org/private/path?token=never-in-audit"


class StaticRenderer:
    def __init__(self, result: BrowserRenderResult) -> None:
        self.result = result
        self.calls = 0

    def render(self, request: BrowserRenderRequest) -> BrowserRenderResult:
        del request
        self.calls += 1
        return self.result


def render_result(*, attempt_id: str = "attempt-browser-1") -> BrowserRenderResult:
    return BrowserRenderResult(
        analysis_id="analysis-browser-1",
        attempt_id=attempt_id,
        engine_name="chromium",
        engine_version="140.0.1",
        started_at=NOW + timedelta(seconds=1),
        finished_at=NOW + timedelta(seconds=5),
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
            raw_url_count=2,
            rendered_url_count=5,
            additional_url_count=3,
            request_count=10,
            internal_request_count=7,
            external_request_count=3,
            api_request_count=2,
            failed_request_count=1,
            blocked_request_count=0,
            transfer_bytes=4096,
            rendered_dom_bytes=2048,
            console_event_count=1,
            elapsed_milliseconds=4000,
            termination_reason="complete",
        ),
    )


class BrowserExecutionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        apply_sqlite_migrations(self.connection)
        event_ids = iter(f"browser-event-{index}" for index in range(100))
        self.repository = SqliteLifecycleRepository(
            self.connection,
            clock=lambda: NOW,
            event_id_factory=lambda: next(event_ids),
        )
        orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: NOW,
            analysis_id_factory=lambda: "analysis-browser-1",
            attempt_id_factory=lambda: "attempt-browser-1",
        )
        analysis = orchestrator.create_analysis(
            subject_uri=SUBJECT,
            methodology=load_methodology(ROOT / "methodology" / "v0.1.0"),
        )
        attempt = orchestrator.start_attempt(analysis.analysis_id)
        orchestrator.finish_attempt(
            analysis.analysis_id,
            attempt.attempt_id,
            target=AttemptState.SUCCEEDED,
        )
        self.request = BrowserRenderRequest(
            analysis_id=analysis.analysis_id,
            attempt_id=attempt.attempt_id,
            subject_uri=SUBJECT,
            requested_at=NOW,
        )
        self.audit = AuditContext(user_id="browser-worker-opaque")

    def tearDown(self) -> None:
        self.connection.close()

    def test_persists_aggregate_observation_and_reuses_it_on_retry(self) -> None:
        renderer = StaticRenderer(render_result())
        service = BrowserExecutionService(
            renderer=renderer,
            repository=self.repository,
            observation_id_factory=lambda: "browser-observation-1",
        )

        first = service.execute(self.request, audit=self.audit)
        second = service.execute(self.request, audit=self.audit)

        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual(first.observation, second.observation)
        self.assertEqual(1, renderer.calls)
        self.assertEqual("browser_render", first.observation.kind)
        self.assertEqual(5, first.observation.payload["aggregate"]["rendered_url_count"])
        events = self.connection.execute(
            """
            SELECT result, extra_json FROM audit_events
            WHERE action = 'job.browser_render'
            ORDER BY event_id
            """
        ).fetchall()
        self.assertEqual(3, len(events))
        self.assertEqual(
            ["started", "succeeded", "reused"],
            [json.loads(item[1])["status"] for item in events],
        )
        serialized = json.dumps(events)
        self.assertNotIn("example.org", serialized)
        self.assertNotIn("private/path", serialized)
        self.assertNotIn("never-in-audit", serialized)
        self.assertNotIn("raw_dom_hash", serialized)

    def test_invalid_result_fails_closed_and_records_stable_error(self) -> None:
        renderer = StaticRenderer(render_result(attempt_id="attempt-other"))
        service = BrowserExecutionService(
            renderer=renderer,
            repository=self.repository,
        )

        with self.assertRaisesRegex(BrowserContractError, "does not match"):
            service.execute(self.request, audit=self.audit)

        self.assertIsNone(
            self.repository.get_browser_render_observation(self.request.attempt_id)
        )
        events = self.connection.execute(
            """
            SELECT result, extra_json FROM audit_events
            WHERE action = 'job.browser_render'
            ORDER BY event_id
            """
        ).fetchall()
        self.assertEqual(["success", "failure"], [item[0] for item in events])
        failure = json.loads(events[-1][1])
        self.assertEqual("failed", failure["status"])
        self.assertEqual("BROWSER_CONTRACT_FAILED", failure["error_code"])

    def test_reuse_rejects_changed_profile_without_rendering(self) -> None:
        renderer = StaticRenderer(render_result())
        service = BrowserExecutionService(
            renderer=renderer,
            repository=self.repository,
            observation_id_factory=lambda: "browser-observation-1",
        )
        service.execute(self.request, audit=self.audit)
        changed = replace(
            self.request,
            profile=replace(self.request.profile, color_scheme="dark"),
        )

        with self.assertRaisesRegex(BrowserContractError, "conflicts"):
            service.execute(changed, audit=self.audit)

        self.assertEqual(1, renderer.calls)
        failure = self.connection.execute(
            """
            SELECT extra_json FROM audit_events
            WHERE action = 'job.browser_render' AND result = 'failure'
            ORDER BY event_id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual(
            "BROWSER_CONTRACT_FAILED", json.loads(failure[0])["error_code"]
        )


if __name__ == "__main__":
    unittest.main()
