from __future__ import annotations

import asyncio
import importlib
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.pq import TransactionStatus

from archivability.application.api import AnalysisApi, ApiRateLimiter, ApiResponse
from archivability.application.asgi import AnalysisAsgiApp, AsgiSyncRunner
from archivability.application.authentication import (
    AuthenticationAuditRecorder,
    BearerAuthenticationMiddleware,
)
from archivability.application.http_workflow import HttpAssessmentWorkflow
from archivability.application.oidc import OidcJwtVerifier, OidcVerifierConfig
from archivability.application.policies import (
    InMemoryTokenBucketRateLimiter,
    OwnershipAuthorizationPolicy,
    RateLimitRule,
)
from archivability.application.read_model import AnalysisReportService
from archivability.jobs.service import HttpAssessmentQueueService
from archivability.lifecycle.service import AnalysisOrchestrator
from archivability.methodology.loader import load_methodology
from archivability.probes.execution import ProbeExecutionService
from archivability.probes.http_metadata import HttpMetadataProbe
from archivability.probes.ports import AddressResolver, Probe
from archivability.probes.resolver import SystemAddressResolver
from archivability.probes.runner import ProbeRunner
from archivability.probes.security import SsrfPolicy
from archivability.storage.audit import AuditContext
from archivability.storage.postgresql_jobs import PostgreSqlAssessmentJobRepository


_ENVIRONMENT_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_LOCAL_DATABASE_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1"})


class RuntimeConfigurationError(ValueError):
    """Raised without configuration values when runtime settings are unsafe."""


class RuntimePool(Protocol):
    def open(self, *, wait: bool, timeout: float) -> None: ...

    def close(self, *, timeout: float) -> None: ...

    def getconn(self, *, timeout: float | None = None) -> psycopg.Connection[Any]: ...

    def putconn(self, connection: psycopg.Connection[Any]) -> None: ...


