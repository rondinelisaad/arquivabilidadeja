from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from archivability.application.api import (
    AnalysisApi,
    ApiPrincipal,
    ApiRequestContext,
    ApiResponse,
    ApiValidationError,
)
from archivability.storage.audit import AuditContext


_ANALYSIS_PATH = re.compile(r"^/v1/analyses/([^/]+)$")
_COLLECTION_PATH = "/v1/analyses"
_PRINCIPAL_STATE_KEY = "archivability.principal"
_REQUEST_ID_STATE_KEY = "archivability.request_id"
_SAFE_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Type": "application/json; charset=utf-8",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


class AsgiAuditRecorder(Protocol):
    def record_analysis_http_event(
        self,
        *,
        request_id: str,
        route: str,
        method: str,
        result: str,
        status_code: int,
        error_code: str,
        audit: AuditContext,
    ) -> None: ...


class AsgiSyncRunner(Protocol):
    async def run(self, operation: Callable[[], ApiResponse]) -> ApiResponse: ...


class InlineAsgiSyncRunner:
    """Run the synchronous application core in the current ASGI worker."""

    async def run(self, operation: Callable[[], ApiResponse]) -> ApiResponse:
        return operation()


@dataclass(frozen=True, slots=True)
class _RequestFailure(Exception):
    status_code: int
    error_code: str
    message: str


