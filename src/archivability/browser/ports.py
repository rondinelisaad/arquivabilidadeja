from __future__ import annotations

from typing import Protocol

from archivability.browser.models import BrowserRenderRequest, BrowserRenderResult
from archivability.evidence.models import Observation
from archivability.storage.audit import AuditContext


class BrowserRenderer(Protocol):
    """Adapter boundary implemented only by an isolated browser worker."""

    def render(self, request: BrowserRenderRequest) -> BrowserRenderResult: ...


class BrowserExecutionRepository(Protocol):
    def authorize_browser_render_request(
        self, *, analysis_id: str, attempt_id: str, subject_uri: str
    ) -> None: ...

    def get_browser_render_observation(self, attempt_id: str) -> Observation | None: ...

    def save_observation(
        self, value: Observation, *, audit: AuditContext
    ) -> None: ...

    def record_browser_render_event(
        self,
        *,
        request: BrowserRenderRequest,
        status: str,
        observation_id: str | None,
        result: BrowserRenderResult | None,
        error_code: str | None,
        audit: AuditContext,
    ) -> None: ...

