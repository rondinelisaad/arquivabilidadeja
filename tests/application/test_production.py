from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import psycopg  # noqa: E402
from psycopg.pq import TransactionStatus  # noqa: E402

from archivability import (  # noqa: E402
    OidcVerifierConfig,
    PooledAsgiApplication,
    ProductionAsgiApplication,
    ProductionSettings,
    RuntimeConfigurationError,
    apply_postgresql_migrations,
    create_production_app,
    validate_runtime_database_role,
)


class _Cursor:
    def __init__(self, row: tuple[bool, ...]) -> None:
        self._row = row

    def fetchone(self) -> tuple[bool, ...]:
        return self._row


class _ConnectionInfo:
    transaction_status = TransactionStatus.IDLE


class _Connection:
    def __init__(self, row: tuple[bool, ...] = (False,) * 10) -> None:
        self.autocommit = True
        self.info = _ConnectionInfo()
        self.row = row
        self.rollbacks = 0

    def execute(self, query: str) -> _Cursor:
        self.query = query
        return _Cursor(self.row)

    def rollback(self) -> None:
        self.rollbacks += 1


class _Pool:
    def __init__(self) -> None:
        self.connection = _Connection()
        self.opened = False
        self.closed = False
        self.borrowed = 0
        self.returned = 0

    def open(self, *, wait: bool, timeout: float) -> None:
        self.opened = wait and timeout > 0

    def close(self, *, timeout: float) -> None:
        self.closed = timeout > 0

    def getconn(self, *, timeout: float | None = None) -> _Connection:
        if not self.opened or self.closed or not timeout:
            raise RuntimeError("pool is unavailable")
        self.borrowed += 1
        return self.connection

    def putconn(self, connection: _Connection) -> None:
        if connection is not self.connection:
            raise RuntimeError("unexpected connection")
        self.returned += 1


class ProductionSettingsTests(unittest.TestCase):
    def settings(self, database_dsn: str) -> ProductionSettings:
        return ProductionSettings(
            environment="test",
            database_dsn=database_dsn,
            methodology_path=(ROOT / "methodology" / "v0.1.0").resolve(),
            oidc=OidcVerifierConfig(
                issuer="https://identity.example/",
                audience="arquivabilidade-ja",
                jwks_uri="https://identity.example/.well-known/jwks.json",
            ),
        )

    def test_local_socket_dsn_is_allowed_and_redacted_from_repr(self) -> None:
        dsn = "host=/private/tmp dbname=archive user=archive_runtime"

        settings = self.settings(dsn)

        self.assertNotIn(dsn, repr(settings))
        self.assertNotIn("database_dsn", repr(settings))

    def test_remote_database_requires_verified_tls(self) -> None:
        insecure = "host=db.example dbname=archive user=archive_runtime"
        secure = f"{insecure} sslmode=verify-full"

        with self.assertRaisesRegex(RuntimeConfigurationError, "verify-full"):
            self.settings(insecure)
        self.assertEqual(secure, self.settings(secure).database_dsn)

    def test_admin_role_and_service_configuration_are_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeConfigurationError, "non-administrative"):
            self.settings("host=/tmp dbname=archive user=postgres")
        with self.assertRaisesRegex(RuntimeConfigurationError, "service"):
            self.settings(
                "host=/tmp dbname=archive user=archive_runtime service=external"
            )

    def test_environment_loader_is_strict_and_bounded(self) -> None:
        values = {
            "ARCHIVABILITY_ENVIRONMENT": "production",
            "ARCHIVABILITY_DATABASE_DSN": (
                "host=db.example dbname=archive user=archive_runtime "
                "sslmode=verify-full"
            ),
            "ARCHIVABILITY_METHODOLOGY_PATH": str(
                (ROOT / "methodology" / "v0.1.0").resolve()
            ),
            "ARCHIVABILITY_OIDC_ISSUER": "https://identity.example/",
            "ARCHIVABILITY_OIDC_AUDIENCE": "arquivabilidade-ja",
            "ARCHIVABILITY_OIDC_JWKS_URI": (
                "https://identity.example/.well-known/jwks.json"
            ),
            "ARCHIVABILITY_DB_POOL_MAX_SIZE": "12",
        }

        settings = ProductionSettings.from_environment(values)

        self.assertEqual(12, settings.pool_max_size)
        with self.assertRaisesRegex(RuntimeConfigurationError, "POOL_MAX"):
            ProductionSettings.from_environment(
                {**values, "ARCHIVABILITY_DB_POOL_MAX_SIZE": "unbounded"}
            )

    def test_database_role_validation_rejects_admin_or_ddl_privileges(self) -> None:
        safe = _Connection()
        validate_runtime_database_role(safe)  # type: ignore[arg-type]
        self.assertIn("rolsuper", safe.query)

        unsafe = _Connection(
            (False, False, False, False, False, False, False, True, False, False)
        )
        with self.assertRaisesRegex(RuntimeConfigurationError, "forbidden"):
            validate_runtime_database_role(unsafe)  # type: ignore[arg-type]


class PooledAsgiApplicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.pool = _Pool()
        self.lifecycle: list[str] = []

        async def request_app(scope, receive, send) -> None:
            del scope, receive
            await send(
                {"type": "http.response.start", "status": 204, "headers": []}
            )
            await send({"type": "http.response.body", "body": b""})

        self.app = PooledAsgiApplication(
            pool=self.pool,  # type: ignore[arg-type]
            request_app_factory=lambda connection: request_app,
            lifecycle_recorder=lambda connection, status: self.lifecycle.append(
                status
            ),
            pool_timeout_seconds=2,
            pool_close_timeout_seconds=2,
        )

    async def test_lifespan_opens_pool_and_binds_connection_per_request(self) -> None:
        events: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue()
        lifespan_sent: list[Mapping[str, Any]] = []

        async def receive_lifespan() -> Mapping[str, Any]:
            return await events.get()

        async def send_lifespan(event: Mapping[str, Any]) -> None:
            lifespan_sent.append(event)

        lifespan = asyncio.create_task(
            self.app({"type": "lifespan"}, receive_lifespan, send_lifespan)
        )
        await events.put({"type": "lifespan.startup"})
        while not lifespan_sent:
            await asyncio.sleep(0)
        self.assertEqual("lifespan.startup.complete", lifespan_sent[0]["type"])

        sent: list[Mapping[str, Any]] = []

        async def receive_http() -> Mapping[str, Any]:
            return {"type": "http.request", "body": b""}

        async def send_http(event: Mapping[str, Any]) -> None:
            sent.append(event)

        await self.app({"type": "http"}, receive_http, send_http)
        self.assertEqual(204, sent[0]["status"])

        await events.put({"type": "lifespan.shutdown"})
        await lifespan
        self.assertEqual("lifespan.shutdown.complete", lifespan_sent[-1]["type"])
        self.assertEqual(["started", "stopped"], self.lifecycle)
        self.assertEqual(self.pool.borrowed, self.pool.returned)
        self.assertTrue(self.pool.closed)

    async def test_request_before_startup_fails_closed_without_borrowing(self) -> None:
        sent: list[Mapping[str, Any]] = []

        async def receive() -> Mapping[str, Any]:
            return {"type": "http.request", "body": b""}

        async def send(event: Mapping[str, Any]) -> None:
            sent.append(event)

        await self.app({"type": "http"}, receive, send)

        self.assertEqual(500, sent[0]["status"])
        self.assertEqual(0, self.pool.borrowed)

    async def test_liveness_is_independent_from_database_readiness(self) -> None:
        application = ProductionAsgiApplication(
            self.app,
            verifier=object(),
            audit_recorder=object(),  # type: ignore[arg-type]
            max_token_bytes=1024,
        )

        async def receive() -> Mapping[str, Any]:
            return {"type": "http.request", "body": b""}

        async def send_live(event: Mapping[str, Any]) -> None:
            live.append(event)

        live: list[Mapping[str, Any]] = []
        await application(
            {"type": "http", "method": "GET", "path": "/health/live"},
            receive,
            send_live,
        )

        async def send_ready(event: Mapping[str, Any]) -> None:
            ready.append(event)

        ready: list[Mapping[str, Any]] = []
        await application(
            {"type": "http", "method": "GET", "path": "/health/ready"},
            receive,
            send_ready,
        )

        self.assertEqual(200, live[0]["status"])
        self.assertEqual(503, ready[0]["status"])
        self.assertEqual(0, self.pool.borrowed)


POSTGRES_DSN = os.environ.get("ARCHIVABILITY_TEST_POSTGRES_DSN")
POSTGRES_RUNTIME_DSN = os.environ.get("ARCHIVABILITY_TEST_POSTGRES_RUNTIME_DSN")


