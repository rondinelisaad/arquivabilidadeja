from __future__ import annotations

import io
import socket
import ssl
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from archivability.probes import (  # noqa: E402
    HttpTransportError,
    PinnedHttpClient,
    ProbeContext,
    ProbeRequest,
    ProbeValidationError,
    SsrfPolicy,
    SystemAddressResolver,
)


NOW = datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc)
PUBLIC_V4 = "93.184.216.34"
PUBLIC_V4_REDIRECT = "142.250.219.14"


class StaticResolver:
    def __init__(self, values: dict[str, tuple[str, ...]]) -> None:
        self.values = values
        self.calls: list[tuple[str, int]] = []

    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        self.calls.append((hostname, port))
        return self.values.get(hostname, ())


class FakeSocket:
    def __init__(self, response: bytes) -> None:
        self._response = io.BytesIO(response)
        self.sent = bytearray()
        self.timeouts: list[float | None] = []
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def makefile(self, mode: str, buffering: int | None = None) -> io.BytesIO:
        del mode, buffering
        return self._response

    def settimeout(self, value: float | None) -> None:
        self.timeouts.append(value)

    def setsockopt(self, level: int, option: int, value: int) -> None:
        del level, option, value

    def close(self) -> None:
        self.closed = True


class FakeSocketFactory:
    def __init__(self, responses: list[bytes]) -> None:
        self._responses = iter(responses)
        self.calls: list[tuple[tuple[str, int], float, tuple[str, int] | None]] = []
        self.sockets: list[FakeSocket] = []

    def __call__(
        self,
        address: tuple[str, int],
        *,
        timeout: float,
        source_address: tuple[str, int] | None,
    ) -> FakeSocket:
        self.calls.append((address, timeout, source_address))
        network_socket = FakeSocket(next(self._responses))
        self.sockets.append(network_socket)
        return network_socket


def make_context(
    uri: str,
    resolver: StaticResolver,
    *,
    max_response_bytes: int = 1024,
    max_redirects: int = 3,
) -> ProbeContext:
    request = ProbeRequest(
        analysis_id="analysis-1",
        attempt_id="attempt-1",
        subject_uri=uri,
        requested_at=NOW,
        timeout_seconds=2,
        max_response_bytes=max_response_bytes,
        max_redirects=max_redirects,
    )
    policy = SsrfPolicy()
    return ProbeContext(
        request=request,
        target=policy.approve(uri, resolver),
        policy=policy,
        resolver=resolver,
    )


class SystemAddressResolverTests(unittest.TestCase):
    def test_returns_unique_canonical_a_and_aaaa_addresses(self) -> None:
        calls: list[tuple[object, ...]] = []

        def lookup(*args: object, **kwargs: object) -> list[tuple[object, ...]]:
            calls.append((*args, kwargs))
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC_V4, 443)),
                (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2606:4700:4700::1111", 443, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC_V4, 443)),
            ]

        resolver = SystemAddressResolver(lookup=lookup)
        self.assertEqual(
            (PUBLIC_V4, "2606:4700:4700::1111"),
            resolver.resolve("example.org", 443),
        )
        self.assertEqual("example.org", calls[0][0])
        self.assertEqual(socket.SOCK_STREAM, calls[0][-1]["type"])

    def test_wraps_lookup_errors_without_disclosing_details(self) -> None:
        def failing_lookup(*args: object, **kwargs: object) -> list[tuple[object, ...]]:
            del args, kwargs
            raise socket.gaierror("sensitive resolver detail")

        with self.assertRaisesRegex(ProbeValidationError, "hostname resolution failed"):
            SystemAddressResolver(lookup=failing_lookup).resolve("example.org", 443)


