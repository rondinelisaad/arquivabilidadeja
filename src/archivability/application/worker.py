from __future__ import annotations

import os
import re
import signal
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from types import FrameType
from typing import Any, Protocol
from uuid import uuid4

import psycopg

from archivability.application.production import (
    ProductionSettings,
    RuntimeConfigurationError,
    validate_runtime_database_role,
)
from archivability.evidence.assessment import HttpMetadataAssessmentService
from archivability.jobs.service import HttpAssessmentWorker, WorkerOutcome
from archivability.methodology.loader import load_methodology
from archivability.storage.audit import AuditContext
from archivability.storage.postgresql_jobs import PostgreSqlAssessmentJobRepository

_WORKER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class WorkerLifecycleRecorder(Protocol):
    def record_assessment_worker_lifecycle_event(
        self,
        *,
        worker_id: str,
        status: str,
        processed_count: int,
        error_code: str | None = None,
        audit: AuditContext = AuditContext(),
    ) -> None: ...


class QueueWorker(Protocol):
    def run_once(self, *, audit: AuditContext = AuditContext()) -> WorkerOutcome: ...


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    environment: str
    database_dsn: str = field(repr=False)
    methodology_path: Path
    worker_id: str
    poll_interval_milliseconds: int = 500
    lease_seconds: int = 300
    retry_base_seconds: int = 30
    connect_timeout_seconds: int = 5

    def __post_init__(self) -> None:
        if not isinstance(self.environment, str) or not re.fullmatch(
            r"[a-z][a-z0-9-]{0,31}", self.environment
        ):
            raise RuntimeConfigurationError("runtime environment name is invalid")
        ProductionSettings._validate_database_dsn(self.database_dsn)
        if (
            not isinstance(self.methodology_path, Path)
            or not self.methodology_path.is_absolute()
            or not self.methodology_path.is_dir()
        ):
            raise RuntimeConfigurationError(
                "methodology path must be an existing absolute directory"
            )
        if not isinstance(self.worker_id, str) or not _WORKER_ID_PATTERN.fullmatch(
            self.worker_id
        ):
            raise RuntimeConfigurationError("worker ID is invalid")
        ProductionSettings._bounded_int(
            "poll_interval_milliseconds",
            self.poll_interval_milliseconds,
            50,
            60_000,
        )
        ProductionSettings._bounded_int("lease_seconds", self.lease_seconds, 1, 900)
        ProductionSettings._bounded_int(
            "retry_base_seconds", self.retry_base_seconds, 1, 900
        )
        ProductionSettings._bounded_int(
            "connect_timeout_seconds", self.connect_timeout_seconds, 1, 30
        )

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> WorkerSettings:
        values = os.environ if environment is None else environment
        worker_id = values.get("ARCHIVABILITY_WORKER_ID", str(uuid4()))
        return cls(
            environment=ProductionSettings._required(
                values, "ARCHIVABILITY_ENVIRONMENT"
            ),
            database_dsn=ProductionSettings._required(
                values, "ARCHIVABILITY_DATABASE_DSN"
            ),
            methodology_path=Path(
                ProductionSettings._required(values, "ARCHIVABILITY_METHODOLOGY_PATH")
            ),
            worker_id=worker_id,
            poll_interval_milliseconds=ProductionSettings._optional_int(
                values, "ARCHIVABILITY_WORKER_POLL_MILLISECONDS", 500
            ),
            lease_seconds=ProductionSettings._optional_int(
                values, "ARCHIVABILITY_WORKER_LEASE_SECONDS", 300
            ),
            retry_base_seconds=ProductionSettings._optional_int(
                values, "ARCHIVABILITY_WORKER_RETRY_BASE_SECONDS", 30
            ),
            connect_timeout_seconds=ProductionSettings._optional_int(
                values, "ARCHIVABILITY_DB_CONNECT_TIMEOUT_SECONDS", 5
            ),
        )


class AssessmentQueueWorkerProcess:
    """Poll the durable queue and stop only between claimed jobs."""

    def __init__(
        self,
        *,
        worker_id: str,
        worker: QueueWorker,
        lifecycle_recorder: WorkerLifecycleRecorder,
        poll_interval_seconds: float,
    ) -> None:
        if not isinstance(worker_id, str) or not _WORKER_ID_PATTERN.fullmatch(
            worker_id
        ):
            raise RuntimeConfigurationError("worker ID is invalid")
        if not 0.05 <= poll_interval_seconds <= 60:
            raise RuntimeConfigurationError("worker poll interval is invalid")
        self._worker_id = worker_id
        self._worker = worker
        self._lifecycle_recorder = lifecycle_recorder
        self._poll_interval_seconds = poll_interval_seconds

    def run(self, stop_event: threading.Event) -> int:
        if not isinstance(stop_event, threading.Event):
            raise TypeError("stop_event must be a threading.Event")
        processed_count = 0
        self._record_lifecycle("started", processed_count)
        try:
            while not stop_event.is_set():
                outcome = self._worker.run_once(audit=AuditContext())
                if outcome.status == "idle":
                    stop_event.wait(self._poll_interval_seconds)
                else:
                    processed_count += 1
        except Exception:
            try:
                self._record_lifecycle(
                    "failed",
                    processed_count,
                    error_code="WORKER_LOOP_FAILED",
                )
            except Exception:
                pass
            raise
        self._record_lifecycle("stopped", processed_count)
        return processed_count

    def _record_lifecycle(
        self,
        status: str,
        processed_count: int,
        *,
        error_code: str | None = None,
    ) -> None:
        self._lifecycle_recorder.record_assessment_worker_lifecycle_event(
            worker_id=self._worker_id,
            status=status,
            processed_count=processed_count,
            error_code=error_code,
            audit=AuditContext(),
        )


ConnectionFactory = Callable[..., psycopg.Connection[Any]]


def run_production_worker(
    settings: WorkerSettings,
    stop_event: threading.Event,
    *,
    connection_factory: ConnectionFactory = psycopg.connect,
) -> int:
    if not isinstance(settings, WorkerSettings):
        raise RuntimeConfigurationError("settings must be WorkerSettings")
    methodology = load_methodology(settings.methodology_path)
    connection = connection_factory(
        settings.database_dsn,
        autocommit=True,
        connect_timeout=settings.connect_timeout_seconds,
        ssl_min_protocol_version="TLSv1.2",
        application_name=f"arquivabilidade-ja-worker-{settings.environment}",
    )
    try:
        validate_runtime_database_role(connection)
        repository = PostgreSqlAssessmentJobRepository(connection)
        worker = HttpAssessmentWorker(
            worker_id=settings.worker_id,
            repository=repository,
            assessor=HttpMetadataAssessmentService(
                methodology=methodology,
                repository=repository,
            ),
            lease_duration=timedelta(seconds=settings.lease_seconds),
            retry_base_delay=timedelta(seconds=settings.retry_base_seconds),
        )
        process = AssessmentQueueWorkerProcess(
            worker_id=settings.worker_id,
            worker=worker,
            lifecycle_recorder=repository,
            poll_interval_seconds=settings.poll_interval_milliseconds / 1000,
        )
        return process.run(stop_event)
    finally:
        connection.close()


def main() -> int:
    stop_event = threading.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        stop_event.set()

    previous = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        settings = WorkerSettings.from_environment()
        run_production_worker(settings, stop_event)
        return 0
    except Exception:
        return 1
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