@unittest.skipUnless(
    POSTGRES_DSN and POSTGRES_RUNTIME_DSN,
    "PostgreSQL migration and restricted runtime DSNs are not configured",
)
class ProductionPostgreSqlIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert POSTGRES_DSN is not None
        with psycopg.connect(POSTGRES_DSN) as connection:
            apply_postgresql_migrations(connection)

    async def test_real_pool_lifespan_and_unauthenticated_request(self) -> None:
        assert POSTGRES_RUNTIME_DSN is not None
        settings = ProductionSettings(
            environment="integration",
            database_dsn=POSTGRES_RUNTIME_DSN,
            methodology_path=(ROOT / "methodology" / "v0.1.0").resolve(),
            oidc=OidcVerifierConfig(
                issuer="https://identity.example/",
                audience="arquivabilidade-ja",
                jwks_uri="https://identity.example/.well-known/jwks.json",
            ),
            pool_min_size=1,
            pool_max_size=2,
        )
        app = create_production_app(settings, verifier=object())
        queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue()
        lifespan_events: list[Mapping[str, Any]] = []

        async def lifespan_receive() -> Mapping[str, Any]:
            return await queue.get()

        async def lifespan_send(event: Mapping[str, Any]) -> None:
            lifespan_events.append(event)

        lifespan = asyncio.create_task(
            app({"type": "lifespan"}, lifespan_receive, lifespan_send)
        )
        await queue.put({"type": "lifespan.startup"})
        while not lifespan_events:
            await asyncio.sleep(0)
        self.assertEqual(
            "lifespan.startup.complete", lifespan_events[0]["type"]
        )

        async def receive() -> Mapping[str, Any]:
            return {"type": "http.request", "body": b""}

        for path, content in (
            ("/health/live", b'"status":"ok"'),
            ("/health/ready", b'"status":"ok"'),
            ("/internal/metrics", b"archivability_queue_jobs"),
        ):
            operational_events: list[Mapping[str, Any]] = []

            async def operational_send(event: Mapping[str, Any]) -> None:
                operational_events.append(event)

            await app(
                {
                    "type": "http",
                    "method": "GET",
                    "path": path,
                    "headers": [],
                    "client": ("192.0.2.41", 12345),
                },
                receive,
                operational_send,
            )
            self.assertEqual(200, operational_events[0]["status"])
            self.assertIn(content, operational_events[1]["body"])

        response_events: list[Mapping[str, Any]] = []

        async def send(event: Mapping[str, Any]) -> None:
            response_events.append(event)

        await app(
            {
                "type": "http",
                "method": "GET",
                "path": "/v1/analyses/missing-analysis",
                "headers": [],
                "client": ("192.0.2.40", 12345),
                "state": {"archivability.request_id": f"request-{id(self)}"},
            },
            receive,
            send,
        )
        self.assertEqual(401, response_events[0]["status"])
        body = json.loads(response_events[1]["body"])
        self.assertEqual("AUTHENTICATION_REQUIRED", body["error"]["code"])

        response_events.clear()
        await app(
            {
                "type": "http",
                "method": "GET",
                "path": "/v1/analyses/missing-analysis",
                "headers": [
                    (b"authorization", b"Bearer signed.payload.signature")
                ],
                "client": ("192.0.2.40", 12345),
                "state": {
                    "archivability.request_id": f"request-invalid-{id(self)}"
                },
            },
            receive,
            send,
        )
        self.assertEqual(401, response_events[0]["status"])
        body = json.loads(response_events[1]["body"])
        self.assertEqual("INVALID_TOKEN", body["error"]["code"])

        await queue.put({"type": "lifespan.shutdown"})
        await lifespan
        self.assertEqual(
            "lifespan.shutdown.complete", lifespan_events[-1]["type"]
        )
        assert POSTGRES_DSN is not None
        with psycopg.connect(POSTGRES_DSN) as connection:
            lifecycle = connection.execute(
                """
                SELECT extra_json->>'status'
                FROM archivability.audit_events
                WHERE action = 'system.application_lifecycle'
                ORDER BY timestamp DESC,
                         event_id DESC
                LIMIT 2
                """
            ).fetchall()
            authentication = connection.execute(
                """
                SELECT result, extra_json->>'error_code'
                FROM archivability.audit_events
                WHERE action = 'auth.bearer_token'
                  AND resource_id = %s
                """,
                (f"request-invalid-{id(self)}",),
            ).fetchone()
            metrics_access = connection.execute(
                """
                SELECT result, ip_address::text
                FROM archivability.audit_events
                WHERE action = 'access.assessment_queue_metrics'
                  AND ip_address = %s::inet
                ORDER BY timestamp DESC, event_id DESC
                LIMIT 1
                """,
                ("192.0.2.41",),
            ).fetchone()
        self.assertEqual({"started", "stopped"}, {row[0] for row in lifecycle})
        self.assertEqual(("failure", "INVALID_TOKEN"), authentication)
        self.assertEqual(("success", "192.0.2.41/32"), metrics_access)


if __name__ == "__main__":
    unittest.main()
