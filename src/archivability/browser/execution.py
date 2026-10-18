from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from archivability.browser.models import (
    BrowserContractError,
    BrowserRenderRequest,
    BrowserRenderResult,
)
from archivability.browser.ports import BrowserExecutionRepository, BrowserRenderer
from archivability.evidence.models import Observation
from archivability.storage.audit import AuditContext
from archivability.storage.errors import PersistenceError

IdentifierFactory = Callable[[], str]


@dataclass(frozen=True, slots=True)
class BrowserExecutionOutcome:
    observation: Observation
    reused: bool


class BrowserExecutionService:
    """Validate an isolated render and persist only bounded aggregate evidence."""

    def __init__(
        self,
        *,
        renderer: BrowserRenderer,
        repository: BrowserExecutionRepository,
        observation_id_factory: IdentifierFactory = lambda: str(uuid.uuid4()),
    ) -> None:
        self._renderer = renderer
        self._repository = repository
        self._observation_id_factory = observation_id_factory

    def execute(
        self,
        request: BrowserRenderRequest,
        *,
        audit: AuditContext = AuditContext(),
    ) -> BrowserExecutionOutcome:
        self._repository.authorize_browser_render_request(
            analysis_id=request.analysis_id,
            attempt_id=request.attempt_id,
            subject_uri=request.subject_uri,
        )
        existing = self._repository.get_browser_render_observation(request.attempt_id)
        if existing is not None:
            try:
                self._validate_existing(existing, request)
            except BrowserContractError:
                self._record_failure(request, "BROWSER_CONTRACT_FAILED", audit)
                raise
            self._repository.record_browser_render_event(
                request=request,
                status="reused",
                observation_id=existing.observation_id,
                result=None,
                error_code=None,
                audit=audit,
            )
            return BrowserExecutionOutcome(existing, reused=True)

        self._repository.record_browser_render_event(
            request=request,
            status="started",
            observation_id=None,
            result=None,
            error_code=None,
            audit=audit,
        )
        try:
            result = self._renderer.render(request)
            if not isinstance(result, BrowserRenderResult):
                raise BrowserContractError("browser renderer returned an invalid result")
            result.validate_against(request)
            observation = self._build_observation(request, result)
        except BrowserContractError:
            self._record_failure(request, "BROWSER_CONTRACT_FAILED", audit)
            raise
        except Exception:
            self._record_failure(request, "BROWSER_RENDER_FAILED", audit)
            raise
        try:
            self._repository.save_observation(observation, audit=audit)
            self._repository.record_browser_render_event(
                request=request,
                status="succeeded",
                observation_id=observation.observation_id,
                result=result,
                error_code=None,
                audit=audit,
            )
        except PersistenceError:
            self._record_failure(request, "BROWSER_PERSISTENCE_FAILED", audit)
            raise
        return BrowserExecutionOutcome(observation, reused=False)

    def _record_failure(
        self,
        request: BrowserRenderRequest,
        error_code: str,
        audit: AuditContext,
    ) -> None:
        self._repository.record_browser_render_event(
            request=request,
            status="failed",
            observation_id=None,
            result=None,
            error_code=error_code,
            audit=audit,
        )

    def _build_observation(
        self,
        request: BrowserRenderRequest,
        result: BrowserRenderResult,
    ) -> Observation:
        return Observation.create(
            observation_id=self._observation_id_factory(),
            analysis_id=request.analysis_id,
            attempt_id=request.attempt_id,
            kind="browser_render",
            subject_uri=request.subject_uri,
            observed_at=result.finished_at,
            probe_id="browser-render",
            tool_name=result.engine_name,
            tool_version=result.engine_version,
            payload_schema_version="1.0",
            payload={
                "budgets": request.budgets.to_dict(),
                "profile": request.profile.to_dict(),
                "isolation": result.isolation.to_dict(),
                "aggregate": result.aggregate.to_dict(),
            },
            truncated=result.aggregate.termination_reason != "complete",
        )

    @staticmethod
    def _validate_existing(
        observation: Observation,
        request: BrowserRenderRequest,
    ) -> None:
        if (
            observation.kind != "browser_render"
            or observation.payload_schema_version != "1.0"
            or observation.error_code is not None
            or observation.analysis_id != request.analysis_id
            or observation.attempt_id != request.attempt_id
            or observation.subject_uri != request.subject_uri
            or observation.payload.get("budgets") != request.budgets.to_dict()
            or observation.payload.get("profile") != request.profile.to_dict()
        ):
            raise BrowserContractError("stored browser observation conflicts with request")

