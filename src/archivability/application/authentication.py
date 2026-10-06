from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol
from uuid import uuid4

from archivability.application.api import (
    ApiPrincipal,
    ApiRequestContext,
    ApiValidationError,
)
from archivability.storage.audit import AuditContext


_PRINCIPAL_STATE_KEY = "archivability.principal"
_REQUEST_ID_STATE_KEY = "archivability.request_id"
_WWW_AUTHENTICATE = 'Bearer realm="arquivabilidade-ja", error="invalid_token"'


class BearerTokenVerifier(Protocol):
    """Cryptographic verification boundary for a deployment-specific token provider.

    Implementations must verify the signature with an explicit approved algorithm
    allowlist and validate issuer, audience, expiry and not-before claims before
    returning an ``ApiPrincipal``. Unverified token claims must never be returned.
    """

    async def verify(self, token: str) -> ApiPrincipal: ...


class AuthenticationAuditRecorder(Protocol):
    def record_bearer_authentication_event(
        self,
        *,
        request_id: str,
        result: str,
        error_code: str | None,
        audit: AuditContext,
    ) -> None: ...


class BearerAuthenticationMiddleware:
    """Authenticate one RFC 6750 Bearer header before invoking an ASGI app."""

    def __init__(
        self,
        app: Callable[
            [
                Mapping[str, Any],
                Callable[[], Awaitable[Mapping[str, Any]]],
                Callable[[Mapping[str, Any]], Awaitable[None]],
            ],
            Awaitable[None],
        ],
        *,
        verifier: BearerTokenVerifier,
        audit_recorder: AuthenticationAuditRecorder,
        max_token_bytes: int = 8192,
    ) -> None:
        if not isinstance(max_token_bytes, int) or not 256 <= max_token_bytes <= 16_384:
            raise ValueError("max_token_bytes must be between 256 and 16384")
        self._app = app
        self._verifier = verifier
        self._audit_recorder = audit_recorder
        self._max_token_bytes = max_token_bytes

    async def __call__(
        self,
        scope: Mapping[str, Any],
        receive: Callable[[], Awaitable[Mapping[str, Any]]],
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        trusted_scope, request_id, ip_address = self._prepare_scope(scope)
        try:
            token = self._bearer_token(scope)
        except ValueError:
            await self._authentication_failure(
                send,
                request_id=request_id,
                ip_address=ip_address,
            )
            return
        if token is None:
            await self._app(trusted_scope, receive, send)
            return

        try:
            principal = await self._verifier.verify(token)
            if not isinstance(principal, ApiPrincipal):
                raise ApiValidationError("verifier returned an invalid principal")
        except Exception:
            await self._authentication_failure(
                send,
                request_id=request_id,
                ip_address=ip_address,
            )
            return

        try:
            audit = AuditContext(
                user_id=principal.user_id,
                session_id=principal.session_id,
                ip_address=ip_address,
            )
            self._audit_recorder.record_bearer_authentication_event(
                request_id=request_id,
                result="success",
                error_code=None,
                audit=audit,
            )
        except Exception:
            await self._send_error(
                send,
                request_id=request_id,
                status_code=500,
                error_code="INTERNAL_ERROR",
                message="Unable to process the request.",
            )
            return

        state = dict(trusted_scope["state"])
        state[_PRINCIPAL_STATE_KEY] = principal
        authenticated_scope = dict(trusted_scope)
        authenticated_scope["state"] = state
        await self._app(authenticated_scope, receive, send)

    def _bearer_token(self, scope: Mapping[str, Any]) -> str | None:
        headers = scope.get("headers", ())
        if not isinstance(headers, (tuple, list)) or len(headers) > 64:
            raise ValueError("invalid ASGI headers")
        authorization: list[bytes] = []
        for item in headers:
            if (
                not isinstance(item, (tuple, list))
                or len(item) != 2
                or not isinstance(item[0], bytes)
                or not isinstance(item[1], bytes)
                or len(item[0]) > 256
                or len(item[1]) > self._max_token_bytes + 16
            ):
                raise ValueError("invalid ASGI header")
            if item[0].lower() == b"authorization":
                authorization.append(item[1])
        if not authorization:
            return None
        if len(authorization) != 1:
            raise ValueError("multiple authorization headers")
        try:
            value = authorization[0].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("authorization header is not ASCII") from exc
        match = re.fullmatch(r"(?i:Bearer) +([A-Za-z0-9\-._~+/]+=*)", value)
        if match is None:
            raise ValueError("authorization scheme is invalid")
        token = match.group(1)
        if len(token.encode("ascii")) > self._max_token_bytes:
            raise ValueError("bearer token is invalid")
        return token

    @staticmethod
    def _prepare_scope(
        scope: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str, str]:
        state = scope.get("state")
        state = dict(state) if isinstance(state, Mapping) else {}
        state.pop(_PRINCIPAL_STATE_KEY, None)
        request_id = state.get(_REQUEST_ID_STATE_KEY)
        if not isinstance(request_id, str):
            request_id = ""
        try:
            ApiRequestContext(
                request_id=request_id,
                ip_address="0.0.0.0",
                principal=None,
            )
        except ApiValidationError:
            request_id = str(uuid4())
        state[_REQUEST_ID_STATE_KEY] = request_id

        client = scope.get("client")
        ip_address = "0.0.0.0"
        if isinstance(client, (tuple, list)) and client and isinstance(client[0], str):
            try:
                ip_address = str(ipaddress.ip_address(client[0]))
            except ValueError:
                pass
        trusted_scope = dict(scope)
        trusted_scope["state"] = state
        return trusted_scope, request_id, ip_address

    async def _authentication_failure(
        self,
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
        *,
        request_id: str,
        ip_address: str,
    ) -> None:
        status_code = 401
        error_code = "INVALID_TOKEN"
        message = "Bearer token is invalid."
        try:
            self._audit_recorder.record_bearer_authentication_event(
                request_id=request_id,
                result="failure",
                error_code=error_code,
                audit=AuditContext(ip_address=ip_address),
            )
        except Exception:
            status_code = 500
            error_code = "INTERNAL_ERROR"
            message = "Unable to process the request."
        await self._send_error(
            send,
            request_id=request_id,
            status_code=status_code,
            error_code=error_code,
            message=message,
        )

    @staticmethod
    async def _send_error(
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
        *,
        request_id: str,
        status_code: int,
        error_code: str,
        message: str,
    ) -> None:
        body = json.dumps(
            {
                "error": {"code": error_code, "message": message},
                "request_id": request_id,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        headers = [
            (b"cache-control", b"no-store"),
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
            (b"referrer-policy", b"no-referrer"),
            (b"x-content-type-options", b"nosniff"),
            (b"x-request-id", request_id.encode("ascii")),
        ]
        if status_code == 401:
            headers.append((b"www-authenticate", _WWW_AUTHENTICATE.encode("ascii")))
        await send({"type": "http.response.start", "status": status_code, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})
