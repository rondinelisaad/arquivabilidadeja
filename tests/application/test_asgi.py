from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from archivability import (  # noqa: E402
    AnalysisAsgiApp,
    ApiPrincipal,
    ApiRequestContext,
    ApiResponse,
    SqliteAssessmentJobRepository,
    apply_sqlite_migrations,
)


class CapturingApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, ApiRequestContext]] = []

    def create_analysis(
        self, payload: Mapping[str, Any], *, context: ApiRequestContext
    ) -> ApiResponse:
        self.calls.append(("create", dict(payload), context))
        if context.principal is None:
            return self._error(context, 401, "AUTHENTICATION_REQUIRED")
        return ApiResponse(
            status_code=202,
            body={"analysis_id": "analysis-1", "status": "assessment_queued"},
            headers={"Cache-Control": "no-store"},
        )

    def get_analysis(
        self, analysis_id: str, *, context: ApiRequestContext
    ) -> ApiResponse:
        self.calls.append(("read", analysis_id, context))
        if context.principal is None:
            return self._error(context, 401, "AUTHENTICATION_REQUIRED")
        return ApiResponse(
            status_code=200,
            body={"analysis_id": analysis_id, "state": "running"},
            headers={"Cache-Control": "no-store"},
        )

    @staticmethod
    def _error(context: ApiRequestContext, status: int, code: str) -> ApiResponse:
        return ApiResponse(
            status_code=status,
            body={"error": {"code": code}, "request_id": context.request_id},
            headers={"Cache-Control": "no-store"},
        )


class CapturingAuditRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record_analysis_http_event(self, **event: Any) -> None:
        self.events.append(event)


class AnalysisAsgiAppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.api = CapturingApi()
        self.audit = CapturingAuditRecorder()
        self.app = AnalysisAsgiApp(
            api=self.api,  # type: ignore[arg-type]
            audit_recorder=self.audit,
            max_body_bytes=256,
        )
        self.principal = ApiPrincipal(user_id="user-1", session_id="session-1")

    @staticmethod
    def scope(
        *,
        method: str = "POST",
        path: str = "/v1/analyses",
        headers: list[tuple[bytes, bytes]] | None = None,
        state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.5"},
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": headers or [],
            "client": ("192.0.2.10", 12345),
            "server": ("api.example", 443),
            "state": dict(state or {}),
        }

    async def request(
        self,
        scope: Mapping[str, Any],
        events: list[Mapping[str, Any]] | None = None,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        incoming = list(events or [{"type": "http.request", "body": b""}])
        sent: list[Mapping[str, Any]] = []

        async def receive() -> Mapping[str, Any]:
            return incoming.pop(0)

        async def send(event: Mapping[str, Any]) -> None:
            sent.append(event)

        await self.app(scope, receive, send)
        self.assertEqual(2, len(sent))
        start, body = sent
        headers = {
            name.decode("ascii"): value.decode("ascii")
            for name, value in start["headers"]
        }
        return start["status"], headers, json.loads(body["body"])

    async def test_streamed_json_is_passed_with_trusted_context(self) -> None:
        state = {
            "archivability.principal": self.principal,
            "archivability.request_id": "request-trusted-1",
        }
        body = b'{"subject_uri":"https://example.org/"}'
        status, headers, response = await self.request(
            self.scope(
                headers=[
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
                state=state,
            ),
            [
                {"type": "http.request", "body": body[:10], "more_body": True},
                {"type": "http.request", "body": body[10:], "more_body": False},
            ],
        )

        self.assertEqual(202, status)
        self.assertEqual("analysis-1", response["analysis_id"])
        self.assertEqual("request-trusted-1", headers["x-request-id"])
        self.assertEqual("nosniff", headers["x-content-type-options"])
        operation, payload, context = self.api.calls[0]
        self.assertEqual("create", operation)
        self.assertEqual("https://example.org/", payload["subject_uri"])
        self.assertIs(self.principal, context.principal)
        self.assertEqual([], self.audit.events)

    async def test_client_identity_and_request_headers_are_never_trusted(self) -> None:
        status, headers, response = await self.request(
            self.scope(
                headers=[
                    (b"authorization", b"Bearer must-not-be-trusted"),
                    (b"x-user-id", b"attacker"),
                    (b"x-request-id", b"client-controlled"),
                ]
            )
        )

        self.assertEqual(401, status)
        self.assertEqual("AUTHENTICATION_REQUIRED", response["error"]["code"])
        self.assertEqual('Bearer realm="arquivabilidade-ja"', headers["www-authenticate"])
        self.assertNotEqual("client-controlled", headers["x-request-id"])
        self.assertIsNone(self.api.calls[0][2].principal)

    async def test_get_routes_analysis_identifier_without_reading_body(self) -> None:
        status, _, response = await self.request(
            self.scope(
                method="GET",
                path="/v1/analyses/analysis-123",
                state={
                    "archivability.principal": self.principal,
                    "archivability.request_id": "request-read-1",
                },
            ),
            [],
        )

        self.assertEqual(200, status)
        self.assertEqual("analysis-123", response["analysis_id"])
        self.assertEqual("analysis-123", self.api.calls[0][1])

    async def test_oversized_body_is_rejected_and_audited_without_content(self) -> None:
        secret = b"sensitive-body-value"
        status, _, response = await self.request(
            self.scope(
                headers=[(b"content-type", b"application/json")],
                state={"archivability.principal": self.principal},
            ),
            [{"type": "http.request", "body": secret * 20}],
        )

        self.assertEqual(413, status)
        self.assertEqual("PAYLOAD_TOO_LARGE", response["error"]["code"])
        self.assertEqual([], self.api.calls)
        serialized = json.dumps(self.audit.events, default=str)
        self.assertNotIn("sensitive-body-value", serialized)
        self.assertEqual("analyses_collection", self.audit.events[0]["route"])

    async def test_content_type_and_invalid_json_have_stable_errors(self) -> None:
        state = {"archivability.principal": self.principal}
        unsupported = await self.request(
            self.scope(headers=[(b"content-type", b"text/plain")], state=state),
            [{"type": "http.request", "body": b"{}"}],
        )
        invalid = await self.request(
            self.scope(headers=[(b"content-type", b"application/json")], state=state),
            [{"type": "http.request", "body": b"not-json"}],
        )

        self.assertEqual(415, unsupported[0])
        self.assertEqual("UNSUPPORTED_MEDIA_TYPE", unsupported[2]["error"]["code"])
        self.assertEqual(400, invalid[0])
        self.assertEqual("INVALID_JSON", invalid[2]["error"]["code"])
        self.assertEqual(2, len(self.audit.events))

    async def test_transport_rejection_is_persisted_as_sanitized_audit(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        apply_sqlite_migrations(connection)
        repository = SqliteAssessmentJobRepository(connection)
        self.app = AnalysisAsgiApp(
            api=self.api,  # type: ignore[arg-type]
            audit_recorder=repository,
            max_body_bytes=256,
        )
        status, _, _ = await self.request(
            self.scope(
                headers=[(b"content-type", b"application/json")],
                state={
                    "archivability.principal": self.principal,
                    "archivability.request_id": "request-audit-1",
                },
            ),
            [{"type": "http.request", "body": b"secret-not-json"}],
        )

        self.assertEqual(400, status)
        event = connection.execute(
            """
            SELECT action, resource, resource_id, result, user_id, ip_address,
                   session_id, extra_json
            FROM audit_events
            """
        ).fetchone()
        self.assertEqual(
            (
                "access.analysis_http_adapter",
                "analysis_http_adapter",
                "request-audit-1",
                "failure",
                "user-1",
                "192.0.2.10",
                "session-1",
            ),
            event[:7],
        )
        self.assertEqual("INVALID_JSON", json.loads(event[7])["error_code"])
        self.assertNotIn("secret-not-json", event[7])

    async def test_unmatched_route_and_method_are_bounded_and_audited(self) -> None:
        not_found = await self.request(
            self.scope(method="GET", path="/private/secret?token=value"), []
        )
        method = await self.request(self.scope(method="DELETE"), [])

        self.assertEqual(404, not_found[0])
        self.assertEqual(405, method[0])
        self.assertEqual("POST", method[1]["allow"])
        self.assertEqual("unmatched", self.audit.events[0]["route"])
        self.assertEqual("DELETE", self.audit.events[1]["method"])
        serialized = json.dumps(self.audit.events, default=str)
        self.assertNotIn("private", serialized)
        self.assertNotIn("token", serialized)

    async def test_non_http_scope_is_rejected(self) -> None:
        async def unused() -> Mapping[str, Any]:
            raise AssertionError("receive must not be called")

        with self.assertRaisesRegex(ValueError, "HTTP scopes"):
            await self.app({"type": "websocket"}, unused, unused)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
