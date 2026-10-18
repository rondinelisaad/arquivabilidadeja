from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message

from archivability.browser import (
    BrowserContractError,
    BrowserRenderRequest,
    LoopbackBrowserRenderer,
)

NOW = datetime(2026, 10, 19, 3, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.status = status
        self.body = body
        self.closed = False
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"

    def read(self, amount: int = -1) -> bytes:
        return self.body[:amount]

    def close(self) -> None:
        self.closed = True


def result_document() -> dict[str, object]:
    return {
        "analysis_id": "analysis-browser-client",
        "attempt_id": "attempt-browser-client",
        "engine_name": "chromium",
        "engine_version": "140.0.1",
        "started_at": NOW.isoformat(),
        "finished_at": (NOW + timedelta(seconds=1)).isoformat(),
        "isolation": {
            "policy_id": "browser-container-1",
            "sandbox_enabled": True,
            "running_as_root": False,
            "read_only_root_filesystem": True,
            "ephemeral_profile": True,
            "host_mounts_present": False,
            "secrets_available": False,
            "docker_socket_available": False,
        },
        "aggregate": {
            "raw_dom_hash": "a" * 64,
            "rendered_dom_hash": "b" * 64,
            "raw_url_set_hash": "c" * 64,
            "rendered_url_set_hash": "d" * 64,
            "raw_url_count": 1,
            "rendered_url_count": 2,
            "additional_url_count": 1,
            "request_count": 3,
            "internal_request_count": 2,
            "external_request_count": 1,
            "api_request_count": 1,
            "external_api_request_count": 1,
            "failed_request_count": 0,
            "blocked_request_count": 0,
            "transfer_bytes": 1024,
            "raw_dom_bytes": 256,
            "rendered_dom_bytes": 512,
            "console_event_count": 0,
            "elapsed_milliseconds": 1000,
            "termination_reason": "complete",
        },
    }


class LoopbackBrowserRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = BrowserRenderRequest(
            analysis_id="analysis-browser-client",
            attempt_id="attempt-browser-client",
            subject_uri="https://example.org/",
            requested_at=NOW,
        )

    def test_accepts_only_explicit_loopback_endpoint(self) -> None:
        for endpoint in (
            "https://127.0.0.1:9223/render",
            "http://localhost:9223/render",
            "http://127.0.0.1:9223/other",
            "http://example.org:9223/render",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(
                BrowserContractError
            ):
                LoopbackBrowserRenderer(endpoint)

    def test_sends_bounded_request_and_validates_response(self) -> None:
        response = FakeResponse(json.dumps(result_document()).encode())
        calls = []

        def transport(request, timeout):
            calls.append((request, timeout))
            return response

        result = LoopbackBrowserRenderer(transport=transport).render(self.request)

        self.assertEqual(self.request.analysis_id, result.analysis_id)
        self.assertEqual(32, calls[0][1])
        sent = json.loads(calls[0][0].data)
        self.assertEqual(self.request.budgets.to_dict(), sent["budgets"])
        self.assertTrue(response.closed)

    def test_rejects_oversized_or_ambiguous_response(self) -> None:
        oversized = FakeResponse(b"{" + b" " * (128 * 1024 + 1))
        with self.assertRaisesRegex(BrowserContractError, "byte limit"):
            LoopbackBrowserRenderer(transport=lambda request, timeout: oversized).render(
                self.request
            )

        duplicate = FakeResponse(b'{"analysis_id":"a","analysis_id":"b"}')
        with self.assertRaisesRegex(BrowserContractError, "duplicate"):
            LoopbackBrowserRenderer(transport=lambda request, timeout: duplicate).render(
                self.request
            )


if __name__ == "__main__":
    unittest.main()