class AsgiApplication(Protocol):
    async def __call__(
        self,
        scope: Mapping[str, Any],
        receive: Callable[[], Awaitable[Mapping[str, Any]]],
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ProductionSettings:
    environment: str
    database_dsn: str = field(repr=False)
    methodology_path: Path
    oidc: OidcVerifierConfig
    pool_min_size: int = 2
    pool_max_size: int = 10
    pool_timeout_seconds: int = 5
    pool_close_timeout_seconds: int = 5
    max_request_body_bytes: int = 4096
    max_bearer_token_bytes: int = 8192

    def __post_init__(self) -> None:
        if not isinstance(self.environment, str) or not _ENVIRONMENT_NAME.fullmatch(
            self.environment
        ):
            raise RuntimeConfigurationError("runtime environment name is invalid")
        self._validate_database_dsn(self.database_dsn)
        path = self.methodology_path
        if not isinstance(path, Path) or not path.is_absolute() or not path.is_dir():
            raise RuntimeConfigurationError(
                "methodology path must be an existing absolute directory"
            )
        if not isinstance(self.oidc, OidcVerifierConfig):
            raise RuntimeConfigurationError("OIDC configuration is invalid")
        self._bounded_int("pool_min_size", self.pool_min_size, 1, 32)
        self._bounded_int("pool_max_size", self.pool_max_size, 1, 128)
        if self.pool_min_size > self.pool_max_size:
            raise RuntimeConfigurationError(
                "pool_min_size cannot exceed pool_max_size"
            )
        self._bounded_int("pool_timeout_seconds", self.pool_timeout_seconds, 1, 30)
        self._bounded_int(
            "pool_close_timeout_seconds",
            self.pool_close_timeout_seconds,
            1,
            30,
        )
        self._bounded_int(
            "max_request_body_bytes", self.max_request_body_bytes, 256, 65_536
        )
        self._bounded_int(
            "max_bearer_token_bytes", self.max_bearer_token_bytes, 256, 16_384
        )

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> ProductionSettings:
        values = os.environ if environment is None else environment
        database_dsn = cls._required(values, "ARCHIVABILITY_DATABASE_DSN")
        methodology = Path(
            cls._required(values, "ARCHIVABILITY_METHODOLOGY_PATH")
        )
        oidc = OidcVerifierConfig(
            issuer=cls._required(values, "ARCHIVABILITY_OIDC_ISSUER"),
            audience=cls._required(values, "ARCHIVABILITY_OIDC_AUDIENCE"),
            jwks_uri=cls._required(values, "ARCHIVABILITY_OIDC_JWKS_URI"),
        )
        return cls(
            environment=cls._required(values, "ARCHIVABILITY_ENVIRONMENT"),
            database_dsn=database_dsn,
            methodology_path=methodology,
            oidc=oidc,
            pool_min_size=cls._optional_int(
                values, "ARCHIVABILITY_DB_POOL_MIN_SIZE", 2
            ),
            pool_max_size=cls._optional_int(
                values, "ARCHIVABILITY_DB_POOL_MAX_SIZE", 10
            ),
            pool_timeout_seconds=cls._optional_int(
                values, "ARCHIVABILITY_DB_POOL_TIMEOUT_SECONDS", 5
            ),
            pool_close_timeout_seconds=cls._optional_int(
                values, "ARCHIVABILITY_DB_POOL_CLOSE_TIMEOUT_SECONDS", 5
            ),
            max_request_body_bytes=cls._optional_int(
                values, "ARCHIVABILITY_MAX_REQUEST_BODY_BYTES", 4096
            ),
            max_bearer_token_bytes=cls._optional_int(
                values, "ARCHIVABILITY_MAX_BEARER_TOKEN_BYTES", 8192
            ),
        )

    @staticmethod
    def _required(values: Mapping[str, str], name: str) -> str:
        value = values.get(name)
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise RuntimeConfigurationError(f"{name} is required or invalid")
        return value

    @classmethod
    def _optional_int(
        cls, values: Mapping[str, str], name: str, default: int
    ) -> int:
        raw = values.get(name)
        if raw is None:
            return default
        if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
            raise RuntimeConfigurationError(f"{name} must be an integer")
        return int(raw)

    @staticmethod
    def _bounded_int(name: str, value: int, minimum: int, maximum: int) -> None:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not minimum <= value <= maximum
        ):
            raise RuntimeConfigurationError(
                f"{name} must be between {minimum} and {maximum}"
            )

    @staticmethod
    def _validate_database_dsn(value: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise RuntimeConfigurationError("database DSN is invalid")
        try:
            parameters = conninfo_to_dict(value)
        except psycopg.Error:
            raise RuntimeConfigurationError("database DSN is invalid") from None
        if parameters.get("service"):
            raise RuntimeConfigurationError(
                "database DSN cannot delegate to service configuration"
            )
        user = parameters.get("user", "").casefold()
        if not user or user in {"postgres", "root", "admin", "administrator"}:
            raise RuntimeConfigurationError(
                "database DSN must name a non-administrative runtime user"
            )
        if not parameters.get("dbname"):
            raise RuntimeConfigurationError("database DSN must name a database")
        hosts = parameters.get("host", "").split(",")
        host_addresses = parameters.get("hostaddr", "").split(",")
        remote = any(
            host not in _LOCAL_DATABASE_HOSTS and not host.startswith("/")
            for host in (*hosts, *host_addresses)
        )
        if remote and parameters.get("sslmode") != "verify-full":
            raise RuntimeConfigurationError(
                "remote database connections require sslmode=verify-full"
            )


class ThreadedAsgiSyncRunner(AsgiSyncRunner):
    """Move the synchronous domain and PostgreSQL work off the event loop."""

    async def run(self, operation: Callable[[], ApiResponse]) -> ApiResponse:
        return await asyncio.to_thread(operation)


class PooledAsgiApplication:
    """Bind one validated PostgreSQL connection to each HTTP request."""

    def __init__(
        self,
        *,
        pool: RuntimePool,
        request_app_factory: Callable[[psycopg.Connection[Any]], AsgiApplication],
        lifecycle_recorder: Callable[[psycopg.Connection[Any], str], None],
        pool_timeout_seconds: int,
        pool_close_timeout_seconds: int,
    ) -> None:
        self._pool = pool
        self._request_app_factory = request_app_factory
        self._lifecycle_recorder = lifecycle_recorder
        self._pool_timeout_seconds = pool_timeout_seconds
        self._pool_close_timeout_seconds = pool_close_timeout_seconds
        self._started = False
        self._lifecycle_lock = asyncio.Lock()

    async def __call__(
        self,
        scope: Mapping[str, Any],
        receive: Callable[[], Awaitable[Mapping[str, Any]]],
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope.get("type") != "http":
            raise ValueError("production application accepts HTTP and lifespan scopes")
        if not self._started:
            await self._send_unavailable(send)
            return
        connection: psycopg.Connection[Any] | None = None
        response_started = False

        async def tracked_send(event: Mapping[str, Any]) -> None:
            nonlocal response_started
            if event.get("type") == "http.response.start":
                response_started = True
            await send(event)

        try:
            connection = await asyncio.to_thread(
                self._pool.getconn, timeout=float(self._pool_timeout_seconds)
            )
            app = self._request_app_factory(connection)
            await app(scope, receive, tracked_send)
        except Exception:
            if not response_started:
                await self._send_unavailable(send)
        finally:
            if connection is not None:
                await asyncio.to_thread(self._return_connection, connection)

    async def _lifespan(
        self,
        receive: Callable[[], Awaitable[Mapping[str, Any]]],
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> None:
        while True:
            event = await receive()
            event_type = event.get("type") if isinstance(event, Mapping) else None
            if event_type == "lifespan.startup":
                if not await self._startup(send):
                    return
            elif event_type == "lifespan.shutdown":
                await self._shutdown(send)
                return
            else:
                await send(
                    {
                        "type": "lifespan.startup.failed",
                        "message": "Invalid lifespan event.",
                    }
                )
                return

    async def _startup(
        self, send: Callable[[Mapping[str, Any]], Awaitable[None]]
    ) -> bool:
        async with self._lifecycle_lock:
            if self._started:
                await send({"type": "lifespan.startup.complete"})
                return True
            try:
                await asyncio.to_thread(
                    self._pool.open,
                    wait=True,
                    timeout=float(self._pool_timeout_seconds),
                )
                await self._record_lifecycle("started")
            except Exception:
                try:
                    await asyncio.to_thread(
                        self._pool.close,
                        timeout=float(self._pool_close_timeout_seconds),
                    )
                except Exception:
                    pass
                await send(
                    {
                        "type": "lifespan.startup.failed",
                        "message": "Application startup failed.",
                    }
                )
                return False
            self._started = True
            await send({"type": "lifespan.startup.complete"})
            return True

    async def _shutdown(
        self, send: Callable[[Mapping[str, Any]], Awaitable[None]]
    ) -> None:
        async with self._lifecycle_lock:
            failed = False
            if self._started:
                try:
                    await self._record_lifecycle("stopped")
                except Exception:
                    failed = True
            self._started = False
            try:
                await asyncio.to_thread(
                    self._pool.close,
                    timeout=float(self._pool_close_timeout_seconds),
                )
            except Exception:
                failed = True
            await send(
                {
                    "type": (
                        "lifespan.shutdown.failed"
                        if failed
                        else "lifespan.shutdown.complete"
                    ),
                    **({"message": "Application shutdown failed."} if failed else {}),
                }
            )

    async def _record_lifecycle(self, status: str) -> None:
        connection = await asyncio.to_thread(
            self._pool.getconn, timeout=float(self._pool_timeout_seconds)
        )
        try:
            await asyncio.to_thread(self._lifecycle_recorder, connection, status)
        finally:
            await asyncio.to_thread(self._return_connection, connection)

    def _return_connection(self, connection: psycopg.Connection[Any]) -> None:
        _return_pool_connection(self._pool, connection)

    @staticmethod
    async def _send_unavailable(
        send: Callable[[Mapping[str, Any]], Awaitable[None]]
    ) -> None:
        body = (
            b'{"error":{"code":"INTERNAL_ERROR",'
            b'"message":"Unable to process the request."}}'
        )
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [
                    (b"cache-control", b"no-store"),
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-security-policy", b"default-src 'none'"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-content-type-options", b"nosniff"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _PooledAuthenticationAuditRecorder:
    def __init__(self, pool: RuntimePool, *, timeout_seconds: int) -> None:
        self._pool = pool
        self._timeout_seconds = timeout_seconds

    async def record_bearer_authentication_event(
        self,
        *,
        request_id: str,
        result: str,
        error_code: str | None,
        audit: AuditContext,
    ) -> None:
        connection = await asyncio.to_thread(
            self._pool.getconn, timeout=float(self._timeout_seconds)
        )
        try:
            await asyncio.to_thread(
                PostgreSqlAssessmentJobRepository(
                    connection
                ).record_bearer_authentication_event,
                request_id=request_id,
                result=result,
                error_code=error_code,
                audit=audit,
            )
        finally:
            await asyncio.to_thread(
                _return_pool_connection, self._pool, connection
            )


class ProductionAsgiApplication:
    """Authenticate before dispatching the request to the PostgreSQL scope."""

    def __init__(
        self,
        core: PooledAsgiApplication,
        *,
        verifier: Any,
        audit_recorder: AuthenticationAuditRecorder,
        max_token_bytes: int,
    ) -> None:
        self._application = BearerAuthenticationMiddleware(
            core,
            verifier=verifier,
            audit_recorder=audit_recorder,
            max_token_bytes=max_token_bytes,
        )

    async def __call__(
        self,
        scope: Mapping[str, Any],
        receive: Callable[[], Awaitable[Mapping[str, Any]]],
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> None:
        await self._application(scope, receive, send)


def _return_pool_connection(
    pool: RuntimePool, connection: psycopg.Connection[Any]
) -> None:
    try:
        if connection.info.transaction_status is not TransactionStatus.IDLE:
            connection.rollback()
    finally:
        pool.putconn(connection)


def validate_runtime_database_role(connection: psycopg.Connection[Any]) -> None:
    """Reject administrative or DDL-capable runtime database roles."""
    if not connection.autocommit:
        raise RuntimeConfigurationError("runtime database connection needs autocommit")
    row = connection.execute(
        """
        SELECT role.rolsuper, role.rolcreatedb, role.rolcreaterole,
               role.rolreplication, role.rolbypassrls,
               has_database_privilege(current_user, current_database(), 'CREATE'),
               has_database_privilege(current_user, current_database(), 'TEMP'),
               has_schema_privilege(current_user, 'archivability', 'CREATE'),
               EXISTS (
                   SELECT 1
                   FROM pg_catalog.pg_class AS relation
                   JOIN pg_catalog.pg_namespace AS namespace
                     ON namespace.oid = relation.relnamespace
                   WHERE namespace.nspname = 'archivability'
                     AND relation.relkind IN ('r', 'p')
                     AND (
                         has_table_privilege(current_user, relation.oid, 'DELETE')
                         OR has_table_privilege(current_user, relation.oid, 'TRUNCATE')
                         OR has_table_privilege(current_user, relation.oid, 'TRIGGER')
                     )
               ),
               NOT (
                   SELECT pg_catalog.bool_and(
                       has_table_privilege(
                           current_user, required.table_name, required.privilege
                       )
                   )
                   FROM (
                       VALUES
                           ('archivability.analyses', 'SELECT'),
                           ('archivability.analyses', 'INSERT'),
                           ('archivability.analyses', 'UPDATE'),
                           ('archivability.attempts', 'SELECT'),
                           ('archivability.attempts', 'INSERT'),
                           ('archivability.attempts', 'UPDATE'),
                           ('archivability.assessment_jobs', 'SELECT'),
                           ('archivability.assessment_jobs', 'INSERT'),
                           ('archivability.assessment_jobs', 'UPDATE'),
                           ('archivability.observations', 'SELECT'),
                           ('archivability.observations', 'INSERT'),
                           ('archivability.evidence', 'SELECT'),
                           ('archivability.evidence', 'INSERT'),
                           ('archivability.evidence_sources', 'SELECT'),
                           ('archivability.evidence_sources', 'INSERT'),
                           ('archivability.indicator_results', 'SELECT'),
                           ('archivability.indicator_results', 'INSERT'),
                           ('archivability.indicator_result_evidence', 'SELECT'),
                           ('archivability.indicator_result_evidence', 'INSERT'),
                           ('archivability.analysis_ownership', 'SELECT'),
                           ('archivability.analysis_ownership', 'INSERT'),
                           ('archivability.audit_events', 'INSERT')
                   ) AS required(table_name, privilege)
               )
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_user
        """
    ).fetchone()
    if row is None or any(bool(value) for value in row):
        raise RuntimeConfigurationError(
            "database runtime role has forbidden administrative or DDL privileges"
        )


def create_production_app(
    settings: ProductionSettings,
    *,
    pool: RuntimePool | None = None,
    verifier: Any | None = None,
    resolver: AddressResolver | None = None,
    probe: Probe | None = None,
    rate_limiter: ApiRateLimiter | None = None,
) -> ProductionAsgiApplication:
    """Create the production ASGI graph without opening database connections."""
    if not isinstance(settings, ProductionSettings):
        raise RuntimeConfigurationError("settings must be ProductionSettings")
    methodology = load_methodology(settings.methodology_path)
    if verifier is None:
        verifier = OidcJwtVerifier(settings.oidc)
    if resolver is None:
        resolver = SystemAddressResolver()
    if probe is None:
        probe = HttpMetadataProbe()
    if rate_limiter is None:
        rate_limiter = InMemoryTokenBucketRateLimiter(
            {
                "analysis.create": RateLimitRule(capacity=10, refill_seconds=60),
                "analysis.read": RateLimitRule(capacity=120, refill_seconds=60),
            }
        )
    if pool is None:
        try:
            pool_module = importlib.import_module("psycopg_pool")
        except ImportError as exc:
            raise RuntimeConfigurationError(
                "psycopg_pool is required for production runtime"
            ) from exc
        pool = pool_module.ConnectionPool(
            settings.database_dsn,
            kwargs={
                "autocommit": True,
                "connect_timeout": settings.pool_timeout_seconds,
                "ssl_min_protocol_version": "TLSv1.2",
                "application_name": (
                    f"arquivabilidade-ja-{settings.environment}"
                ),
            },
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            max_waiting=settings.pool_max_size * 2,
            timeout=float(settings.pool_timeout_seconds),
            open=False,
            configure=validate_runtime_database_role,
            check=pool_module.ConnectionPool.check_connection,
            name=f"arquivabilidade-ja-{settings.environment}",
        )

    instance_id = str(uuid4())

    def repository(connection: psycopg.Connection[Any]):
        return PostgreSqlAssessmentJobRepository(connection)

    def request_app(connection: psycopg.Connection[Any]) -> AsgiApplication:
        persistence = repository(connection)
        orchestrator = AnalysisOrchestrator(persistence)
        runner = ProbeRunner(
            policy=SsrfPolicy(),
            resolver=resolver,
            event_recorder=persistence,
            authorizer=persistence,
        )
        workflow = HttpAssessmentWorkflow(
            methodology=methodology,
            repository=persistence,
            orchestrator=orchestrator,
            probe_execution=ProbeExecutionService(
                runner=runner,
                repository=persistence,
            ),
            queue=HttpAssessmentQueueService(repository=persistence),
        )
        api = AnalysisApi(
            workflow=workflow,
            reports=AnalysisReportService(persistence),
            probe=probe,
            authorization=OwnershipAuthorizationPolicy(persistence),
            rate_limiter=rate_limiter,
            audit_recorder=persistence,
        )
        http_app = AnalysisAsgiApp(
            api=api,
            audit_recorder=persistence,
            max_body_bytes=settings.max_request_body_bytes,
            sync_runner=ThreadedAsgiSyncRunner(),
        )
        return http_app

    def lifecycle_recorder(
        connection: psycopg.Connection[Any], status: str
    ) -> None:
        repository(connection).record_application_lifecycle_event(
            instance_id=instance_id,
            status=status,
            audit=AuditContext(),
        )

    core = PooledAsgiApplication(
        pool=pool,
        request_app_factory=request_app,
        lifecycle_recorder=lifecycle_recorder,
        pool_timeout_seconds=settings.pool_timeout_seconds,
        pool_close_timeout_seconds=settings.pool_close_timeout_seconds,
    )
    return ProductionAsgiApplication(
        core,
        verifier=verifier,
        audit_recorder=_PooledAuthenticationAuditRecorder(
            pool, timeout_seconds=settings.pool_timeout_seconds
        ),
        max_token_bytes=settings.max_bearer_token_bytes,
    )
