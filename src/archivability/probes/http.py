from __future__ import annotations

import http.client
import re
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from archivability.probes.models import ApprovedTarget, ProbeContext, ProbeValidationError


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_SAFE_RESPONSE_HEADERS = frozenset(
    {
        "accept-ranges",
        "age",
        "cache-control",
        "content-encoding",
        "content-language",
        "content-length",
        "content-type",
        "date",
        "etag",
        "expires",
        "last-modified",
        "link",
        "server",
        "vary",
    }
)
_HEADER_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_MAX_HEADER_BYTES = 64 * 1024
_MAX_HEADER_VALUE_BYTES = 8 * 1024
_READ_CHUNK_SIZE = 64 * 1024


class HttpTransportError(RuntimeError):
    """Raised when a bounded, pinned HTTP exchange cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    reason: str
    headers: tuple[tuple[str, str], ...]
    body: bytes
    truncated: bool = False

    def __post_init__(self) -> None:
        if not 100 <= self.status_code <= 599:
            raise HttpTransportError("HTTP response status is invalid")
        if len(self.reason) > 256 or _HEADER_CONTROL_PATTERN.search(self.reason):
            raise HttpTransportError("HTTP response reason is invalid")
        if not isinstance(self.headers, tuple) or any(
            not isinstance(item, tuple) or len(item) != 2 for item in self.headers
        ):
            raise HttpTransportError("HTTP response headers must be immutable pairs")
        if not isinstance(self.body, bytes):
            raise HttpTransportError("HTTP response body must be bytes")

    def header_values(self, name: str) -> tuple[str, ...]:
        candidate = name.lower()
        return tuple(value for key, value in self.headers if key == candidate)


@dataclass(frozen=True, slots=True)
class HttpFetchResult:
    target: ApprovedTarget
    response: HttpResponse
    redirect_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.redirect_count, int) or self.redirect_count < 0:
            raise HttpTransportError("redirect_count must be a non-negative integer")


SocketFactory = Callable[..., socket.socket]
Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class _ExchangeResult:
    response: HttpResponse
    redirect_location: str | None


class PinnedHttpClient:
    """Minimal HTTP client that connects only to addresses approved by SsrfPolicy."""

    user_agent = "arquivabilidade-ja/0.1"

    def __init__(
        self,
        *,
        ssl_context: ssl.SSLContext | None = None,
        socket_factory: SocketFactory = socket.create_connection,
        clock: Clock = time.monotonic,
    ) -> None:
        context = ssl_context or ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        if ssl_context is None:
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._validate_ssl_context(context)
        self._ssl_context = context
        self._socket_factory = socket_factory
        self._clock = clock

    def fetch(self, context: ProbeContext) -> HttpFetchResult:
        if not isinstance(context, ProbeContext):
            raise ProbeValidationError("HTTP fetch requires an approved probe context")
        current = context
        deadline = self._clock() + context.request.timeout_seconds
        while True:
            exchange = self._exchange(current, deadline)
            if exchange.redirect_location is None:
                return HttpFetchResult(
                    target=current.target,
                    response=exchange.response,
                    redirect_count=current.redirect_count,
                )
            current = current.redirect(exchange.redirect_location)

    @staticmethod
    def _validate_ssl_context(context: ssl.SSLContext) -> None:
        if not isinstance(context, ssl.SSLContext):
            raise TypeError("ssl_context must be an SSLContext")
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError("TLS must verify both the certificate and hostname")
        if context.minimum_version < ssl.TLSVersion.TLSv1_2:
            raise ValueError("TLS minimum version must be TLS 1.2 or newer")

    def _exchange(self, context: ProbeContext, deadline: float) -> _ExchangeResult:
        target = context.target
        connection = http.client.HTTPConnection(
            target.hostname,
            target.port,
            timeout=context.request.timeout_seconds,
        )
        connection._create_connection = self._connector(target, deadline)  # type: ignore[attr-defined]
        response: http.client.HTTPResponse | None = None
        try:
            request_target = self._request_target(target)
            connection.request(
                "GET",
                request_target,
                headers={
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                    "User-Agent": self.user_agent,
                },
            )
            self._set_remaining_timeout(connection.sock, deadline)
            response = connection.getresponse()
            location = response.getheader("Location")
            is_redirect = response.status in _REDIRECT_STATUSES and location is not None
            headers = self._safe_headers(response.getheaders())
            if is_redirect:
                body = b""
                truncated = False
            else:
                body, truncated = self._read_bounded(
                    response,
                    connection.sock,
                    context.request.max_response_bytes,
                    deadline,
                )
            return _ExchangeResult(
                response=HttpResponse(
                    status_code=response.status,
                    reason=response.reason or "",
                    headers=headers,
                    body=body,
                    truncated=truncated,
                ),
                redirect_location=location if is_redirect else None,
            )
        except (OSError, ssl.SSLError, http.client.HTTPException, UnicodeError) as exc:
            raise HttpTransportError(type(exc).__name__) from exc
        finally:
            if response is not None:
                response.close()
            connection.close()

    def _connector(
        self,
        target: ApprovedTarget,
        deadline: float,
    ) -> Callable[..., socket.socket]:
        def connect(
            _authority: tuple[str, int],
            timeout: float | object | None = None,
            source_address: tuple[str, int] | None = None,
        ) -> socket.socket:
            del timeout
            last_error: OSError | None = None
            for address in target.addresses:
                remaining = self._remaining(deadline)
                try:
                    raw_socket = self._socket_factory(
                        (address, target.port),
                        timeout=remaining,
                        source_address=source_address,
                    )
                except OSError as exc:
                    last_error = exc
                    continue
                if target.scheme == "https":
                    try:
                        return self._wrap_tls(raw_socket, target.hostname)
                    except Exception:
                        raw_socket.close()
                        raise
                return raw_socket
            raise HttpTransportError("all approved addresses failed") from last_error

        return connect

    def _wrap_tls(self, raw_socket: socket.socket, hostname: str) -> socket.socket:
        return self._ssl_context.wrap_socket(raw_socket, server_hostname=hostname)

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("HTTP request deadline exceeded")
        return remaining

    def _set_remaining_timeout(
        self,
        network_socket: socket.socket | None,
        deadline: float,
    ) -> None:
        if network_socket is None:
            raise HttpTransportError("HTTP connection did not create a socket")
        network_socket.settimeout(self._remaining(deadline))

    def _read_bounded(
        self,
        response: http.client.HTTPResponse,
        network_socket: socket.socket | None,
        maximum_bytes: int,
        deadline: float,
    ) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        total = 0
        while total <= maximum_bytes:
            self._set_remaining_timeout(network_socket, deadline)
            chunk = response.read(min(_READ_CHUNK_SIZE, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        body = b"".join(chunks)
        if len(body) > maximum_bytes:
            return body[:maximum_bytes], True
        return body, False

    @staticmethod
    def _request_target(target: ApprovedTarget) -> str:
        parsed = urlsplit(target.normalized_uri)
        request_target = parsed.path or "/"
        if parsed.query:
            request_target = f"{request_target}?{parsed.query}"
        try:
            request_target.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ProbeValidationError(
                "HTTP request path and query must be ASCII or percent-encoded"
            ) from exc
        return request_target

    @staticmethod
    def _safe_headers(
        raw_headers: list[tuple[str, str]],
    ) -> tuple[tuple[str, str], ...]:
        safe: list[tuple[str, str]] = []
        total_bytes = 0
        for raw_name, raw_value in raw_headers:
            name = raw_name.lower()
            value = raw_value.strip()
            if _HEADER_CONTROL_PATTERN.search(value):
                raise HttpTransportError("HTTP response contains an unsafe header value")
            value_bytes = len(value.encode("utf-8"))
            if value_bytes > _MAX_HEADER_VALUE_BYTES:
                raise HttpTransportError("HTTP response header value is too large")
            total_bytes += len(name) + value_bytes
            if total_bytes > _MAX_HEADER_BYTES:
                raise HttpTransportError("HTTP response headers are too large")
            if name in _SAFE_RESPONSE_HEADERS:
                safe.append((name, value))
        return tuple(safe)
