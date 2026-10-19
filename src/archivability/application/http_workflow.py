from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from archivability.jobs.collection import (
    CollectionEnqueueOutcome,
    CollectionLimits,
    HttpCollectionQueueService,
)
from archivability.lifecycle.models import Analysis, AnalysisState
from archivability.lifecycle.service import AnalysisOrchestrator
from archivability.methodology.models import MethodologyConfig
from archivability.probes.ports import Probe
from archivability.storage.audit import AuditContext


class WorkflowError(ValueError):
    """Raised when an explicit HTTP assessment workflow request is invalid."""


ProbeLimits = CollectionLimits


@dataclass(frozen=True, slots=True)
class HttpWorkflowOutcome:
    status: str
    analysis: Analysis
    collection: CollectionEnqueueOutcome

    def __post_init__(self) -> None:
        if self.status != "collection_queued":
            raise WorkflowError("HTTP workflow outcome status is invalid")
        if self.analysis.state is not AnalysisState.REQUESTED:
            raise WorkflowError("queued collection requires a requested analysis")
        if self.collection.job.analysis_id != self.analysis.analysis_id:
            raise WorkflowError("collection job belongs to another analysis")


class HttpAssessmentWorkflow:
    """Create an analysis and enqueue collection without performing network I/O."""

    def __init__(
        self,
        *,
        methodology: MethodologyConfig,
        orchestrator: AnalysisOrchestrator,
        collection_queue: HttpCollectionQueueService | None = None,
        repository: Any | None = None,
        probe_execution: Any | None = None,
        queue: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        del probe_execution, queue
        self._methodology = methodology
        self._orchestrator = orchestrator
        if collection_queue is None:
            if repository is None:
                raise WorkflowError("collection queue or repository is required")
            self._collection_queue = HttpCollectionQueueService(
                repository,
                **({"clock": clock} if clock is not None else {}),
            )
        else:
            self._collection_queue = collection_queue

    def start(
        self,
        *,
        subject_uri: str,
        probe: Probe | None = None,
        owner_user_id: str | None = None,
        analysis_max_attempts: int = 3,
        assessment_max_attempts: int = 3,
        limits: ProbeLimits = ProbeLimits(),
        audit: AuditContext = AuditContext(),
    ) -> HttpWorkflowOutcome:
        # Accepted for compatibility, but deliberately never invoked here.
        del probe
        if not isinstance(limits, CollectionLimits):
            raise WorkflowError("limits must be a ProbeLimits value")
        if (
            not isinstance(assessment_max_attempts, int)
            or not 1 <= assessment_max_attempts <= 10
        ):
            raise WorkflowError("assessment_max_attempts must be between 1 and 10")
        analysis = self._orchestrator.prepare_analysis(
            subject_uri=subject_uri,
            methodology=self._methodology,
            max_attempts=analysis_max_attempts,
        )
        queued = self._collection_queue.enqueue_new_analysis(
            analysis,
            owner_user_id=owner_user_id,
            limits=limits,
            max_attempts=analysis_max_attempts,
            assessment_max_attempts=assessment_max_attempts,
            audit=audit,
        )
        return HttpWorkflowOutcome(
            status="collection_queued",
            analysis=analysis,
            collection=queued,
        )

    def retry(self, *args: object, **kwargs: object) -> HttpWorkflowOutcome:
        del args, kwargs
        raise WorkflowError("collection retries are managed by the durable worker")

    def cancel(
        self, analysis_id: str, *, audit: AuditContext = AuditContext()
    ) -> Analysis:
        return self._orchestrator.cancel_analysis(analysis_id, audit=audit)
