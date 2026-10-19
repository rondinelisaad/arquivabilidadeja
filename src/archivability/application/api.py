from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from urllib.parse import quote, urlsplit
from uuid import uuid4

from archivability.application.http_workflow import (
    HttpAssessmentWorkflow,
    WorkflowError,
)
from archivability.application.openapi import build_openapi_document
from archivability.application.read_model import (
    AnalysisReportService,
    ReportError,
    ReportNotFoundError,
)
from archivability.lifecycle.models import LifecycleError
from archivability.methodology.catalog import MethodologyCatalog
from archivability.probes.models import ProbeValidationError
from archivability.probes.ports import Probe
from archivability.storage.audit import AuditContext

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_PERMISSION_PATTERN = re.compile(r'^[\x21\x23-\x5B\x5D-\x7E]{1,128}$')


class ApiValidationError(ValueError):
    """Raised when trusted adapter context is malformed."""


@dataclass(frozen=True, slots=True)
class ApiPrincipal:
    user_id: str
    session_id: str
    permissions: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for field in ("user_id", "session_id"):
            value = getattr(self, field)
            if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
                raise ApiValidationError(f"{field} is invalid")
        if not isinstance(self.permissions, frozenset) or any(
            not isinstance(value, str) or not _PERMISSION_PATTERN.fullmatch(value)
            for value in self.permissions
        ):
            raise ApiValidationError("permissions are invalid")


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
        if self.status_code not in {
            200,
            202,
            400,
            401,
            403,
            404,
            405,
            413,
            415,
            429,
            500,
        }:
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
        methodology_catalog: MethodologyCatalog,
        batch_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._workflow = workflow
        self._reports = reports
        self._probe = probe
        self._authorization = authorization
        self._rate_limiter = rate_limiter
        self._audit_recorder = audit_recorder
        if not isinstance(methodology_catalog, MethodologyCatalog):
            raise ApiValidationError("methodology catalog is invalid")
        self._methodology_catalog = methodology_catalog
        self._openapi_document = build_openapi_document(
            methodology_version=methodology_catalog.version
        )
        self._batch_id_factory = batch_id_factory or (
            lambda: f"batch-{uuid4().hex}"
        )

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
                owner_user_id=principal.user_id,
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
            "export_path": (
                f"/v1/analyses/{quote(outcome.analysis.analysis_id, safe='')}/export.json"
            ),
        }
        return self._response(
            context,
            operation="create",
            status_code=202,
            body=body,
            result="success",
            analysis_id=outcome.analysis.analysis_id,
        )

    def create_batch(
        self,
        payload: Mapping[str, Any],
        *,
        context: ApiRequestContext,
    ) -> ApiResponse:
        """Submit independent durable analyses through one bounded request."""
        denied = self._authenticate(context, "batch_create")
        if denied is not None:
            return denied
        principal = context.principal
        assert principal is not None
        try:
            authorized = self._authorization.can_create_analysis(principal)
        except Exception:
            return self._error(
                context,
                operation="batch_create",
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
            )
        if not authorized:
            return self._error(
                context,
                operation="batch_create",
                status_code=403,
                code="ACCESS_DENIED",
                message="Access denied.",
                result="unauthorized",
            )
        try:
            within_limit = self._rate_limiter.allow(principal, "analysis.batch_create")
        except Exception:
            return self._error(
                context,
                operation="batch_create",
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
            )
        if not within_limit:
            return self._error(
                context,
                operation="batch_create",
                status_code=429,
                code="RATE_LIMITED",
                message="Request limit exceeded.",
            )
        try:
            subjects = self._batch_subjects(payload)
            batch_id = self._batch_id_factory()
            if not isinstance(batch_id, str) or not _IDENTIFIER_PATTERN.fullmatch(
                batch_id
            ):
                raise ApiValidationError("batch identifier is invalid")
        except (ApiValidationError, LifecycleError):
            return self._error(
                context,
                operation="batch_create",
                status_code=400,
                code="INVALID_REQUEST",
                message="Request is invalid.",
            )

        items: list[dict[str, Any]] = []
        accepted = 0
        for position, subject_uri in enumerate(subjects):
            try:
                outcome = self._workflow.start(
                    subject_uri=subject_uri,
                    probe=self._probe,
                    owner_user_id=principal.user_id,
                    audit=context.audit_context(),
                )
            except Exception:
                items.append(
                    {
                        "position": position,
                        "status": "failed",
                        "error_code": "SUBMISSION_FAILED",
                    }
                )
                continue
            accepted += 1
            items.append(
                {
                    "position": position,
                    "status": outcome.status,
                    "analysis_id": outcome.analysis.analysis_id,
                    "report_path": (
                        f"/v1/analyses/{quote(outcome.analysis.analysis_id, safe='')}"
                    ),
                }
            )
        return self._response(
            context,
            operation="batch_create",
            status_code=202,
            body={
                "batch_schema_version": "1.0",
                "batch_id": batch_id,
                "accepted_count": accepted,
                "failed_count": len(items) - accepted,
                "items": items,
            },
            result="success",
        )

    def get_analysis(
        self,
        analysis_id: str,
        *,
        context: ApiRequestContext,
    ) -> ApiResponse:
        return self._get_analysis(analysis_id, context=context, operation="read")

    def export_analysis(
        self,
        analysis_id: str,
        *,
        context: ApiRequestContext,
    ) -> ApiResponse:
        return self._get_analysis(analysis_id, context=context, operation="export")

    def cancel_analysis(
        self,
        analysis_id: str,
        *,
        context: ApiRequestContext,
    ) -> ApiResponse:
        denied = self._authenticate(context, "cancel")
        if denied is not None:
            return denied
        if not isinstance(analysis_id, str) or not _IDENTIFIER_PATTERN.fullmatch(
            analysis_id
        ):
            return self._error(
                context,
                operation="cancel",
                status_code=400,
                code="INVALID_REQUEST",
                message="Request is invalid.",
            )
        principal = context.principal
        assert principal is not None
        try:
            authorized = (
                "analysis:cancel" in principal.permissions
                and self._authorization.can_read_analysis(principal, analysis_id)
            )
        except Exception:
            return self._error(
                context,
                operation="cancel",
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
                analysis_id=analysis_id,
            )
        if not authorized:
            return self._error(
                context,
                operation="cancel",
                status_code=403,
                code="ACCESS_DENIED",
                message="Access denied.",
                result="unauthorized",
                analysis_id=analysis_id,
            )
        try:
            within_limit = self._rate_limiter.allow(principal, "analysis.cancel")
        except Exception:
            return self._error(
                context,
                operation="cancel",
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
                analysis_id=analysis_id,
            )
        if not within_limit:
            return self._error(
                context,
                operation="cancel",
                status_code=429,
                code="RATE_LIMITED",
                message="Request limit exceeded.",
                analysis_id=analysis_id,
            )
        try:
            analysis = self._workflow.cancel(
                analysis_id, audit=context.audit_context()
            )
        except LifecycleError:
            return self._error(
                context,
                operation="cancel",
                status_code=400,
                code="INVALID_REQUEST",
                message="Request is invalid.",
                analysis_id=analysis_id,
            )
        except Exception:
            return self._error(
                context,
                operation="cancel",
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
                analysis_id=analysis_id,
            )
        return self._response(
            context,
            operation="cancel",
            status_code=200,
            body={"analysis_id": analysis.analysis_id, "state": analysis.state.value},
            result="success",
            analysis_id=analysis.analysis_id,
        )

    def list_analyses(self, *, context: ApiRequestContext) -> ApiResponse:
        denied = self._authenticate(context, "history")
        if denied is not None:
            return denied
        principal = context.principal
        assert principal is not None
        if not principal.permissions.intersection({"analysis:read", "analysis:read:any"}):
            return self._error(
                context,
                operation="history",
                status_code=403,
                code="ACCESS_DENIED",
                message="Access denied.",
                result="unauthorized",
            )
        try:
            within_limit = self._rate_limiter.allow(principal, "analysis.read")
        except Exception:
            return self._error(
                context,
                operation="history",
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
            )
        if not within_limit:
            return self._error(
                context,
                operation="history",
                status_code=429,
                code="RATE_LIMITED",
                message="Request limit exceeded.",
            )
        try:
            history = self._reports.list_history(
                principal.user_id,
                audit=context.audit_context(),
            )
        except ReportError:
            return self._error(
                context,
                operation="history",
                status_code=400,
                code="INVALID_REQUEST",
                message="Request is invalid.",
            )
        except Exception:
            return self._error(
                context,
                operation="history",
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
            )
        return self._response(
            context,
            operation="history",
            status_code=200,
            body={
                "history_schema_version": "1.1",
                "items": [item.to_dict() for item in history],
            },
            result="success",
        )

    def list_site_history(
        self,
        domain: str,
        *,
        context: ApiRequestContext,
    ) -> ApiResponse:
        denied = self._authenticate(context, "site_history")
        if denied is not None:
            return denied
        principal = context.principal
        assert principal is not None
        if not principal.permissions.intersection({"analysis:read", "analysis:read:any"}):
            return self._error(
                context,
                operation="site_history",
                status_code=403,
                code="ACCESS_DENIED",
                message="Access denied.",
                result="unauthorized",
            )
        try:
            within_limit = self._rate_limiter.allow(principal, "analysis.read")
        except Exception:
            return self._error(
                context,
                operation="site_history",
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
            )
        if not within_limit:
            return self._error(
                context,
                operation="site_history",
                status_code=429,
                code="RATE_LIMITED",
                message="Request limit exceeded.",
            )
        try:
            history = self._reports.list_site_history(
                domain,
                principal.user_id,
                audit=context.audit_context(),
            )
        except ReportError:
            return self._error(
                context,
                operation="site_history",
                status_code=400,
                code="INVALID_REQUEST",
                message="Request is invalid.",
            )
        except Exception:
            return self._error(
                context,
                operation="site_history",
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
            )
        strata: dict[tuple[str, str, tuple[str, ...]], int] = {}
        for item in history:
            key = (item.methodology_id, item.methodology_version, item.probe_ids)
            strata[key] = strata.get(key, 0) + 1
        return self._response(
            context,
            operation="site_history",
            status_code=200,
            body={
                "site_history_schema_version": "1.0",
                "strata": [
                    {
                        "methodology_id": methodology_id,
                        "methodology_version": methodology_version,
                        "probe_ids": list(probe_ids),
                        "item_count": item_count,
                    }
                    for (methodology_id, methodology_version, probe_ids), item_count
                    in sorted(strata.items())
                ],
                "items": [item.to_dict() for item in history],
            },
            result="success",
        )

    def get_methodology(self, *, context: ApiRequestContext) -> ApiResponse:
        denied = self._authorize_catalog_read(context, "methodology_read")
        if denied is not None:
            return denied
        return self._response(
            context,
            operation="methodology_read",
            status_code=200,
            body=self._methodology_catalog.methodology_document(),
            result="success",
        )

    def get_openapi(self, *, context: ApiRequestContext) -> ApiResponse:
        denied = self._authorize_catalog_read(context, "openapi_read")
        if denied is not None:
            return denied
        return self._response(
            context,
            operation="openapi_read",
            status_code=200,
            body=self._openapi_document,
            result="success",
        )

    def list_indicators(
        self,
        filters: Mapping[str, Any],
        *,
        context: ApiRequestContext,
    ) -> ApiResponse:
        denied = self._authorize_catalog_read(context, "indicator_list")
        if denied is not None:
            return denied
        try:
            dimension, classification = self._indicator_filters(filters)
        except ApiValidationError:
            return self._error(
                context,
                operation="indicator_list",
                status_code=400,
                code="INVALID_REQUEST",
                message="Request is invalid.",
            )
        items = self._methodology_catalog.list_indicators(
            dimension=dimension,
            classification=classification,
        )
        return self._response(
            context,
            operation="indicator_list",
            status_code=200,
            body={
                "indicator_catalog_schema_version": "1.0",
                "methodology_id": self._methodology_catalog.methodology_id,
                "methodology_version": self._methodology_catalog.version,
                "filters": {
                    "dimension": dimension,
                    "classification": classification,
                },
                "items": [item.to_dict() for item in items],
            },
            result="success",
        )

    def get_indicator(
        self,
        identifier: str,
        *,
        context: ApiRequestContext,
    ) -> ApiResponse:
        denied = self._authorize_catalog_read(context, "indicator_read")
        if denied is not None:
            return denied
        if not isinstance(identifier, str) or not _IDENTIFIER_PATTERN.fullmatch(
            identifier
        ):
            return self._error(
                context,
                operation="indicator_read",
                status_code=400,
                code="INVALID_REQUEST",
                message="Request is invalid.",
            )
        indicator = self._methodology_catalog.get_indicator(identifier)
        if indicator is None:
            return self._error(
                context,
                operation="indicator_read",
                status_code=404,
                code="NOT_FOUND",
                message="Indicator was not found.",
            )
        return self._response(
            context,
            operation="indicator_read",
            status_code=200,
            body={
                "indicator_schema_version": "1.0",
                "methodology_id": self._methodology_catalog.methodology_id,
                "methodology_version": self._methodology_catalog.version,
                "indicator": indicator.to_dict(),
            },
            result="success",
        )

    def compare_analysis(
        self,
        analysis_id: str,
        *,
        context: ApiRequestContext,
    ) -> ApiResponse:
        denied = self._authenticate(context, "compare")
        if denied is not None:
            return denied
        principal = context.principal
        assert principal is not None
        if not isinstance(analysis_id, str) or not _IDENTIFIER_PATTERN.fullmatch(
            analysis_id
        ):
            return self._error(
                context,
                operation="compare",
                status_code=400,
                code="INVALID_REQUEST",
                message="Request is invalid.",
            )
        try:
            authorized = self._authorization.can_read_analysis(principal, analysis_id)
        except Exception:
            return self._error(
                context,
                operation="compare",
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
                analysis_id=analysis_id,
            )
        if not authorized:
            return self._error(
                context,
                operation="compare",
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
                operation="compare",
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
                analysis_id=analysis_id,
            )
        if not within_limit:
            return self._error(
                context,
                operation="compare",
                status_code=429,
                code="RATE_LIMITED",
                message="Request limit exceeded.",
                analysis_id=analysis_id,
            )
        try:
            comparison = self._reports.compare_with_previous(
                analysis_id,
                principal.user_id,
                audit=context.audit_context(),
            )
        except ReportNotFoundError:
            return self._error(
                context,
                operation="compare",
                status_code=404,
                code="NOT_FOUND",
                message="Compatible comparison was not found.",
                analysis_id=analysis_id,
            )
        except ReportError:
            return self._error(
                context,
                operation="compare",
                status_code=400,
                code="INVALID_REQUEST",
                message="Request is invalid.",
                analysis_id=analysis_id,
            )
        except Exception:
            return self._error(
                context,
                operation="compare",
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
                analysis_id=analysis_id,
            )
        return self._response(
            context,
            operation="compare",
            status_code=200,
            body=comparison.to_dict(),
            result="success",
            analysis_id=analysis_id,
        )

    def _get_analysis(
        self,
        analysis_id: str,
        *,
        context: ApiRequestContext,
        operation: str,
    ) -> ApiResponse:
        denied = self._authenticate(context, operation)
        if denied is not None:
            return denied
        principal = context.principal
        assert principal is not None
        if not isinstance(analysis_id, str) or not _IDENTIFIER_PATTERN.fullmatch(
            analysis_id
        ):
            return self._error(
                context,
                operation=operation,
                status_code=400,
                code="INVALID_REQUEST",
                message="Request is invalid.",
            )
        try:
            authorized = self._authorization.can_read_analysis(principal, analysis_id)
        except Exception:
            return self._error(
                context,
                operation=operation,
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
                analysis_id=analysis_id,
            )
        if not authorized:
            return self._error(
                context,
                operation=operation,
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
                operation=operation,
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
                analysis_id=analysis_id,
            )
        if not within_limit:
            return self._error(
                context,
                operation=operation,
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
                operation=operation,
                status_code=404,
                code="NOT_FOUND",
                message="Analysis was not found.",
                analysis_id=analysis_id,
            )
        except ReportError:
            return self._error(
                context,
                operation=operation,
                status_code=400,
                code="INVALID_REQUEST",
                message="Request is invalid.",
                analysis_id=analysis_id,
            )
        except Exception:
            return self._error(
                context,
                operation=operation,
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
                analysis_id=analysis_id,
            )
        response = self._response(
            context,
            operation=operation,
            status_code=200,
            body=report.to_dict(),
            result="success",
            analysis_id=analysis_id,
        )
        if operation == "export":
            return ApiResponse(
                status_code=response.status_code,
                body=response.body,
                headers={
                    **response.headers,
                    "Content-Disposition": (
                        f'attachment; filename="analysis-{analysis_id}.json"'
                    ),
                },
            )
        return response

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

    def _authorize_catalog_read(
        self, context: ApiRequestContext, operation: str
    ) -> ApiResponse | None:
        denied = self._authenticate(context, operation)
        if denied is not None:
            return denied
        principal = context.principal
        assert principal is not None
        if not principal.permissions.intersection({"analysis:read", "analysis:read:any"}):
            return self._error(
                context,
                operation=operation,
                status_code=403,
                code="ACCESS_DENIED",
                message="Access denied.",
                result="unauthorized",
            )
        try:
            within_limit = self._rate_limiter.allow(principal, "methodology.read")
        except Exception:
            return self._error(
                context,
                operation=operation,
                status_code=500,
                code="INTERNAL_ERROR",
                message="Unable to process the request.",
            )
        if not within_limit:
            return self._error(
                context,
                operation=operation,
                status_code=429,
                code="RATE_LIMITED",
                message="Request limit exceeded.",
            )
        return None

    @staticmethod
    def _subject_uri(payload: Mapping[str, Any]) -> str:
        if not isinstance(payload, Mapping) or set(payload) != {"subject_uri"}:
            raise ApiValidationError("request body fields are invalid")
        value = payload["subject_uri"]
        if not isinstance(value, str) or not value or len(value) > 2048:
            raise ApiValidationError("subject_uri is invalid")
        return value

    @classmethod
    def _batch_subjects(cls, payload: Mapping[str, Any]) -> tuple[str, ...]:
        if not isinstance(payload, Mapping) or set(payload) != {"subjects"}:
            raise ApiValidationError("batch request body fields are invalid")
        values = payload["subjects"]
        if not isinstance(values, list) or not 1 <= len(values) <= 25:
            raise ApiValidationError("batch subjects must contain 1 to 25 items")
        subjects = tuple(cls._subject_uri(value) for value in values)
        if len(set(subjects)) != len(subjects):
            raise ApiValidationError("batch subjects must be unique")
        for subject in subjects:
            parsed = urlsplit(subject)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ApiValidationError("batch subject URI is invalid")
        return subjects

    def _indicator_filters(
        self, filters: Mapping[str, Any]
    ) -> tuple[str | None, str | None]:
        if not isinstance(filters, Mapping) or not set(filters) <= {
            "version",
            "dimension",
            "classification",
        }:
            raise ApiValidationError("indicator filters are invalid")
        version = filters.get("version")
        if version is not None and version != self._methodology_catalog.version:
            raise ApiValidationError("indicator version is unavailable")
        dimension = filters.get("dimension")
        classification = filters.get("classification")
        if dimension is not None and (
            not isinstance(dimension, str)
            or dimension not in {item["id"] for item in self._methodology_catalog.dimensions}
        ):
            raise ApiValidationError("indicator dimension is invalid")
        allowed_classifications = {
            "intrinsic",
            "harvest_dependent",
            "observation_context",
        }
        if classification is not None and classification not in allowed_classifications:
            raise ApiValidationError("indicator classification is invalid")
        return dimension, classification

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
