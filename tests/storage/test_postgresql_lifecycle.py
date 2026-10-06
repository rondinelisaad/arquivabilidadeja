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
    PostgreSqlLifecycleRepository,
    apply_postgresql_migrations,
    load_methodology,
)
from archivability.lifecycle.state_machine import finish_attempt  # noqa: E402


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
        self.repository = PostgreSqlLifecycleRepository(
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


if __name__ == "__main__":
    unittest.main()
