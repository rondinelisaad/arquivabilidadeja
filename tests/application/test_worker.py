from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from archivability import (  # noqa: E402
    AssessmentQueueWorkerProcess,
    RuntimeConfigurationError,
    WorkerSettings,
    run_production_worker,
)


class _LifecycleRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def record_assessment_worker_lifecycle_event(self, **event) -> None:
        event.pop("audit")
        self.events.append(event)


class _StoppingWorker:
    def __init__(self, stop_event: threading.Event) -> None:
        self.stop_event = stop_event
        self.calls = 0

    def run_once(self, *, audit):
        del audit
        self.calls += 1
        self.stop_event.set()
        return SimpleNamespace(status="succeeded")


class _FailingWorker:
    def run_once(self, *, audit):
        del audit
        raise RuntimeError("token=must-not-be-logged")


class WorkerSettingsTests(unittest.TestCase):
    def test_environment_loader_generates_opaque_worker_id(self) -> None:
        settings = WorkerSettings.from_environment(
            {
                "ARCHIVABILITY_ENVIRONMENT": "test",
                "ARCHIVABILITY_DATABASE_DSN": (
                    "host=/private/tmp dbname=archive user=archive_runtime"
                ),
                "ARCHIVABILITY_METHODOLOGY_PATH": str(
                    (ROOT / "methodology" / "v0.1.0").resolve()
                ),
                "ARCHIVABILITY_WORKER_POLL_MILLISECONDS": "250",
            }
        )

        self.assertEqual(250, settings.poll_interval_milliseconds)
        self.assertTrue(settings.worker_id)
        self.assertNotIn(settings.database_dsn, repr(settings))

    def test_invalid_worker_limits_are_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeConfigurationError, "poll"):
            WorkerSettings(
                environment="test",
                database_dsn=("host=/private/tmp dbname=archive user=archive_runtime"),
                methodology_path=(ROOT / "methodology" / "v0.1.0").resolve(),
                worker_id="worker-1",
                poll_interval_milliseconds=1,
            )


class AssessmentQueueWorkerProcessTests(unittest.TestCase):
    def test_stop_requested_during_job_prevents_another_claim(self) -> None:
        stop_event = threading.Event()
        worker = _StoppingWorker(stop_event)
        recorder = _LifecycleRecorder()
        process = AssessmentQueueWorkerProcess(
            worker_id="worker-1",
            worker=worker,
            lifecycle_recorder=recorder,
            poll_interval_seconds=0.05,
        )

        processed = process.run(stop_event)

        self.assertEqual(1, processed)
        self.assertEqual(1, worker.calls)
        self.assertEqual(["started", "stopped"], [e["status"] for e in recorder.events])
        self.assertEqual(1, recorder.events[-1]["processed_count"])

    def test_failure_audit_uses_only_stable_error_code(self) -> None:
        recorder = _LifecycleRecorder()
        process = AssessmentQueueWorkerProcess(
            worker_id="worker-1",
            worker=_FailingWorker(),
            lifecycle_recorder=recorder,
            poll_interval_seconds=0.05,
        )

        with self.assertRaisesRegex(RuntimeError, "must-not-be-logged"):
            process.run(threading.Event())

        self.assertEqual(["started", "failed"], [e["status"] for e in recorder.events])
        self.assertEqual("WORKER_LOOP_FAILED", recorder.events[-1]["error_code"])
        self.assertNotIn("token", repr(recorder.events))


POSTGRES_DSN = os.environ.get("ARCHIVABILITY_TEST_POSTGRES_DSN")
POSTGRES_RUNTIME_DSN = os.environ.get("ARCHIVABILITY_TEST_POSTGRES_RUNTIME_DSN")


@unittest.skipUnless(
    POSTGRES_DSN and POSTGRES_RUNTIME_DSN,
    "PostgreSQL migration and restricted runtime DSNs are not configured",
)
class WorkerPostgreSqlIntegrationTests(unittest.TestCase):
    def test_stopped_worker_records_lifecycle_with_restricted_role(self) -> None:
        assert POSTGRES_DSN is not None
        assert POSTGRES_RUNTIME_DSN is not None
        worker_id = f"worker-{uuid4()}"
        settings = WorkerSettings(
            environment="integration",
            database_dsn=POSTGRES_RUNTIME_DSN,
            methodology_path=(ROOT / "methodology" / "v0.1.0").resolve(),
            worker_id=worker_id,
            poll_interval_milliseconds=50,
        )
        stop_event = threading.Event()
        stop_event.set()

        processed = run_production_worker(settings, stop_event)

        self.assertEqual(0, processed)
        with psycopg.connect(POSTGRES_DSN) as connection:
            events = connection.execute(
                """
                SELECT result, extra_json->>'status',
                       (extra_json->>'processed_count')::integer
                FROM archivability.audit_events
                WHERE action = 'system.assessment_worker_lifecycle'
                  AND resource_id = %s
                ORDER BY timestamp, event_id
                """,
                (worker_id,),
            ).fetchall()
        self.assertEqual(
            [("success", "started", 0), ("success", "stopped", 0)],
            events,
        )


if __name__ == "__main__":
    unittest.main()
