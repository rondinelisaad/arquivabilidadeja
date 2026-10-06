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

from archivability.evidence import (  # noqa: E402
    HttpMetadataAssessmentService,
    Observation,
    derive_http_metadata_indicators,
)
from archivability.lifecycle import (  # noqa: E402
    AnalysisOrchestrator,
    AnalysisState,
    AttemptState,
    finish_attempt,
)
from archivability.methodology import load_methodology  # noqa: E402
from archivability.storage import (  # noqa: E402
    AuditContext,
    IntegrityViolation,
    SqliteLifecycleRepository,
    apply_sqlite_migrations,
)


T0 = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)
SUBJECT = "https://example.org/private?token=must-not-be-logged"
METHODOLOGY_PATH = ROOT / "methodology" / "v0.1.0"


def observation_for(analysis_id: str, attempt_id: str) -> Observation:
    return Observation.create(
        observation_id="observation-http-1",
        analysis_id=analysis_id,
        attempt_id=attempt_id,
        kind="http_metadata",
        subject_uri=SUBJECT,
        observed_at=T0 + timedelta(seconds=2),
        probe_id="http-metadata",
        tool_name="arquivabilidade-http",
        tool_version="0.1.0",
        payload_schema_version="1.1",
        payload={
            "status_code": 200,
            "headers": {"content-length": ["5"]},
            "response_bytes_observed": 5,
            "response_byte_limit": 1024,
            "response_truncated": False,
            "redirect_count": 0,
            "final_transport_secure": True,
        },
    )


class HttpMetadataAssessmentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        apply_sqlite_migrations(self.connection)
        event_ids = iter(f"event-{index:03d}" for index in range(200))
        self.repository = SqliteLifecycleRepository(
            self.connection,
            clock=lambda: T0,
            event_id_factory=lambda: next(event_ids),
        )
        self.methodology = load_methodology(METHODOLOGY_PATH)
        times = iter((T0, T0 + timedelta(seconds=1)))
        orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: next(times),
            analysis_id_factory=lambda: "analysis-1",
            attempt_id_factory=lambda: "attempt-1",
        )
        analysis = orchestrator.create_analysis(
            subject_uri=SUBJECT,
            methodology=self.methodology,
        )
        attempt = orchestrator.start_attempt(analysis.analysis_id)
        self.observation = observation_for(analysis.analysis_id, attempt.attempt_id)
        succeeded = finish_attempt(
            attempt,
            target=AttemptState.SUCCEEDED,
            occurred_at=T0 + timedelta(seconds=2),
        )
        self.repository.complete_probe_attempt(
            attempt,
            succeeded,
            (self.observation,),
        )
        self.audit = AuditContext(user_id="user-opaque-1")
        self.service = HttpMetadataAssessmentService(
            methodology=self.methodology,
            repository=self.repository,
            clock=lambda: T0 + timedelta(seconds=3),
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_persists_derivation_and_closes_analysis_atomically(self) -> None:
        outcome = self.service.assess(self.observation.observation_id, audit=self.audit)

        self.assertEqual("created", outcome.persistence.outcome)
        self.assertEqual(AnalysisState.COMPLETED, outcome.analysis.state)
        self.assertEqual(outcome.analysis, self.repository.get_analysis("analysis-1"))
        self.assertEqual(2, self.connection.execute("SELECT count(*) FROM evidence").fetchone()[0])
        self.assertEqual(
            2,
            self.connection.execute("SELECT count(*) FROM indicator_results").fetchone()[0],
        )
        events = self.connection.execute(
            "SELECT action, result, extra_json FROM audit_events ORDER BY event_id"
        ).fetchall()
        self.assertIn(
            ("job.http_metadata_assessment", "success"),
            [row[:2] for row in events],
        )
        serialized = json.dumps(events)
        self.assertNotIn("example.org", serialized)
        self.assertNotIn("must-not-be-logged", serialized)

    def test_exact_retry_is_an_idempotent_replay(self) -> None:
        first = self.service.assess(self.observation.observation_id, audit=self.audit)
        second = self.service.assess(self.observation.observation_id, audit=self.audit)

        self.assertEqual("created", first.persistence.outcome)
        self.assertEqual("replayed", second.persistence.outcome)
        self.assertEqual(first.analysis, second.analysis)
        self.assertEqual(2, self.connection.execute("SELECT count(*) FROM evidence").fetchone()[0])
        outcomes = [
            json.loads(row[0])["outcome"]
            for row in self.connection.execute(
                """
                SELECT extra_json FROM audit_events
                WHERE action = 'job.http_metadata_assessment'
                ORDER BY event_id
                """
            ).fetchall()
        ]
        self.assertEqual(["created", "replayed"], outcomes)

    def test_partial_preexisting_derivation_is_rejected_without_finalizing(self) -> None:
        derivation = derive_http_metadata_indicators(self.methodology, self.observation)
        self.repository.save_evidence(derivation.evidence[0], audit=self.audit)

        with self.assertRaises(IntegrityViolation):
            self.service.assess(self.observation.observation_id, audit=self.audit)

        self.assertEqual(AnalysisState.RUNNING, self.repository.get_analysis("analysis-1").state)
        self.assertEqual(1, self.connection.execute("SELECT count(*) FROM evidence").fetchone()[0])
        self.assertEqual(
            0,
            self.connection.execute("SELECT count(*) FROM indicator_results").fetchone()[0],
        )
        event = self.connection.execute(
            """
            SELECT result, extra_json FROM audit_events
            WHERE action = 'job.http_metadata_assessment'
            ORDER BY event_id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual("failure", event[0])
        self.assertEqual("IntegrityViolation", json.loads(event[1])["error_type"])


if __name__ == "__main__":
    unittest.main()
