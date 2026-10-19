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

    def create_batch(
        self, payload: Mapping[str, Any], *, context: ApiRequestContext
    ) -> ApiResponse:
        self.calls.append(("batch_create", dict(payload), context))
        if context.principal is None:
            return self._error(context, 401, "AUTHENTICATION_REQUIRED")
        return ApiResponse(
            status_code=202,
            body={
                "batch_schema_version": "1.0",
                "batch_id": "batch-1",
                "accepted_count": len(payload.get("subjects", [])),
                "failed_count": 0,
                "items": [],
            },
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

    def export_analysis(
        self, analysis_id: str, *, context: ApiRequestContext
    ) -> ApiResponse:
        self.calls.append(("export", analysis_id, context))
        if context.principal is None:
            return self._error(context, 401, "AUTHENTICATION_REQUIRED")
        return ApiResponse(
            status_code=200,
            body={"analysis_id": analysis_id, "report_schema_version": "1.0"},
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": (
                    f'attachment; filename="analysis-{analysis_id}.json"'
                ),
            },
        )

    def cancel_analysis(
        self, analysis_id: str, *, context: ApiRequestContext
    ) -> ApiResponse:
        self.calls.append(("cancel", analysis_id, context))
        return ApiResponse(
            status_code=200,
            body={"analysis_id": analysis_id, "state": "cancelled"},
            headers={"Cache-Control": "no-store"},
        )

    def list_analyses(self, *, context: ApiRequestContext) -> ApiResponse:
        self.calls.append(("history", None, context))
        return ApiResponse(
            status_code=200,
            body={"history_schema_version": "1.0", "items": []},
            headers={"Cache-Control": "no-store"},
        )

    def list_site_history(
        self, domain: str, *, context: ApiRequestContext
    ) -> ApiResponse:
        self.calls.append(("site_history", domain, context))
        return ApiResponse(
            status_code=200,
            body={"site_history_schema_version": "1.0", "strata": [], "items": []},
            headers={"Cache-Control": "no-store"},
        )

    def compare_analysis(
        self, analysis_id: str, *, context: ApiRequestContext
    ) -> ApiResponse:
        self.calls.append(("compare", analysis_id, context))
        return ApiResponse(
            status_code=200,
            body={
                "comparison_schema_version": "1.0",
                "classification": "no_observed_change",
            },
            headers={"Cache-Control": "no-store"},
        )

    def get_methodology(self, *, context: ApiRequestContext) -> ApiResponse:
        self.calls.append(("methodology_read", None, context))
        return ApiResponse(
            status_code=200,
            body={"methodology_schema_version": "1.0", "version": "0.1.0"},
            headers={"Cache-Control": "no-store"},
        )

    def get_openapi(self, *, context: ApiRequestContext) -> ApiResponse:
        self.calls.append(("openapi_read", None, context))
        return ApiResponse(
            status_code=200,
            body={"openapi": "3.1.0", "paths": {}},
            headers={"Cache-Control": "no-store"},
        )

    def list_indicators(
        self, filters: Mapping[str, Any], *, context: ApiRequestContext
    ) -> ApiResponse:
        self.calls.append(("indicator_list", dict(filters), context))
        return ApiResponse(
            status_code=200,
            body={"indicator_catalog_schema_version": "1.0", "items": []},
            headers={"Cache-Control": "no-store"},
        )

    def get_indicator(
        self, identifier: str, *, context: ApiRequestContext
    ) -> ApiResponse:
        self.calls.append(("indicator_read", identifier, context))
        return ApiResponse(
            status_code=200,
            body={"indicator_schema_version": "1.0", "indicator": {"id": identifier}},
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
        query_string: bytes = b"",
    ) -> dict[str, Any]:
        return {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.5"},
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query_string,
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

    async def test_batch_route_accepts_bounded_json(self) -> None:
        state = {
            "archivability.principal": self.principal,
            "archivability.request_id": "request-batch-1",
        }
        body = b'{"subjects":[{"subject_uri":"https://example.org/"}]}'
        status, _, response = await self.request(
            self.scope(
                path="/v1/batches",
                headers=[
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
                state=state,
            ),
            [{"type": "http.request", "body": body}],
        )

        self.assertEqual(202, status)
        self.assertEqual("batch-1", response["batch_id"])
        self.assertEqual("batch_create", self.api.calls[0][0])

    async def test_methodology_routes_and_indicator_filters_are_explicit(self) -> None:
        state = {
            "archivability.principal": self.principal,
            "archivability.request_id": "request-catalog-1",
        }
        methodology = await self.request(
            self.scope(method="GET", path="/v1/methodology", state=state), []
        )
        openapi = await self.request(
            self.scope(method="GET", path="/openapi.json", state=state), []
        )
        indicators = await self.request(
            self.scope(
                method="GET",
                path="/v1/indicators",
                query_string=b"dimension=discoverability&version=0.1.0",
                state=state,
            ),
            [],
        )
        indicator = await self.request(
            self.scope(
                method="GET",
                path="/v1/indicators/latency_breakdown",
                state=state,
            ),
            [],
        )

        self.assertEqual(200, methodology[0])
        self.assertEqual("3.1.0", openapi[2]["openapi"])
        self.assertEqual(200, indicators[0])
        self.assertEqual(200, indicator[0])
        self.assertEqual(
            {"dimension": "discoverability", "version": "0.1.0"},
            self.api.calls[2][1],
        )
        self.assertEqual("latency_breakdown", self.api.calls[3][1])

    async def test_duplicate_or_unknown_indicator_filters_are_rejected(self) -> None:
        state = {"archivability.principal": self.principal}
        duplicate = await self.request(
            self.scope(
                method="GET",
                path="/v1/indicators",
                query_string=b"dimension=a&dimension=b",
                state=state,
            ),
            [],
        )
        unknown = await self.request(
            self.scope(
                method="GET",
                path="/v1/indicators",
                query_string=b"target=https%3A%2F%2Fexample.org",
                state=state,
            ),
            [],
        )

        self.assertEqual(400, duplicate[0])
        self.assertEqual(400, unknown[0])
        self.assertEqual([], self.api.calls)

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

    async def test_export_routes_canonical_json_as_attachment(self) -> None:
        status, headers, response = await self.request(
            self.scope(
                method="GET",
                path="/v1/analyses/analysis-123/export.json",
                state={
                    "archivability.principal": self.principal,
                    "archivability.request_id": "request-export-1",
                },
            ),
            [],
        )

        self.assertEqual(200, status)
        self.assertEqual("1.0", response["report_schema_version"])
        self.assertEqual(
            'attachment; filename="analysis-analysis-123.json"',
            headers["content-disposition"],
        )
        self.assertEqual("export", self.api.calls[0][0])

    async def test_history_and_comparison_routes_do_not_read_bodies(self) -> None:
        state = {
            "archivability.principal": self.principal,
            "archivability.request_id": "request-history-1",
        }
        history = await self.request(
            self.scope(method="GET", path="/v1/analyses", state=state), []
        )
        comparison = await self.request(
            self.scope(
                method="GET",
                path="/v1/analyses/analysis-123/comparison",
                state=state,
            ),
            [],
        )
        site_history = await self.request(
            self.scope(
                method="GET",
                path="/v1/sites/example.org/history",
                state=state,
            ),
            [],
        )

        self.assertEqual(200, history[0])
        self.assertEqual("1.0", history[2]["history_schema_version"])
        self.assertEqual(200, comparison[0])
        self.assertEqual(200, site_history[0])
        self.assertEqual("1.0", site_history[2]["site_history_schema_version"])
        self.assertEqual("no_observed_change", comparison[2]["classification"])
        self.assertEqual(
            ["history", "compare", "site_history"],
            [item[0] for item in self.api.calls],
        )
        self.assertEqual("example.org", self.api.calls[2][1])

    async def test_cancel_route_is_post_only_and_does_not_require_a_body(self) -> None:
        state = {
            "archivability.principal": self.principal,
            "archivability.request_id": "request-cancel-1",
        }
        cancelled = await self.request(
            self.scope(
                method="POST",
                path="/v1/analyses/analysis-123/cancel",
                state=state,
            ),
            [],
        )

        self.assertEqual(200, cancelled[0])
        self.assertEqual("cancelled", cancelled[2]["state"])
        self.assertEqual("cancel", self.api.calls[0][0])

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
        self.assertEqual("GET, POST", method[1]["allow"])
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
