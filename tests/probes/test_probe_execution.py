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

from archivability.evidence import Observation  # noqa: E402
from archivability.lifecycle import AnalysisOrchestrator, AttemptState  # noqa: E402
from archivability.methodology import load_methodology  # noqa: E402
from archivability.probes import (  # noqa: E402
    HttpTransportError,
    ProbeExecutionService,
    ProbeRequest,
    ProbeRunner,
    SsrfPolicy,
)
from archivability.storage import (  # noqa: E402
    AuditContext,
    SqliteLifecycleRepository,
    apply_sqlite_migrations,
)


T0 = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc)
METHODOLOGY = ROOT / "methodology" / "v0.1.0"
PUBLIC_V4 = "93.184.216.34"
SUBJECT = "https://example.org/private?token=must-not-be-logged"


class StaticResolver:
    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        del hostname, port
        return (PUBLIC_V4,)


def build_observation(request: ProbeRequest, observation_id: str) -> Observation:
    return Observation.create(
        observation_id=observation_id,
        analysis_id=request.analysis_id,
        attempt_id=request.attempt_id,
        kind="http_metadata",
        subject_uri=request.subject_uri,
        observed_at=T0 + timedelta(seconds=2),
        probe_id="http-metadata",
        tool_name="fake-http-probe",
        tool_version="1.0.0",
        payload_schema_version="1.0",
        payload={"status_code": 200, "secret": "must-not-be-audited"},
    )


class SuccessfulProbe:
    probe_id = "http-metadata"
    tool_name = "fake-http-probe"
    tool_version = "1.0.0"

    def __init__(self, observation_id: str = "observation-1") -> None:
        self.observation_id = observation_id

    def execute(self, context):
        return (build_observation(context.request, self.observation_id),)


class BatchProbe(SuccessfulProbe):
    def execute(self, context):
        return (
            build_observation(context.request, "new-observation"),
            build_observation(context.request, "duplicate-observation"),
        )


class FailingProbe(SuccessfulProbe):
    def execute(self, context):
        del context
        raise HttpTransportError("token=must-not-be-logged")


class ProbeExecutionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        apply_sqlite_migrations(self.connection)
        event_ids = iter(f"event-{index:03d}" for index in range(100))
        self.repository = SqliteLifecycleRepository(
            self.connection,
            clock=lambda: T0,
            event_id_factory=lambda: next(event_ids),
        )
        orchestrator_times = iter((T0, T0 + timedelta(seconds=1)))
        orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: next(orchestrator_times),
            analysis_id_factory=lambda: "analysis-1",
            attempt_id_factory=lambda: "attempt-1",
        )
        analysis = orchestrator.create_analysis(
            subject_uri=SUBJECT,
            methodology=load_methodology(METHODOLOGY),
        )
        orchestrator.start_attempt(analysis.analysis_id)
        self.audit = AuditContext(user_id="user-opaque-1")
        runner = ProbeRunner(
            policy=SsrfPolicy(),
            resolver=StaticResolver(),
            event_recorder=self.repository,
            authorizer=self.repository,
        )
        self.service = ProbeExecutionService(
            runner=runner,
            repository=self.repository,
            clock=lambda: T0 + timedelta(seconds=3),
        )
        self.request = ProbeRequest(
            analysis_id="analysis-1",
            attempt_id="attempt-1",
            subject_uri=SUBJECT,
            requested_at=T0 + timedelta(seconds=1),
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_persists_observation_and_success_transition_atomically(self) -> None:
        outcome = self.service.execute(
            self.request,
            SuccessfulProbe(),
            audit=self.audit,
        )

        self.assertTrue(outcome.succeeded)
        self.assertEqual(AttemptState.SUCCEEDED, outcome.attempt.state)
        self.assertEqual(
            outcome.observations[0].to_dict(),
            self.repository.get_observation("observation-1").to_dict(),
        )
        self.assertEqual(outcome.attempt, self.repository.get_attempt("attempt-1"))
        events = self.connection.execute(
            """
            SELECT action, resource, result, before_json, after_json, extra_json
            FROM audit_events
            ORDER BY event_id
            """
        ).fetchall()
        self.assertIn(("job.probe", "attempt", "success"), [row[:3] for row in events])
        self.assertIn(("data.create", "observation", "success"), [row[:3] for row in events])
        self.assertIn(
            ("job.analysis_attempt", "attempt", "success"),
            [row[:3] for row in events],
        )
        serialized = json.dumps(events)
        self.assertNotIn("example.org", serialized)
        self.assertNotIn("must-not-be", serialized)

    def test_probe_failure_marks_attempt_failed_without_exposing_error(self) -> None:
        outcome = self.service.execute(self.request, FailingProbe(), audit=self.audit)

        self.assertFalse(outcome.succeeded)
        self.assertEqual("PROBE_FAILED", outcome.failure_code)
        self.assertEqual(AttemptState.FAILED, outcome.attempt.state)
        self.assertEqual((), outcome.observations)
        self.assertEqual(outcome.attempt, self.repository.get_attempt("attempt-1"))
        events = self.connection.execute(
            "SELECT action, result, after_json, extra_json FROM audit_events"
        ).fetchall()
        serialized = json.dumps(events)
        self.assertIn("PROBE_FAILED", serialized)
        self.assertIn("HTTPTRANSPORTERROR", serialized)
        self.assertNotIn("token=must-not-be-logged", serialized)

    def test_persistence_failure_rolls_back_batch_then_fails_attempt(self) -> None:
        existing = build_observation(self.request, "duplicate-observation")
        self.repository.save_observation(existing, audit=self.audit)

        outcome = self.service.execute(
            self.request,
            BatchProbe(),
            audit=self.audit,
        )

        self.assertEqual("PERSISTENCE_FAILED", outcome.failure_code)
        self.assertEqual(AttemptState.FAILED, outcome.attempt.state)
        self.assertEqual(
            1,
            self.connection.execute(
                "SELECT count(*) FROM observations WHERE observation_id = ?",
                ("duplicate-observation",),
            ).fetchone()[0],
        )
        self.assertIsNone(self.repository.get_observation("new-observation"))
        events = self.connection.execute(
            "SELECT action, result, extra_json FROM audit_events ORDER BY event_id"
        ).fetchall()
        self.assertIn(("job.probe_persistence", "failure"), [row[:2] for row in events])
        self.assertIn(("job.analysis_attempt", "failure"), [row[:2] for row in events])
        self.assertNotIn("example.org", json.dumps(events))


if __name__ == "__main__":
    unittest.main()
