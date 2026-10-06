from __future__ import annotations

from datetime import datetime
from typing import Protocol

from archivability.evidence.models import Observation
from archivability.jobs.models import AssessmentJob
from archivability.lifecycle.models import Analysis, Attempt
from archivability.storage.audit import AuditContext


class AssessmentJobRepository(Protocol):
    def get_analysis(self, analysis_id: str) -> Analysis | None: ...

    def get_attempt(self, attempt_id: str) -> Attempt | None: ...

    def get_observation(self, observation_id: str) -> Observation | None: ...

    def enqueue_assessment_job(
        self, job: AssessmentJob, *, audit: AuditContext
    ) -> tuple[AssessmentJob, bool]: ...

    def get_assessment_job(self, job_id: str) -> AssessmentJob | None: ...

    def claim_next_assessment_job(
        self,
        *,
        worker_id: str,
        occurred_at: datetime,
        lease_expires_at: datetime,
        audit: AuditContext,
    ) -> AssessmentJob | None: ...

    def update_assessment_job(
        self,
        previous: AssessmentJob,
        current: AssessmentJob,
        *,
        action: str,
        audit: AuditContext,
    ) -> None: ...
