from __future__ import annotations

from typing import Protocol

from archivability.lifecycle.models import Analysis, Attempt
from archivability.storage.audit import AuditContext


class LifecycleRepository(Protocol):
    def add_analysis(
        self,
        value: Analysis,
        *,
        owner_user_id: str | None = None,
        audit: AuditContext,
    ) -> None: ...

    def get_analysis(self, analysis_id: str) -> Analysis | None: ...

    def add_attempt(
        self,
        previous_analysis: Analysis,
        analysis: Analysis,
        attempt: Attempt,
        *,
        audit: AuditContext,
    ) -> None: ...

    def get_attempt(self, attempt_id: str) -> Attempt | None: ...

    def list_attempts(self, analysis_id: str) -> tuple[Attempt, ...]: ...

    def update_attempt(
        self, previous: Attempt, current: Attempt, *, audit: AuditContext
    ) -> None: ...

    def update_analysis(
        self, previous: Analysis, current: Analysis, *, audit: AuditContext
    ) -> None: ...
