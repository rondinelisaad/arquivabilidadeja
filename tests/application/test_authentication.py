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
    ApiPrincipal,
    BearerAuthenticationMiddleware,
    SqliteAssessmentJobRepository,
    apply_sqlite_migrations,
)


class CapturingApp:
    def __init__(self) -> None:
        self.scopes: list[Mapping[str, Any]] = []

    async def __call__(self, scope, receive, send) -> None:
        del receive
        self.scopes.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


class TokenVerifier:
    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.failure: Exception | None = None
        self.result: Any = ApiPrincipal(user_id="oidc-user-1", session_id="oidc-session-1")

    async def verify(self, token: str) -> ApiPrincipal:
        self.tokens.append(token)
        if self.failure is not None:
            raise self.failure
        return self.result


class AuditRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.failure: Exception | None = None

    def record_bearer_authentication_event(self, **event: Any) -> None:
        if self.failure is not None:
            raise self.failure
        self.events.append(event)


class BearerAuthenticationMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.downstream = CapturingApp()
        self.verifier = TokenVerifier()
        self.audit = AuditRecorder()
        self.middleware = BearerAuthenticationMiddleware(
            self.downstream,
            verifier=self.verifier,
            audit_recorder=self.audit,
            max_token_bytes=256,
        )

    @staticmethod
    def scope(
        headers: list[tuple[bytes, bytes]] | None = None,
        *,
        state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "http",
            "method": "GET",
            "path": "/v1/analyses/analysis-1",
            "headers": headers or [],
            "client": ("192.0.2.20", 54321),
            "state": dict(state or {}),
        }

    async def request(self, scope: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        sent: list[Mapping[str, Any]] = []

        async def receive() -> Mapping[str, Any]:
            return {"type": "http.request", "body": b""}

        async def send(event: Mapping[str, Any]) -> None:
            sent.append(event)

        await self.middleware(scope, receive, send)
        return sent

    async def test_verified_bearer_principal_is_added_to_copied_scope(self) -> None:
        token = "signed.payload.signature"
        original = self.scope(
            [(b"authorization", f"Bearer {token}".encode("ascii"))],
            state={"archivability.request_id": "request-auth-1"},
        )
        sent = await self.request(original)

        self.assertEqual(204, sent[0]["status"])
        self.assertEqual([token], self.verifier.tokens)
        self.assertEqual(1, len(self.downstream.scopes))
        downstream_state = self.downstream.scopes[0]["state"]
        self.assertIs(self.verifier.result, downstream_state["archivability.principal"])
        self.assertNotIn("archivability.principal", original["state"])
        event = self.audit.events[0]
        self.assertEqual("success", event["result"])
        self.assertEqual("oidc-user-1", event["audit"].user_id)
        self.assertNotIn(token, json.dumps(event, default=str))

    async def test_missing_header_clears_preexisting_principal_and_defers_401(self) -> None:
        untrusted = ApiPrincipal(user_id="injected", session_id="injected-session")
        sent = await self.request(
            self.scope(state={"archivability.principal": untrusted})
        )

        self.assertEqual(204, sent[0]["status"])
        state = self.downstream.scopes[0]["state"]
        self.assertNotIn("archivability.principal", state)
        self.assertTrue(state["archivability.request_id"])
        self.assertEqual([], self.verifier.tokens)
        self.assertEqual([], self.audit.events)

    async def test_malformed_or_duplicate_authorization_is_rejected(self) -> None:
        malformed = await self.request(
            self.scope([(b"authorization", b"Basic not-a-bearer-token")])
        )
        duplicate = await self.request(
            self.scope(
                [
                    (b"authorization", b"Bearer first.token.value"),
                    (b"authorization", b"Bearer second.token.value"),
                ]
            )
        )
        whitespace_smuggling = await self.request(
            self.scope([(b"authorization", b"Bearer\tsigned.payload.signature")])
        )

        for response in (malformed, duplicate, whitespace_smuggling):
            self.assertEqual(401, response[0]["status"])
            headers = dict(response[0]["headers"])
            self.assertIn(b"invalid_token", headers[b"www-authenticate"])
            body = json.loads(response[1]["body"])
            self.assertEqual("INVALID_TOKEN", body["error"]["code"])
        self.assertEqual([], self.downstream.scopes)
        self.assertEqual(3, len(self.audit.events))

    async def test_verifier_failure_is_closed_and_token_is_not_audited(self) -> None:
        token = "secret.payload.signature"
        self.verifier.failure = RuntimeError("provider details must not escape")
        sent = await self.request(
            self.scope([(b"authorization", f"Bearer {token}".encode("ascii"))])
        )

        self.assertEqual(401, sent[0]["status"])
        body = json.loads(sent[1]["body"])
        self.assertEqual("INVALID_TOKEN", body["error"]["code"])
        serialized = json.dumps(self.audit.events, default=str)
        self.assertNotIn(token, serialized)
        self.assertNotIn("provider details", serialized)

    async def test_wrong_verifier_return_type_is_rejected(self) -> None:
        self.verifier.result = {"sub": "unverified-claim"}
        sent = await self.request(
            self.scope([(b"authorization", b"Bearer signed.payload.signature")])
        )

        self.assertEqual(401, sent[0]["status"])
        self.assertEqual([], self.downstream.scopes)

    async def test_audit_failure_blocks_verified_authentication(self) -> None:
        self.audit.failure = RuntimeError("audit unavailable")
        sent = await self.request(
            self.scope([(b"authorization", b"Bearer signed.payload.signature")])
        )

        self.assertEqual(500, sent[0]["status"])
        self.assertEqual("INTERNAL_ERROR", json.loads(sent[1]["body"])["error"]["code"])
        self.assertEqual([], self.downstream.scopes)

    async def test_authentication_event_is_persisted_without_token(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        apply_sqlite_migrations(connection)
        repository = SqliteAssessmentJobRepository(connection)
        self.middleware = BearerAuthenticationMiddleware(
            self.downstream,
            verifier=self.verifier,
            audit_recorder=repository,
            max_token_bytes=256,
        )
        token = "signed.sensitive.signature"
        sent = await self.request(
            self.scope(
                [(b"authorization", f"Bearer {token}".encode("ascii"))],
                state={"archivability.request_id": "request-auth-audit-1"},
            )
        )

        self.assertEqual(204, sent[0]["status"])
        event = connection.execute(
            """
            SELECT action, result, user_id, ip_address, session_id, extra_json
            FROM audit_events
            """
        ).fetchone()
        self.assertEqual(
            (
                "auth.bearer_token",
                "success",
                "oidc-user-1",
                "192.0.2.20",
                "oidc-session-1",
            ),
            event[:5],
        )
        self.assertNotIn(token, event[5])


if __name__ == "__main__":
    unittest.main()
