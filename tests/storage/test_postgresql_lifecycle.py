from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import psycopg  # noqa: E402

from archivability import (  # noqa: E402
    AnalysisOrchestrator,
    AnalysisState,
    AttemptState,
    AuditContext,
    ConcurrencyConflict,
    PersistenceError,
    PostgreSqlEvidenceRepository,
    PostgreSqlLifecycleRepository,
    apply_postgresql_migrations,
    load_methodology,
)
from archivability.evidence import (  # noqa: E402
    Evidence,
    Observation,
    derive_indicator_result,
)
from archivability.lifecycle.state_machine import finish_attempt  # noqa: E402
from archivability.methodology import ResultState  # noqa: E402


POSTGRES_DSN = os.environ.get("ARCHIVABILITY_TEST_POSTGRES_DSN")
POSTGRES_RUNTIME_DSN = os.environ.get(
    "ARCHIVABILITY_TEST_POSTGRES_RUNTIME_DSN", POSTGRES_DSN
)
T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


@unittest.skipUnless(POSTGRES_DSN, "PostgreSQL integration DSN is not configured")
class PostgreSqlLifecycleIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert POSTGRES_DSN is not None
        with psycopg.connect(POSTGRES_DSN) as migration_connection:
            apply_postgresql_migrations(migration_connection)

    def setUp(self) -> None:
        assert POSTGRES_RUNTIME_DSN is not None
        self.connection = psycopg.connect(POSTGRES_RUNTIME_DSN, autocommit=True)
        event_prefix = uuid4().hex
        event_sequence = iter(range(100))
        self.repository = PostgreSqlEvidenceRepository(
            self.connection,
            clock=lambda: T0,
            event_id_factory=lambda: f"{event_prefix}-{next(event_sequence)}",
        )
        identifier = uuid4().hex
        self.analysis_id = f"analysis-{identifier}"
        self.attempt_id = f"attempt-{identifier}"
        self.audit = AuditContext(
            user_id=f"owner-{identifier}",
            session_id=f"session-{identifier}",
            ip_address="192.0.2.20",
        )
        self.orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: T0,
            analysis_id_factory=lambda: self.analysis_id,
            attempt_id_factory=lambda: self.attempt_id,
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_lifecycle_owner_audit_and_optimistic_concurrency(self) -> None:
        methodology = load_methodology(ROOT / "methodology" / "v0.1.0")
        analysis = self.orchestrator.create_analysis(
            subject_uri="https://example.org/private?token=not-persisted-in-audit",
            methodology=methodology,
            owner_user_id=self.audit.user_id,
            audit=self.audit,
        )
        attempt = self.orchestrator.start_attempt(
            analysis.analysis_id, audit=self.audit
        )
        failed = self.orchestrator.finish_attempt(
            analysis.analysis_id,
            attempt.attempt_id,
            target=AttemptState.FAILED,
            failure_code="PROBE_FAILED",
            audit=self.audit,
        )
        finalized = self.orchestrator.finalize_analysis(
            analysis.analysis_id,
            target=AnalysisState.FAILED,
            audit=self.audit,
        )

        self.assertEqual(
            self.audit.user_id,
            self.repository.get_analysis_owner(analysis.analysis_id),
        )
        self.assertEqual(AnalysisState.FAILED, finalized.state)
        stored_attempt = self.repository.get_attempt(attempt.attempt_id)
        self.assertIsNotNone(stored_attempt)
        assert stored_attempt is not None
        self.assertEqual(AttemptState.FAILED, stored_attempt.state)
        stale = finish_attempt(
            attempt,
            target=AttemptState.CANCELLED,
            occurred_at=T0 + timedelta(seconds=1),
        )
        with self.assertRaises(ConcurrencyConflict):
            self.repository.update_attempt(attempt, stale, audit=self.audit)

        events = self.connection.execute(
            """
            SELECT action, result, after_json, extra_json
            FROM archivability.audit_events
            WHERE resource_id IN (%s, %s)
            ORDER BY timestamp, event_id
            """,
            (analysis.analysis_id, attempt.attempt_id),
        ).fetchall()
        serialized = repr(events)
        self.assertIn("permission.analysis_owner_assign", serialized)
        self.assertIn("job.analysis_attempt_start", serialized)
        self.assertIn("ConcurrencyConflict", serialized)
        self.assertNotIn("not-persisted-in-audit", serialized)

    def test_runtime_repository_requires_autocommit(self) -> None:
        assert POSTGRES_RUNTIME_DSN is not None
        with psycopg.connect(POSTGRES_RUNTIME_DSN) as connection:
            with self.assertRaisesRegex(PersistenceError, "autocommit"):
                PostgreSqlLifecycleRepository(connection)

    def test_probe_completion_and_evidence_chain_round_trip(self) -> None:
        methodology = load_methodology(ROOT / "methodology" / "v0.1.0")
        analysis = self.orchestrator.create_analysis(
            subject_uri="https://example.org/private?token=not-in-audit",
            methodology=methodology,
            owner_user_id=self.audit.user_id,
            audit=self.audit,
        )
        attempt = self.orchestrator.start_attempt(
            analysis.analysis_id, audit=self.audit
        )
        observation = Observation.create(
            observation_id=f"observation-{uuid4().hex}",
            analysis_id=analysis.analysis_id,
            attempt_id=attempt.attempt_id,
            kind="http_metadata",
            subject_uri=analysis.subject_uri,
            observed_at=T0,
            probe_id="http-metadata",
            tool_name="integration-probe",
            tool_version="1.0.0",
            payload_schema_version="1.0",
            payload={"status_code": 200, "secret": "not-in-audit"},
        )
        succeeded = finish_attempt(
            attempt,
            target=AttemptState.SUCCEEDED,
            occurred_at=T0 + timedelta(seconds=1),
        )

        self.repository.complete_probe_attempt(
            attempt, succeeded, (observation,), audit=self.audit
        )
        evidence = Evidence.create(
            evidence_id=f"evidence-{uuid4().hex}",
            analysis_id=analysis.analysis_id,
            indicator_id="D01",
            kind="derived_measurement",
            subject_uri=analysis.subject_uri,
            method_id="discoverability-check",
            method_version="1.0.0",
            created_at=T0,
            confidence=0.9,
            observations=[observation],
            data={"found": True},
            summary="Origem localizada.",
        )
        result = derive_indicator_result(
            methodology,
            indicator_id="D01",
            state=ResultState.PASS,
            evidence=[evidence],
        )
        self.repository.save_evidence(evidence, audit=self.audit)
        self.repository.save_indicator_result(result, audit=self.audit)

        stored_observation = self.repository.get_observation(
            observation.observation_id
        )
        stored_evidence = self.repository.get_evidence(evidence.evidence_id)
        stored_result = self.repository.get_indicator_result(
            analysis.analysis_id, "D01"
        )
        self.assertEqual(observation, stored_observation)
        self.assertEqual(evidence, stored_evidence)
        self.assertEqual(result, stored_result)
        events = self.connection.execute(
            """
            SELECT before_json, after_json, extra_json
            FROM archivability.audit_events
            WHERE resource_id IN (%s, %s, %s)
            """,
            (attempt.attempt_id, observation.observation_id, evidence.evidence_id),
        ).fetchall()
        serialized = repr(events)
        self.assertNotIn("example.org", serialized)
        self.assertNotIn("not-in-audit", serialized)

    def test_probe_batch_failure_rolls_back_observations_and_transition(self) -> None:
        methodology = load_methodology(ROOT / "methodology" / "v0.1.0")
        analysis = self.orchestrator.create_analysis(
            subject_uri="https://example.org/",
            methodology=methodology,
            owner_user_id=self.audit.user_id,
            audit=self.audit,
        )
        attempt = self.orchestrator.start_attempt(
            analysis.analysis_id, audit=self.audit
        )
        duplicate = self._observation(attempt, f"duplicate-{uuid4().hex}")
        new_observation = self._observation(attempt, f"new-{uuid4().hex}")
        self.repository.save_observation(duplicate, audit=self.audit)
        succeeded = finish_attempt(
            attempt,
            target=AttemptState.SUCCEEDED,
            occurred_at=T0 + timedelta(seconds=1),
        )

        with self.assertRaises(PersistenceError):
            self.repository.complete_probe_attempt(
                attempt,
                succeeded,
                (new_observation, duplicate),
                audit=self.audit,
            )

        self.assertIsNone(
            self.repository.get_observation(new_observation.observation_id)
        )
        stored_attempt = self.repository.get_attempt(attempt.attempt_id)
        self.assertIsNotNone(stored_attempt)
        assert stored_attempt is not None
        self.assertEqual(AttemptState.RUNNING, stored_attempt.state)
        failure = self.connection.execute(
            """
            SELECT result, extra_json
            FROM archivability.audit_events
            WHERE resource_id = %s AND action = 'job.probe_persistence'
            """,
            (attempt.attempt_id,),
        ).fetchone()
        self.assertEqual("failure", failure[0])
        self.assertEqual("UniqueViolation", failure[1]["error_type"])

    @staticmethod
    def _observation(attempt, observation_id: str) -> Observation:
        return Observation.create(
            observation_id=observation_id,
            analysis_id=attempt.analysis_id,
            attempt_id=attempt.attempt_id,
            kind="http_metadata",
            subject_uri="https://example.org/",
            observed_at=T0,
            probe_id="http-metadata",
            tool_name="integration-probe",
            tool_version="1.0.0",
            payload_schema_version="1.0",
            payload={"status_code": 200},
        )


if __name__ == "__main__":
    unittest.main()