class AnalysisAsgiApp:
    """Dependency-free ASGI HTTP adapter for the analysis API.

    Authentication is deliberately outside this adapter. A trusted middleware must
    place an ``ApiPrincipal`` in ``scope["state"]["archivability.principal"]``.
    Client-supplied identity and request-id headers are never interpreted.
    """

    def __init__(
        self,
        *,
        api: AnalysisApi,
        audit_recorder: AsgiAuditRecorder,
        max_body_bytes: int = 4096,
        sync_runner: AsgiSyncRunner | None = None,
    ) -> None:
        if not isinstance(max_body_bytes, int) or not 256 <= max_body_bytes <= 65_536:
            raise ValueError("max_body_bytes must be between 256 and 65536")
        self._api = api
        self._audit_recorder = audit_recorder
        self._max_body_bytes = max_body_bytes
        self._sync_runner = sync_runner or InlineAsgiSyncRunner()

    async def __call__(
        self,
        scope: Mapping[str, Any],
        receive: Callable[[], Awaitable[Mapping[str, Any]]],
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            raise ValueError("AnalysisAsgiApp only accepts ASGI HTTP scopes")

        context = self._context(scope)
        method = scope.get("method")
        path = scope.get("path")
        method = method if isinstance(method, str) else ""
        path = path if isinstance(path, str) else ""
        route, analysis_id = self._route(path)

        try:
            if route == "analyses_collection":
                if method != "POST":
                    raise _RequestFailure(405, "METHOD_NOT_ALLOWED", "Method is not allowed.")
                if context.principal is None:
                    response = await self._sync_runner.run(
                        lambda: self._api.create_analysis({}, context=context)
                    )
                else:
                    payload = await self._read_json_object(scope, receive)
                    response = await self._sync_runner.run(
                        lambda: self._api.create_analysis(payload, context=context)
                    )
            elif route == "analysis_item":
                if method != "GET":
                    raise _RequestFailure(405, "METHOD_NOT_ALLOWED", "Method is not allowed.")
                assert analysis_id is not None
                response = await self._sync_runner.run(
                    lambda: self._api.get_analysis(analysis_id, context=context)
                )
            else:
                raise _RequestFailure(404, "NOT_FOUND", "Resource was not found.")
        except _RequestFailure as exc:
            response = self._transport_error(
                context=context,
                route=route,
                method=method,
                status_code=exc.status_code,
                error_code=exc.error_code,
                message=exc.message,
            )
        except Exception:
            response = self._transport_error(
                context=context,
                route=route,
                method=method,
                status_code=500,
                error_code="INTERNAL_ERROR",
                message="Unable to process the request.",
            )

        await self._send_response(send, response, request_id=context.request_id)

    @staticmethod
    def _context(scope: Mapping[str, Any]) -> ApiRequestContext:
        state = scope.get("state")
        state = state if isinstance(state, Mapping) else {}
        principal = state.get(_PRINCIPAL_STATE_KEY)
        if not isinstance(principal, ApiPrincipal):
            principal = None

        request_id = state.get(_REQUEST_ID_STATE_KEY)
        request_id = request_id if isinstance(request_id, str) else ""
        ip_address = AnalysisAsgiApp._client_ip(scope)
        try:
            return ApiRequestContext(
                request_id=request_id,
                ip_address=ip_address,
                principal=principal,
            )
        except ApiValidationError:
            return ApiRequestContext(
                request_id=str(uuid4()),
                ip_address=ip_address,
                principal=principal,
            )

    @staticmethod
    def _client_ip(scope: Mapping[str, Any]) -> str:
        client = scope.get("client")
        if isinstance(client, (tuple, list)) and client and isinstance(client[0], str):
            candidate = client[0]
            try:
                ApiRequestContext(
                    request_id="ip-validation",
                    ip_address=candidate,
                    principal=None,
                )
            except ApiValidationError:
                pass
            else:
                return candidate
        return "0.0.0.0"

    @staticmethod
    def _route(path: str) -> tuple[str, str | None]:
        if path == _COLLECTION_PATH:
            return "analyses_collection", None
        match = _ANALYSIS_PATH.fullmatch(path)
        if match is not None:
            return "analysis_item", match.group(1)
        return "unmatched", None

    async def _read_json_object(
        self,
        scope: Mapping[str, Any],
        receive: Callable[[], Awaitable[Mapping[str, Any]]],
    ) -> Mapping[str, Any]:
        expected_length = self._validate_headers(scope)
        chunks: list[bytes] = []
        size = 0
        while True:
            event = await receive()
            event_type = event.get("type") if isinstance(event, Mapping) else None
            if event_type == "http.disconnect":
                raise _RequestFailure(400, "INVALID_REQUEST", "Request is invalid.")
            if event_type != "http.request":
                raise _RequestFailure(400, "INVALID_REQUEST", "Request is invalid.")
            chunk = event.get("body", b"")
            more_body = event.get("more_body", False)
            if not isinstance(chunk, bytes) or not isinstance(more_body, bool):
                raise _RequestFailure(400, "INVALID_REQUEST", "Request is invalid.")
            size += len(chunk)
            if size > self._max_body_bytes:
                raise _RequestFailure(413, "PAYLOAD_TOO_LARGE", "Request body is too large.")
            chunks.append(chunk)
            if not more_body:
                break

        if expected_length is not None and size != expected_length:
            raise _RequestFailure(400, "INVALID_REQUEST", "Request is invalid.")
        try:
            payload = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _RequestFailure(400, "INVALID_JSON", "Request body is not valid JSON.")
        if not isinstance(payload, dict):
            raise _RequestFailure(400, "INVALID_JSON", "Request body must be a JSON object.")
        return payload

    def _validate_headers(self, scope: Mapping[str, Any]) -> int | None:
        headers = scope.get("headers", ())
        if not isinstance(headers, (tuple, list)) or len(headers) > 64:
            raise _RequestFailure(400, "INVALID_REQUEST", "Request is invalid.")
        values: dict[bytes, list[bytes]] = {}
        for item in headers:
            if (
                not isinstance(item, (tuple, list))
                or len(item) != 2
                or not isinstance(item[0], bytes)
                or not isinstance(item[1], bytes)
                or len(item[0]) > 256
                or len(item[1]) > 8192
            ):
                raise _RequestFailure(400, "INVALID_REQUEST", "Request is invalid.")
            values.setdefault(item[0].lower(), []).append(item[1])

        content_types = values.get(b"content-type", [])
        if len(content_types) != 1 or not self._is_json_content_type(content_types[0]):
            raise _RequestFailure(
                415,
                "UNSUPPORTED_MEDIA_TYPE",
                "Content-Type must be application/json with UTF-8 encoding.",
            )
        encodings = values.get(b"content-encoding", [])
        if len(encodings) > 1 or (encodings and encodings[0].strip().lower() != b"identity"):
            raise _RequestFailure(415, "UNSUPPORTED_MEDIA_TYPE", "Content encoding is unsupported.")

        lengths = values.get(b"content-length", [])
        if not lengths:
            return None
        if len(lengths) != 1:
            raise _RequestFailure(400, "INVALID_REQUEST", "Request is invalid.")
        try:
            raw_length = lengths[0].decode("ascii")
            if not raw_length.isdecimal():
                raise ValueError
            length = int(raw_length)
        except (UnicodeDecodeError, ValueError):
            raise _RequestFailure(400, "INVALID_REQUEST", "Request is invalid.")
        if length > self._max_body_bytes:
            raise _RequestFailure(413, "PAYLOAD_TOO_LARGE", "Request body is too large.")
        return length

    @staticmethod
    def _is_json_content_type(value: bytes) -> bool:
        try:
            parts = [part.strip().lower() for part in value.decode("ascii").split(";")]
        except UnicodeDecodeError:
            return False
        if not parts or parts[0] != "application/json":
            return False
        for parameter in parts[1:]:
            if parameter != "charset=utf-8":
                return False
        return True

    def _transport_error(
        self,
        *,
        context: ApiRequestContext,
        route: str,
        method: str,
        status_code: int,
        error_code: str,
        message: str,
    ) -> ApiResponse:
        try:
            self._audit_recorder.record_analysis_http_event(
                request_id=context.request_id,
                route=route,
                method=method if method in _SAFE_METHODS else "OTHER",
                result="failure",
                status_code=status_code,
                error_code=error_code,
                audit=context.audit_context(),
            )
        except Exception:
            status_code = 500
            error_code = "INTERNAL_ERROR"
            message = "Unable to process the request."
        headers = dict(_SECURITY_HEADERS)
        if status_code == 405:
            headers["Allow"] = "POST" if route == "analyses_collection" else "GET"
        return ApiResponse(
            status_code=status_code,
            body={
                "error": {"code": error_code, "message": message},
                "request_id": context.request_id,
            },
            headers=headers,
        )

    @staticmethod
    async def _send_response(
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
        response: ApiResponse,
        *,
        request_id: str,
    ) -> None:
        body = json.dumps(
            dict(response.body),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {**_SECURITY_HEADERS, **dict(response.headers), "X-Request-ID": request_id}
        if response.status_code == 401:
            headers["WWW-Authenticate"] = 'Bearer realm="arquivabilidade-ja"'
        await send(
            {
                "type": "http.response.start",
                "status": response.status_code,
                "headers": [
                    (name.lower().encode("ascii"), value.encode("ascii"))
                    for name, value in headers.items()
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})
