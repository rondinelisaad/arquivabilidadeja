from __future__ import annotations

import ipaddress
import re
from collections.abc import Awaitable, Callable, Mapping
from importlib.resources import files
from typing import Any, Protocol
from uuid import uuid4

from archivability.application.api import ApiPrincipal
from archivability.storage.audit import AuditContext

_ANALYSIS_PAGE = re.compile(r"^/analyses/[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_INDICATOR_PAGE = re.compile(
    r"^/methodology/indicators/[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
)
_REQUEST_ID_STATE_KEY = "archivability.request_id"
_PRINCIPAL_STATE_KEY = "archivability.principal"
_PAGE_HEADERS = (
    (b"cache-control", b"no-store"),
    (b"content-type", b"text/html; charset=utf-8"),
    (
        b"content-security-policy",
        b"default-src 'none'; script-src 'self'; style-src 'self'; "
        b"connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
        b"form-action 'self'; frame-ancestors 'none'",
    ),
    (b"referrer-policy", b"no-referrer"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
)


class WebAuditRecorder(Protocol):
    def record_web_ui_event(
        self,
        *,
        request_id: str,
        route: str,
        result: str,
        status_code: int,
        error_code: str | None,
        audit: AuditContext,
    ) -> None: ...


class WebAsgiApp:
    """Serve the same-origin browser shell and delegate data routes to the API."""

    def __init__(
        self,
        api_app: Callable[
            [
                Mapping[str, Any],
                Callable[[], Awaitable[Mapping[str, Any]]],
                Callable[[Mapping[str, Any]], Awaitable[None]],
            ],
            Awaitable[None],
        ],
        *,
        audit_recorder: WebAuditRecorder,
    ) -> None:
        self._api_app = api_app
        self._audit_recorder = audit_recorder
        resources = files("archivability.web")
        self._index = resources.joinpath("index.html").read_bytes()
        self._styles = resources.joinpath("app.css").read_bytes()
        self._script = resources.joinpath("app.js").read_bytes()
        self._favicon = resources.joinpath("favicon.svg").read_bytes()

    async def __call__(
        self,
        scope: Mapping[str, Any],
        receive: Callable[[], Awaitable[Mapping[str, Any]]],
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self._api_app(scope, receive, send)
            return
        path = scope.get("path")
        method = scope.get("method")
        path = path if isinstance(path, str) else ""
        method = method if isinstance(method, str) else ""
        if path == "/assets/app.css":
            await self._asset(send, method, self._styles, b"text/css; charset=utf-8")
            return
        if path == "/assets/app.js":
            await self._asset(
                send, method, self._script, b"text/javascript; charset=utf-8"
            )
            return
        if path == "/favicon.svg":
            await self._asset(send, method, self._favicon, b"image/svg+xml")
            return
        route = (
            "home"
            if path == "/"
            else "analysis"
            if _ANALYSIS_PAGE.fullmatch(path)
            else "methodology"
            if path == "/methodology"
            else "indicator_catalog"
            if path == "/methodology/indicators"
            else "indicator"
            if _INDICATOR_PAGE.fullmatch(path)
            else None
        )
        if route is None:
            await self._api_app(scope, receive, send)
            return
        request_id, audit = self._request_context(scope)
        if method not in {"GET", "HEAD"}:
            await self._record_and_send(
                send,
                request_id=request_id,
                route=route,
                result="failure",
                status_code=405,
                error_code="METHOD_NOT_ALLOWED",
                audit=audit,
                body=b"Method not allowed.\n",
                headers=((b"allow", b"GET, HEAD"),),
                head=method == "HEAD",
            )
            return
        await self._record_and_send(
            send,
            request_id=request_id,
            route=route,
            result="success",
            status_code=200,
            error_code=None,
            audit=audit,
            body=self._index,
            headers=(),
            head=method == "HEAD",
        )

    async def _record_and_send(
        self,
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
        *,
        request_id: str,
        route: str,
        result: str,
        status_code: int,
        error_code: str | None,
        audit: AuditContext,
        body: bytes,
        headers: tuple[tuple[bytes, bytes], ...],
        head: bool,
    ) -> None:
        try:
            self._audit_recorder.record_web_ui_event(
                request_id=request_id,
                route=route,
                result=result,
                status_code=status_code,
                error_code=error_code,
                audit=audit,
            )
        except Exception:
            status_code = 500
            body = b"Unable to serve the interface.\n"
            headers = ()
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [*_PAGE_HEADERS, *headers, (b"x-request-id", request_id.encode("ascii"))],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"" if head else body,
                "more_body": False,
            }
        )

    @staticmethod
    async def _asset(
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
        method: str,
        body: bytes,
        content_type: bytes,
    ) -> None:
        if method not in {"GET", "HEAD"}:
            status = 405
            body = b"Method not allowed.\n"
        else:
            status = 200
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"cache-control", b"public, max-age=3600"),
                    (b"content-type", content_type),
                    (b"x-content-type-options", b"nosniff"),
                    *(((b"allow", b"GET, HEAD"),) if status == 405 else ()),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"" if method == "HEAD" else body,
                "more_body": False,
            }
        )

    @staticmethod
    def _request_context(scope: Mapping[str, Any]) -> tuple[str, AuditContext]:
        state = scope.get("state")
        state = state if isinstance(state, Mapping) else {}
        request_id = state.get(_REQUEST_ID_STATE_KEY)
        if (
            not isinstance(request_id, str)
            or not request_id
            or len(request_id) > 256
            or any(ord(character) < 33 or ord(character) > 126 for character in request_id)
        ):
            request_id = str(uuid4())
        principal = state.get(_PRINCIPAL_STATE_KEY)
        principal = principal if isinstance(principal, ApiPrincipal) else None
        ip_address = None
        client = scope.get("client")
        if isinstance(client, (tuple, list)) and client and isinstance(client[0], str):
            try:
                ip_address = str(ipaddress.ip_address(client[0]))
            except ValueError:
                pass
        return request_id, AuditContext(
            user_id=principal.user_id if principal else None,
            session_id=principal.session_id if principal else None,
            ip_address=ip_address,
        )
