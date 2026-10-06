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

from archivability.lifecycle import (  # noqa: E402
    Analysis,
    AnalysisOrchestrator,
    AnalysisState,
    AttemptState,
    LifecycleError,
    finalize_analysis,
    finish_attempt,
    start_attempt,
)
from archivability.methodology import load_methodology  # noqa: E402
from archivability.storage import (  # noqa: E402
    AuditContext,
    ConcurrencyConflict,
    SqliteLifecycleRepository,
    apply_sqlite_migrations,
)


T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
METHODOLOGY = ROOT / "methodology" / "v0.1.0"


class StateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.analysis = Analysis.create(
            analysis_id="analysis-1",
            subject_uri="https://example.org/",
            methodology_id="archivability-ja",
            methodology_version="0.1.0",
            created_at=T0,
            max_attempts=2,
        )

    def test_successful_state_sequence_is_immutable(self) -> None:
        running, attempt = start_attempt(
            self.analysis, attempt_id="attempt-1", occurred_at=T0 + timedelta(seconds=1)
        )
        succeeded = finish_attempt(
            attempt,
            target=AttemptState.SUCCEEDED,
            occurred_at=T0 + timedelta(seconds=2),
        )
        completed = finalize_analysis(
            running,
            [succeeded],
            target=AnalysisState.COMPLETED,
            occurred_at=T0 + timedelta(seconds=3),
        )
        self.assertEqual(AnalysisState.REQUESTED, self.analysis.state)
        self.assertEqual(AnalysisState.COMPLETED, completed.state)
        self.assertEqual(2, completed.revision)
        self.assertEqual(1, succeeded.revision)

    def test_invalid_transitions_and_backwards_time_are_rejected(self) -> None:
        running, attempt = start_attempt(
            self.analysis, attempt_id="attempt-1", occurred_at=T0 + timedelta(seconds=2)
        )
        with self.assertRaises(LifecycleError):
            start_attempt(
                Analysis(
                    **{
                        **running.to_dict(),
                        "state": AnalysisState.COMPLETED,
                        "attempt_ids": running.attempt_ids,
                        "created_at": running.created_at,
                        "updated_at": running.updated_at,
                        "started_at": running.started_at,
                        "finished_at": T0 + timedelta(seconds=3),
                    }
                ),
                attempt_id="attempt-2",
                occurred_at=T0 + timedelta(seconds=4),
            )
        with self.assertRaises(LifecycleError):
            finish_attempt(
                attempt,
                target=AttemptState.SUCCEEDED,
                occurred_at=T0,
            )

    def test_failure_code_is_required_and_machine_readable(self) -> None:
        _, attempt = start_attempt(
            self.analysis, attempt_id="attempt-1", occurred_at=T0
        )
        for code in (None, "network timeout", "token=do-not-log"):
            with self.subTest(code=code), self.assertRaises(LifecycleError):
                finish_attempt(
                    attempt,
                    target=AttemptState.FAILED,
                    occurred_at=T0 + timedelta(seconds=1),
                    failure_code=code,
                )
        failed = finish_attempt(
            attempt,
            target=AttemptState.FAILED,
            occurred_at=T0 + timedelta(seconds=1),
            failure_code="NETWORK_TIMEOUT",
        )
        self.assertEqual("NETWORK_TIMEOUT", failed.failure_code)

    def test_analysis_outcome_must_match_attempt_outcomes(self) -> None:
        running, attempt = start_attempt(
            self.analysis, attempt_id="attempt-1", occurred_at=T0
        )
        failed = finish_attempt(
            attempt,
            target=AttemptState.FAILED,
            occurred_at=T0 + timedelta(seconds=1),
            failure_code="PROBE_FAILED",
        )
        with self.assertRaises(LifecycleError):
            finalize_analysis(
                running,
                [failed],
                target=AnalysisState.COMPLETED,
                occurred_at=T0 + timedelta(seconds=2),
            )


class OrchestratorPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        apply_sqlite_migrations(self.connection)
        event_ids = iter(f"event-{index}" for index in range(100))
        self.repository = SqliteLifecycleRepository(
            self.connection,
            clock=lambda: T0,
            event_id_factory=lambda: next(event_ids),
        )
        times = iter(T0 + timedelta(seconds=index) for index in range(100))
        attempt_ids = iter(f"attempt-{index}" for index in range(1, 10))
        self.orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: next(times),
            analysis_id_factory=lambda: "analysis-1",
            attempt_id_factory=lambda: next(attempt_ids),
        )
        self.methodology = load_methodology(METHODOLOGY)
        self.audit = AuditContext(
            user_id="user-opaque-1",
            ip_address="192.0.2.10",
            session_id="session-opaque-1",
        )

    def tearDown(self) -> None:
        self.connection.close()

    def _create(self, max_attempts: int = 3) -> Analysis:
        return self.orchestrator.create_analysis(
            subject_uri="https://example.org/private?token=must-not-be-logged",
            methodology=self.methodology,
            max_attempts=max_attempts,
            audit=self.audit,
        )

    def test_orchestrates_complete_analysis_without_network_collection(self) -> None:
        analysis = self._create()
        attempt = self.orchestrator.start_attempt(analysis.analysis_id, audit=self.audit)
        succeeded = self.orchestrator.finish_attempt(
            analysis.analysis_id,
            attempt.attempt_id,
            target=AttemptState.SUCCEEDED,
            audit=self.audit,
        )
        completed = self.orchestrator.finalize_analysis(
            analysis.analysis_id,
            target=AnalysisState.COMPLETED,
            audit=self.audit,
        )

        self.assertEqual(AttemptState.SUCCEEDED, succeeded.state)
        self.assertEqual(AnalysisState.COMPLETED, completed.state)
        self.assertEqual(completed, self.repository.get_analysis(analysis.analysis_id))
        self.assertEqual((succeeded,), self.repository.list_attempts(analysis.analysis_id))

        audit_rows = self.connection.execute(
            "SELECT action, result, before_json, after_json FROM audit_events ORDER BY event_id"
        ).fetchall()
        self.assertEqual(5, len(audit_rows))
        self.assertIn(("job.analysis_attempt", "success"), [(r[0], r[1]) for r in audit_rows])
        serialized = json.dumps(audit_rows)
        self.assertNotIn("example.org", serialized)
        self.assertNotIn("must-not-be-logged", serialized)

    def test_retry_can_finish_as_partially_completed(self) -> None:
        analysis = self._create(max_attempts=2)
        first = self.orchestrator.start_attempt(analysis.analysis_id)
        self.orchestrator.finish_attempt(
            analysis.analysis_id,
            first.attempt_id,
            target=AttemptState.FAILED,
            failure_code="NETWORK_TIMEOUT",
        )
        second = self.orchestrator.start_attempt(analysis.analysis_id)
        self.orchestrator.finish_attempt(
            analysis.analysis_id,
            second.attempt_id,
            target=AttemptState.SUCCEEDED,
        )
        result = self.orchestrator.finalize_analysis(
            analysis.analysis_id,
            target=AnalysisState.PARTIALLY_COMPLETED,
        )
        self.assertEqual(AnalysisState.PARTIALLY_COMPLETED, result.state)
        failure_events = self.connection.execute(
            "SELECT count(*) FROM audit_events WHERE action = 'job.analysis_attempt' AND result = 'failure'"
        ).fetchone()[0]
        self.assertEqual(1, failure_events)

    def test_attempt_limit_and_running_finalization_are_rejected(self) -> None:
        analysis = self._create(max_attempts=2)
        attempt = self.orchestrator.start_attempt(analysis.analysis_id)
        with self.assertRaises(LifecycleError):
            self.orchestrator.start_attempt(analysis.analysis_id)
        with self.assertRaises(LifecycleError):
            self.orchestrator.finalize_analysis(
                analysis.analysis_id, target=AnalysisState.COMPLETED
            )
        self.orchestrator.finish_attempt(
            analysis.analysis_id,
            attempt.attempt_id,
            target=AttemptState.FAILED,
            failure_code="PROBE_FAILED",
        )
        second = self.orchestrator.start_attempt(analysis.analysis_id)
        self.orchestrator.finish_attempt(
            analysis.analysis_id,
            second.attempt_id,
            target=AttemptState.FAILED,
            failure_code="PROBE_FAILED",
        )
        with self.assertRaises(LifecycleError):
            self.orchestrator.start_attempt(analysis.analysis_id)

    def test_cancelled_attempt_can_cancel_analysis(self) -> None:
        analysis = self._create()
        attempt = self.orchestrator.start_attempt(analysis.analysis_id)
        self.orchestrator.finish_attempt(
            analysis.analysis_id,
            attempt.attempt_id,
            target=AttemptState.CANCELLED,
        )
        cancelled = self.orchestrator.finalize_analysis(
            analysis.analysis_id,
            target=AnalysisState.CANCELLED,
        )
        self.assertEqual(AnalysisState.CANCELLED, cancelled.state)

    def test_stale_analysis_revision_is_rejected_and_audited(self) -> None:
        original = self._create(max_attempts=2)
        first_analysis, first_attempt = start_attempt(
            original, attempt_id="manual-1", occurred_at=T0 + timedelta(seconds=20)
        )
        stale_analysis, stale_attempt = start_attempt(
            original, attempt_id="manual-2", occurred_at=T0 + timedelta(seconds=21)
        )
        self.repository.add_attempt(original, first_analysis, first_attempt)
        with self.assertRaises(ConcurrencyConflict):
            self.repository.add_attempt(original, stale_analysis, stale_attempt)
        self.assertIsNone(self.repository.get_attempt("manual-2"))
        failure = self.connection.execute(
            "SELECT result FROM audit_events WHERE resource_id = 'manual-2'"
        ).fetchone()
        self.assertEqual(("failure",), failure)

    def test_migrations_are_idempotent(self) -> None:
        apply_sqlite_migrations(self.connection)
        versions = self.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        self.assertEqual(
            [
                ("001_initial",),
                ("002_analysis_lifecycle",),
                ("003_assessment_jobs",),
                ("004_analysis_ownership",),
            ],
            versions,
        )


if __name__ == "__main__":
    unittest.main()