class PinnedHttpClientTests(unittest.TestCase):
    def test_connects_to_pinned_ip_and_preserves_host_header(self) -> None:
        resolver = StaticResolver({"example.org": (PUBLIC_V4,)})
        context = make_context("http://example.org/path?q=1", resolver)
        sockets = FakeSocketFactory(
            [b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nSet-Cookie: secret=value\r\nContent-Length: 5\r\n\r\nhello"]
        )

        result = PinnedHttpClient(socket_factory=sockets).fetch(context)

        self.assertEqual(((PUBLIC_V4, 80),), tuple(call[0] for call in sockets.calls))
        request = bytes(sockets.sockets[0].sent)
        self.assertIn(b"GET /path?q=1 HTTP/1.1\r\n", request)
        self.assertIn(b"Host: example.org\r\n", request)
        self.assertNotIn(b"Set-Cookie", request)
        self.assertEqual(b"hello", result.response.body)
        self.assertEqual(("text/plain",), result.response.header_values("Content-Type"))
        self.assertEqual((), result.response.header_values("Set-Cookie"))

    def test_truncates_body_at_request_limit(self) -> None:
        resolver = StaticResolver({"example.org": (PUBLIC_V4,)})
        context = make_context(
            "http://example.org/",
            resolver,
            max_response_bytes=4,
        )
        sockets = FakeSocketFactory(
            [b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\nabcdef"]
        )

        response = PinnedHttpClient(socket_factory=sockets).fetch(context).response

        self.assertEqual(b"abcd", response.body)
        self.assertTrue(response.truncated)

    def test_reapproves_redirect_before_second_connection(self) -> None:
        resolver = StaticResolver(
            {
                "example.org": (PUBLIC_V4,),
                "redirect.example": (PUBLIC_V4_REDIRECT,),
            }
        )
        context = make_context("http://example.org/start", resolver)
        sockets = FakeSocketFactory(
            [
                b"HTTP/1.1 302 Found\r\nLocation: http://redirect.example/final\r\nContent-Length: 100\r\n\r\n",
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
            ]
        )

        result = PinnedHttpClient(socket_factory=sockets).fetch(context)

        self.assertEqual(1, result.redirect_count)
        self.assertEqual("redirect.example", result.target.hostname)
        self.assertEqual(
            [(PUBLIC_V4, 80), (PUBLIC_V4_REDIRECT, 80)],
            [call[0] for call in sockets.calls],
        )
        self.assertEqual(b"ok", result.response.body)

    def test_timeout_is_shared_across_redirect_chain(self) -> None:
        resolver = StaticResolver(
            {
                "example.org": (PUBLIC_V4,),
                "redirect.example": (PUBLIC_V4_REDIRECT,),
            }
        )
        context = make_context("http://example.org/start", resolver)
        sockets = FakeSocketFactory(
            [
                b"HTTP/1.1 302 Found\r\nLocation: http://redirect.example/final\r\nContent-Length: 0\r\n\r\n"
            ]
        )
        times = iter((0.0, 0.1, 0.2, 2.1))

        with self.assertRaisesRegex(HttpTransportError, "TimeoutError"):
            PinnedHttpClient(socket_factory=sockets, clock=lambda: next(times)).fetch(
                context
            )
        self.assertEqual(1, len(sockets.calls))

    def test_rejects_private_redirect_before_connecting_to_it(self) -> None:
        resolver = StaticResolver({"example.org": (PUBLIC_V4,)})
        context = make_context("http://example.org/start", resolver)
        sockets = FakeSocketFactory(
            [
                b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/private\r\nContent-Length: 0\r\n\r\n"
            ]
        )

        with self.assertRaises(ProbeValidationError):
            PinnedHttpClient(socket_factory=sockets).fetch(context)
        self.assertEqual(1, len(sockets.calls))

    def test_https_wraps_pinned_socket_with_approved_hostname_for_sni(self) -> None:
        class RecordingTlsClient(PinnedHttpClient):
            def __init__(self, **kwargs: object) -> None:
                super().__init__(**kwargs)  # type: ignore[arg-type]
                self.hostnames: list[str] = []

            def _wrap_tls(self, raw_socket: socket.socket, hostname: str) -> socket.socket:
                self.hostnames.append(hostname)
                return raw_socket

        resolver = StaticResolver({"example.org": (PUBLIC_V4,)})
        context = make_context("https://example.org/", resolver)
        sockets = FakeSocketFactory(
            [b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"]
        )
        client = RecordingTlsClient(socket_factory=sockets)

        client.fetch(context)

        self.assertEqual(["example.org"], client.hostnames)
        self.assertEqual((PUBLIC_V4, 443), sockets.calls[0][0])

    def test_rejects_insecure_custom_tls_context(self) -> None:
        insecure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        insecure.check_hostname = False
        insecure.verify_mode = ssl.CERT_NONE
        insecure.minimum_version = ssl.TLSVersion.TLSv1_2
        with self.assertRaisesRegex(ValueError, "certificate and hostname"):
            PinnedHttpClient(ssl_context=insecure)


if __name__ == "__main__":
    unittest.main()
