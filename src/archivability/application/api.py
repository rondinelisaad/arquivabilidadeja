from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from urllib.parse import quote

from archivability.application.http_workflow import HttpAssessmentWorkflow, WorkflowError
from archivability.application.read_model import (
    AnalysisReportService,
    ReportError,
    ReportNotFoundError,
)
from archivability.lifecycle.models import LifecycleError
from archivability.probes.models import ProbeValidationError
from archivability.probes.ports import Probe
from archivability.storage.audit import AuditContext


_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")


class ApiValidationError(ValueError):
    """Raised when trusted adapter context is malformed."""


@dataclass(frozen=True, slots=True)
class ApiPrincipal:
    user_id: str
    session_id: str

    def __post_init__(self) -> None:
        for field in ("user_id", "session_id"):
            value = getattr(self, field)
            if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
                raise ApiValidationError(f"{field} is invalid")


@dataclass(frozen=True, slots=True)
class ApiRequestContext:
    request_id: str
    ip_address: str
    principal: ApiPrincipal | None

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not _IDENTIFIER_PATTERN.fullmatch(
            self.request_id
        ):
            raise ApiValidationError("request_id is invalid")
        try:
            canonical_ip = str(ipaddress.ip_address(self.ip_address))
        except ValueError as exc:
            raise ApiValidationError("ip_address is invalid") from exc
        object.__setattr__(self, "ip_address", canonical_ip)

    def audit_context(self) -> AuditContext:
        return AuditContext(
            user_id=self.principal.user_id if self.principal else None,
            session_id=self.principal.session_id if self.principal else None,
            ip_address=self.ip_address,
        )


@dataclass(frozen=True, slots=True)
class ApiResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.status_code not in {200, 202, 400, 401, 403, 404, 429, 500}:
            raise ApiValidationError("API response status is unsupported")
        object.__setattr__(self, "body", MappingProxyType(dict(self.body)))
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status_code": self.status_code,
            "body": dict(self.body),
            "headers": dict(self.headers),
        }


class ApiAuthorizationPolicy(Protocol):
    def can_create_analysis(self, principal: ApiPrincipal) -> bool: ...

    def can_read_analysis(self, principal: ApiPrincipal, analysis_id: str) -> bool: ...


class ApiRateLimiter(Protocol):
    def allow(self, principal: ApiPrincipal, operation: str) -> bool: ...


class ApiAuditRecorder(Protocol):
    def record_analysis_api_event(
        self,
        *,
        request_id: str,
        operation: str,
        result: str,
        status_code: int,
        error_code: str | None,
        analysis_id: str | None,
        audit: AuditContext,
    ) -> None: ...


