from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from archivability.probes.models import (
    ApprovedTarget,
    ProbeContext,
    ProbeValidationError,
)

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
class HttpTiming:
    """Timing for one network exchange without retaining the connected address."""

    total_milliseconds: float
    tcp_milliseconds: float | None
    tls_milliseconds: float | None
    ttfb_milliseconds: float | None
    address_family: str | None
    tcp_null_reason: str | None = None
    tls_null_reason: str | None = None
    ttfb_null_reason: str | None = None
    connection_reused: bool = False

    def __post_init__(self) -> None:
        for field, value in (
            ("total", self.total_milliseconds),
            ("tcp", self.tcp_milliseconds),
            ("tls", self.tls_milliseconds),
            ("ttfb", self.ttfb_milliseconds),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 <= value <= 120_000
            ):
                raise HttpTransportError(f"HTTP {field} timing is invalid")
        if self.address_family not in {None, "ipv4", "ipv6"}:
            raise HttpTransportError("HTTP address family is invalid")
        for value, reason in (
            (self.tcp_milliseconds, self.tcp_null_reason),
            (self.tls_milliseconds, self.tls_null_reason),
            (self.ttfb_milliseconds, self.ttfb_null_reason),
        ):
            if (value is None) != (reason is not None):
                raise HttpTransportError("HTTP timing null reason is inconsistent")


@dataclass(frozen=True, slots=True)
class HttpFetchResult:
    target: ApprovedTarget
    response: HttpResponse
    redirect_count: int
    elapsed_milliseconds: float = 0.0
    timings: tuple[HttpTiming, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.redirect_count, int) or self.redirect_count < 0:
            raise HttpTransportError("redirect_count must be a non-negative integer")
        if (
            isinstance(self.elapsed_milliseconds, bool)
            or not isinstance(self.elapsed_milliseconds, (int, float))
            or not 0 <= self.elapsed_milliseconds <= 120_000
        ):
            raise HttpTransportError("HTTP elapsed time is invalid")
        if not isinstance(self.timings, tuple) or any(
            not isinstance(item, HttpTiming) for item in self.timings
        ):
            raise HttpTransportError("HTTP timings must be an immutable tuple")


SocketFactory = Callable[..., socket.socket]
Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class _ExchangeResult:
    response: HttpResponse
    redirect_location: str | None
    timing: HttpTiming


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
        return self._fetch(context, required_origin=None)

    def fetch_same_origin(self, context: ProbeContext) -> HttpFetchResult:
        """Fetch while rejecting redirects outside the initially approved origin."""
        return self._fetch(context, required_origin=self._origin(context.target))

    def _fetch(
        self,
        context: ProbeContext,
        *,
        required_origin: tuple[str, str, int] | None,
    ) -> HttpFetchResult:
        if not isinstance(context, ProbeContext):
            raise ProbeValidationError("HTTP fetch requires an approved probe context")
        started_at = self._clock()
        current = context
        deadline = self._clock() + context.request.timeout_seconds
        timings: list[HttpTiming] = []
        while True:
            exchange = self._exchange(current, deadline)
            timings.append(exchange.timing)
            if exchange.redirect_location is None:
                return HttpFetchResult(
                    target=current.target,
                    response=exchange.response,
                    redirect_count=current.redirect_count,
                    elapsed_milliseconds=max(
                        0.0, (self._clock() - started_at) * 1000
                    ),
                    timings=tuple(timings),
                )
            redirected = current.redirect(exchange.redirect_location)
            if required_origin is not None and self._origin(redirected.target) != required_origin:
                raise ProbeValidationError("cross-origin crawl redirect is forbidden")
            current = redirected

    @staticmethod
    def _origin(target: ApprovedTarget) -> tuple[str, str, int]:
        return (target.scheme, target.hostname, target.port)

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
        exchange_started_at = self._clock()
        metrics: dict[str, object] = {}
        connection = http.client.HTTPConnection(
            target.hostname,
            target.port,
            timeout=context.request.timeout_seconds,
        )
        connection._create_connection = self._connector(  # type: ignore[attr-defined]
            target, deadline, metrics
        )
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
            network_socket = connection.sock
            response = connection.getresponse()
            headers_received_at = self._clock()
            location = response.getheader("Location")
            is_redirect = response.status in _REDIRECT_STATUSES and location is not None
            headers = self._safe_headers(response.getheaders())
            if is_redirect:
                body = b""
                truncated = False
            else:
                body, truncated = self._read_bounded(
                    response,
                    network_socket,
                    context.request.max_response_bytes,
                    deadline,
                )
            completed_at = self._clock()
            tls_milliseconds = metrics.get("tls_milliseconds")
            return _ExchangeResult(
                response=HttpResponse(
                    status_code=response.status,
                    reason=response.reason or "",
                    headers=headers,
                    body=body,
                    truncated=truncated,
                ),
                redirect_location=location if is_redirect else None,
                timing=HttpTiming(
                    total_milliseconds=max(
                        0.0, (completed_at - exchange_started_at) * 1000
                    ),
                    tcp_milliseconds=self._metric(metrics, "tcp_milliseconds"),
                    tls_milliseconds=self._metric(metrics, "tls_milliseconds"),
                    ttfb_milliseconds=max(
                        0.0, (headers_received_at - exchange_started_at) * 1000
                    ),
                    address_family=self._string_metric(metrics, "address_family"),
                    tcp_null_reason=(
                        None
                        if "tcp_milliseconds" in metrics
                        else "connection_phase_not_observed"
                    ),
                    tls_null_reason=(
                        None
                        if tls_milliseconds is not None
                        else (
                            "not_applicable_plain_http"
                            if target.scheme == "http"
                            else "tls_phase_not_observed"
                        )
                    ),
                    ttfb_null_reason=None,
                ),
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
        metrics: dict[str, object],
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
                connect_started_at = self._clock()
                try:
                    raw_socket = self._socket_factory(
                        (address, target.port),
                        timeout=remaining,
                        source_address=source_address,
                    )
                except OSError as exc:
                    last_error = exc
                    continue
                metrics["tcp_milliseconds"] = max(
                    0.0, (self._clock() - connect_started_at) * 1000
                )
                metrics["address_family"] = (
                    "ipv4"
                    if ipaddress.ip_address(address).version == 4
                    else "ipv6"
                )
                if target.scheme == "https":
                    try:
                        tls_started_at = self._clock()
                        wrapped = self._wrap_tls(raw_socket, target.hostname)
                        metrics["tls_milliseconds"] = max(
                            0.0, (self._clock() - tls_started_at) * 1000
                        )
                        return wrapped
                    except Exception:
                        raw_socket.close()
                        raise
                return raw_socket
            raise HttpTransportError("all approved addresses failed") from last_error

        return connect

    @staticmethod
    def _metric(metrics: dict[str, object], name: str) -> float | None:
        value = metrics.get(name)
        return float(value) if isinstance(value, (int, float)) else None

    @staticmethod
    def _string_metric(metrics: dict[str, object], name: str) -> str | None:
        value = metrics.get(name)
        return value if isinstance(value, str) else None

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
            if response.isclosed():
                break
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
