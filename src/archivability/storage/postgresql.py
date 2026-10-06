from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Protocol

from archivability.storage.errors import PersistenceError


_MIGRATIONS = Path(__file__).parent / "migrations" / "postgresql"
_MIGRATION_LOCK_ID = 4_157_263_891


class PostgreSqlCursor(Protocol):
    def execute(self, query: str, parameters: tuple[Any, ...] = ()) -> Any: ...

    def fetchall(self) -> list[tuple[Any, ...]]: ...

    def close(self) -> None: ...


class PostgreSqlMigrationConnection(Protocol):
    autocommit: bool

    def cursor(self) -> PostgreSqlCursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def apply_postgresql_migrations(connection: PostgreSqlMigrationConnection) -> None:
    """Apply immutable PostgreSQL migrations using a dedicated migration account."""
    if connection.autocommit:
        raise PersistenceError("PostgreSQL migrations require autocommit to be disabled")
    if not _connection_is_idle(connection):
        raise PersistenceError("PostgreSQL migrations require an idle connection")

    cursor = connection.cursor()
    try:
        cursor.execute("SET LOCAL lock_timeout = '5s'")
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_ID,))
        cursor.execute("CREATE SCHEMA IF NOT EXISTS archivability")
        cursor.execute("REVOKE ALL ON SCHEMA archivability FROM PUBLIC")
        cursor.execute(
            """
            DO $block$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_namespace
                    WHERE nspname = 'archivability'
                      AND pg_catalog.pg_get_userbyid(nspowner) = CURRENT_USER
                ) THEN
                    RAISE EXCEPTION 'archivability schema has an unexpected owner';
                END IF;
            END;
            $block$
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS archivability.schema_migrations (
                version TEXT PRIMARY KEY,
                checksum TEXT NOT NULL CHECK (length(checksum) = 64),
                applied_by TEXT NOT NULL DEFAULT CURRENT_USER,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            "SELECT version, checksum FROM archivability.schema_migrations"
        )
        applied = dict(cursor.fetchall())
        for migration in sorted(_MIGRATIONS.glob("*.sql")):
            version = migration.stem
            sql = migration.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            previous_checksum = applied.get(version)
            if previous_checksum is not None:
                if previous_checksum != checksum:
                    raise PersistenceError(
                        f"PostgreSQL migration checksum changed: {version}"
                    )
                continue
            cursor.execute(sql)
            cursor.execute(
                """
                INSERT INTO archivability.schema_migrations (version, checksum)
                VALUES (%s, %s)
                """,
                (version, checksum),
            )
        connection.commit()
    except Exception as exc:
        connection.rollback()
        if isinstance(exc, PersistenceError):
            raise
        raise PersistenceError("PostgreSQL migration failed") from exc
    finally:
        cursor.close()


def _connection_is_idle(connection: PostgreSqlMigrationConnection) -> bool:
    get_status = getattr(connection, "get_transaction_status", None)
    if callable(get_status):
        return int(get_status()) == 0
    info = getattr(connection, "info", None)
    status = getattr(info, "transaction_status", 0)
    return int(status) == 0
