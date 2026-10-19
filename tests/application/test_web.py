from __future__ import annotations

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
    ApiPrincipal,
    SqliteAssessmentJobRepository,
    WebAsgiApp,
    apply_sqlite_migrations,
)


class DelegateApp:
    def __init__(self) -> None:
        self.paths: list[str] = []

    async def __call__(self, scope, receive, send) -> None:
        del receive
        self.paths.append(scope["path"])
        await send(
            {
                "type": "http.response.start",
                "status": 418,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"delegated"})


class CapturingAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record_web_ui_event(self, **event: Any) -> None:
        self.events.append(event)


class WebAsgiAppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.delegate = DelegateApp()
        self.audit = CapturingAudit()
        self.app = WebAsgiApp(self.delegate, audit_recorder=self.audit)

    @staticmethod
    def scope(path: str, *, method: str = "GET") -> dict[str, Any]:
        return {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "client": ("192.0.2.10", 1234),
            "state": {
                "archivability.request_id": "request-web-1",
                "archivability.principal": ApiPrincipal(
                    user_id="user-1",
                    session_id="session-1",
                ),
            },
        }

    async def request(
        self, scope: Mapping[str, Any]
    ) -> tuple[int, dict[str, str], bytes]:
        sent: list[Mapping[str, Any]] = []

        async def receive() -> Mapping[str, Any]:
            return {"type": "http.request", "body": b""}

        async def send(event: Mapping[str, Any]) -> None:
            sent.append(event)

        await self.app(scope, receive, send)
        self.assertEqual(2, len(sent))
        return (
            sent[0]["status"],
            {
                name.decode("ascii"): value.decode("ascii")
                for name, value in sent[0]["headers"]
            },
            sent[1]["body"],
        )

    async def test_human_pages_are_secure_and_audited(self) -> None:
        home = await self.request(self.scope("/"))
        report = await self.request(self.scope("/analyses/analysis-123"))
        methodology = await self.request(self.scope("/methodology"))
        catalog = await self.request(self.scope("/methodology/indicators"))
        indicator = await self.request(
            self.scope("/methodology/indicators/latency_breakdown")
        )

        self.assertEqual(200, home[0])
        self.assertIn(b"Arquivabilidade J", home[2])
        self.assertEqual(200, report[0])
        self.assertIn(b"cancel-button", report[2])
        self.assertEqual(200, methodology[0])
        self.assertIn(b"methodology-view", methodology[2])
        self.assertEqual(200, catalog[0])
        self.assertEqual(200, indicator[0])
        self.assertIn("default-src 'none'", home[1]["content-security-policy"])
        self.assertEqual("no-store", home[1]["cache-control"])
        self.assertEqual(
            ["home", "analysis", "methodology", "indicator_catalog", "indicator"],
            [item["route"] for item in self.audit.events],
        )
        self.assertEqual("user-1", self.audit.events[0]["audit"].user_id)

    async def test_invalid_indicator_page_is_delegated(self) -> None:
        response = await self.request(
            self.scope("/methodology/indicators/unsafe%2Fidentifier")
        )

        self.assertEqual(418, response[0])
        self.assertEqual(
            ["/methodology/indicators/unsafe%2Fidentifier"], self.delegate.paths
        )

    async def test_assets_support_head_and_api_paths_are_delegated(self) -> None:
        script = await self.request(self.scope("/assets/app.js", method="HEAD"))
        delegated = await self.request(self.scope("/v1/analyses"))

        self.assertEqual(200, script[0])
        self.assertEqual(b"", script[2])
        self.assertEqual("text/javascript; charset=utf-8", script[1]["content-type"])
        self.assertEqual(418, delegated[0])
        self.assertEqual(["/v1/analyses"], self.delegate.paths)

    async def test_unsafe_page_method_is_rejected_and_persistently_audited(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        apply_sqlite_migrations(connection)
        repository = SqliteAssessmentJobRepository(connection)
        self.app = WebAsgiApp(self.delegate, audit_recorder=repository)

        response = await self.request(self.scope("/", method="POST"))

        self.assertEqual(405, response[0])
        self.assertEqual("GET, HEAD", response[1]["allow"])
        event = connection.execute(
            """
            SELECT action, result, user_id, session_id, ip_address, extra_json
            FROM audit_events WHERE action = 'access.web_ui'
            """
        ).fetchone()
        self.assertEqual(
            ("access.web_ui", "failure", "user-1", "session-1", "192.0.2.10"),
            event[:5],
        )
        self.assertNotIn("analysis-123", event[5])


if __name__ == "__main__":
    unittest.main()
