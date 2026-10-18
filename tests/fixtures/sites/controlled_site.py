from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class FixtureResponse:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/plain"
    headers: tuple[tuple[str, str], ...] = ()
    delay_seconds: float = 0.0


class _FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, routes: dict[str, FixtureResponse]) -> None:
        self.routes = routes
        self.requests: list[str] = []
        self.request_headers: list[dict[str, str]] = []
        self.lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _FixtureHandler)


class _FixtureHandler(BaseHTTPRequestHandler):
    server: _FixtureServer

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        with self.server.lock:
            self.server.requests.append(path)
            self.server.request_headers.append(
                {name.lower(): value for name, value in self.headers.items()}
            )
        response = self.server.routes.get(
            path,
            FixtureResponse(status=404, body=b"not found"),
        )
        if response.delay_seconds:
            time.sleep(response.delay_seconds)
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(response.body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class ControlledSite:
    """HTTP fixture reached through a socket mapper, never production SSRF policy."""

    public_address = "93.184.216.34"
    hostname = "fixture.test"

    def __init__(self, routes: dict[str, FixtureResponse]) -> None:
        self._server = _FixtureServer(routes)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="controlled-site-fixture",
            daemon=True,
        )
        self.socket_calls: list[tuple[str, int]] = []

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def requests(self) -> tuple[str, ...]:
        with self._server.lock:
            return tuple(self._server.requests)

    @property
    def request_headers(self) -> tuple[dict[str, str], ...]:
        with self._server.lock:
            return tuple(dict(item) for item in self._server.request_headers)

    def url(self, path: str = "/", *, hostname: str | None = None) -> str:
        authority = hostname or self.hostname
        return f"http://{authority}:{self.port}{path}"

    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        if hostname not in {self.hostname, "other.test"} or port != self.port:
            return ()
        return (self.public_address,)

    def set_route(self, path: str, response: FixtureResponse) -> None:
        with self._server.lock:
            self._server.routes[path] = response

    def socket_factory(
        self,
        address: tuple[str, int],
        *,
        timeout: float,
        source_address: tuple[str, int] | None,
    ) -> socket.socket:
        if address != (self.public_address, self.port):
            raise OSError("fixture received an unexpected approved destination")
        self.socket_calls.append(address)
        return socket.create_connection(
            ("127.0.0.1", self.port),
            timeout=timeout,
            source_address=source_address,
        )

    def __enter__(self) -> ControlledSite:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
