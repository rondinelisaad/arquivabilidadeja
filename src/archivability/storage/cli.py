from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence

import psycopg
from psycopg.conninfo import conninfo_to_dict

from archivability.storage.postgresql import apply_postgresql_migrations


class MigrationConfigurationError(ValueError):
    """Raised without echoing a migration credential or DSN."""


def validate_migration_dsn(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise MigrationConfigurationError("migration database DSN is invalid")
    try:
        parameters = conninfo_to_dict(value)
    except psycopg.Error:
        raise MigrationConfigurationError("migration database DSN is invalid") from None
    if parameters.get("service"):
        raise MigrationConfigurationError(
            "migration database DSN cannot use a service definition"
        )
    user = parameters.get("user", "").casefold()
    if not user or user in {"postgres", "root", "admin", "administrator"}:
        raise MigrationConfigurationError(
            "migration database DSN must name a dedicated migration user"
        )
    if not parameters.get("dbname"):
        raise MigrationConfigurationError("migration database DSN must name a database")
    hosts = parameters.get("host", "").split(",")
    host_addresses = parameters.get("hostaddr", "").split(",")
    local_hosts = {"", "localhost", "127.0.0.1", "::1"}
    remote = any(
        host not in local_hosts and not host.startswith("/")
        for host in (*hosts, *host_addresses)
    )
    if remote and parameters.get("sslmode") != "verify-full":
        raise MigrationConfigurationError(
            "remote migration connections require sslmode=verify-full"
        )


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        raise MigrationConfigurationError("migration command accepts no arguments")
    values = os.environ if environment is None else environment
    dsn = values.get("ARCHIVABILITY_MIGRATION_DATABASE_DSN", "")
    validate_migration_dsn(dsn)
    try:
        with psycopg.connect(
            dsn,
            autocommit=False,
            application_name="arquivabilidade-ja-migration",
        ) as connection:
            apply_postgresql_migrations(connection)
    except psycopg.Error as exc:
        raise MigrationConfigurationError("database migration failed") from exc
    sys.stdout.write("database migrations applied\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