class AnalysisApi:
    """Framework-neutral, authenticated boundary for analysis creation and reads."""

    def __init__(
        self,
        *,
        workflow: HttpAssessmentWorkflow,
        reports: AnalysisReportService,
        probe: Probe,
        authorization: ApiAuthorizationPolicy,
        rate_limiter: ApiRateLimiter,
        audit_recorder: ApiAuditRecorder,
    ) -> None:
        self._workflow = workflow
        self._reports = reports
        self._probe = probe
        self._authorization = authorization
        self._rate_limiter = rate_limiter
        self._audit_recorder = audit_recorder

    def create_analysis(
        self,
        payload: Mapping[str, Any],
        *,
        context: ApiRequestContext,
    ) -> ApiResponse:
        denied = self._authenticate(context, "create")
        if denied is not None:
            return denied
        principal = context.principal
        assert principal is not None
        try:
            authorized = self._authorization.can_create_analysis(principal)
        except Exception:
            return self._error(
                context,
                operation="create",
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
            )
        if not authorized:
            return self._error(
                context,
                operation="create",
                status_code=403,
                code="ACCESS_DENIED",
                message="Access denied.",
                result="unauthorized",
            )
        try:
            within_limit = self._rate_limiter.allow(principal, "analysis.create")
        except Exception:
            return self._error(
                context,
                operation="create",
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
            )
        if not within_limit:
            return self._error(
                context,
                operation="create",
                status_code=429,
                code="RATE_LIMITED",
                message="Request limit exceeded.",
            )
        try:
            subject_uri = self._subject_uri(payload)
            outcome = self._workflow.start(
                subject_uri=subject_uri,
                probe=self._probe,
                audit=context.audit_context(),
            )
        except (ApiValidationError, WorkflowError, LifecycleError, ProbeValidationError):
            return self._error(
                context,
                operation="create",
                status_code=400,
                code="INVALID_REQUEST",
                message="Request is invalid.",
            )
        except Exception:
            return self._error(
                context,
                operation="create",
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
            )
        body = {
            "analysis_id": outcome.analysis.analysis_id,
            "status": outcome.status,
            "report_path": f"/v1/analyses/{quote(outcome.analysis.analysis_id, safe='')}",
        }
        return self._response(
            context,
            operation="create",
            status_code=202,
            body=body,
            result="success",
            analysis_id=outcome.analysis.analysis_id,
        )

    def get_analysis(
        self,
        analysis_id: str,
        *,
        context: ApiRequestContext,
    ) -> ApiResponse:
        denied = self._authenticate(context, "read")
        if denied is not None:
            return denied
        principal = context.principal
        assert principal is not None
        if not isinstance(analysis_id, str) or not _IDENTIFIER_PATTERN.fullmatch(
            analysis_id
        ):
            return self._error(
                context,
                operation="read",
                status_code=400,
                code="INVALID_REQUEST",
                message="Request is invalid.",
            )
        try:
            authorized = self._authorization.can_read_analysis(principal, analysis_id)
        except Exception:
            return self._error(
                context,
                operation="read",
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
                analysis_id=analysis_id,
            )
        if not authorized:
            return self._error(
                context,
                operation="read",
                status_code=403,
                code="ACCESS_DENIED",
                message="Access denied.",
                result="unauthorized",
                analysis_id=analysis_id,
            )
        try:
            within_limit = self._rate_limiter.allow(principal, "analysis.read")
        except Exception:
            return self._error(
                context,
                operation="read",
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
                analysis_id=analysis_id,
            )
        if not within_limit:
            return self._error(
                context,
                operation="read",
                status_code=429,
                code="RATE_LIMITED",
                message="Request limit exceeded.",
                analysis_id=analysis_id,
            )
        try:
            report = self._reports.get_report(
                analysis_id,
                audit=context.audit_context(),
            )
        except ReportNotFoundError:
            return self._error(
                context,
                operation="read",
                status_code=404,
                code="NOT_FOUND",
                message="Analysis was not found.",
                analysis_id=analysis_id,
            )
        except ReportError:
            return self._error(
                context,
                operation="read",
                status_code=400,
                code="INVALID_REQUEST",
                message="Request is invalid.",
                analysis_id=analysis_id,
            )
        except Exception:
            return self._error(
                context,
                operation="read",
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
                analysis_id=analysis_id,
            )
        return self._response(
            context,
            operation="read",
            status_code=200,
            body=report.to_dict(),
            result="success",
            analysis_id=analysis_id,
        )

    def _authenticate(
        self, context: ApiRequestContext, operation: str
    ) -> ApiResponse | None:
        if context.principal is not None:
            return None
        return self._error(
            context,
            operation=operation,
            status_code=401,
            code="AUTHENTICATION_REQUIRED",
            message="Authentication is required.",
            result="unauthorized",
        )

    @staticmethod
    def _subject_uri(payload: Mapping[str, Any]) -> str:
        if not isinstance(payload, Mapping) or set(payload) != {"subject_uri"}:
            raise ApiValidationError("request body fields are invalid")
        value = payload["subject_uri"]
        if not isinstance(value, str) or not value or len(value) > 2048:
            raise ApiValidationError("subject_uri is invalid")
        return value

    def _error(
        self,
        context: ApiRequestContext,
        *,
        operation: str,
        status_code: int,
        code: str,
        message: str,
        result: str = "failure",
        analysis_id: str | None = None,
    ) -> ApiResponse:
        return self._response(
            context,
            operation=operation,
            status_code=status_code,
            body={
                "error": {"code": code, "message": message},
                "request_id": context.request_id,
            },
            result=result,
            error_code=code,
            analysis_id=analysis_id,
        )

    def _response(
        self,
        context: ApiRequestContext,
        *,
        operation: str,
        status_code: int,
        body: Mapping[str, Any],
        result: str,
        error_code: str | None = None,
        analysis_id: str | None = None,
    ) -> ApiResponse:
        self._audit_recorder.record_analysis_api_event(
            request_id=context.request_id,
            operation=operation,
            result=result,
            status_code=status_code,
            error_code=error_code,
            analysis_id=analysis_id,
            audit=context.audit_context(),
        )
        return ApiResponse(
            status_code=status_code,
            body=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
