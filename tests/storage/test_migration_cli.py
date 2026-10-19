from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from archivability.storage.cli import (  # noqa: E402
    MigrationConfigurationError,
    main,
    validate_migration_dsn,
)


class MigrationCliTests(unittest.TestCase):
    def test_rejects_administrative_and_insecure_remote_connections(self) -> None:
        with self.assertRaisesRegex(MigrationConfigurationError, "dedicated"):
            validate_migration_dsn("postgresql://postgres@localhost/database")
        with self.assertRaisesRegex(MigrationConfigurationError, "verify-full"):
            validate_migration_dsn(
                "postgresql://migration@database.example/database?sslmode=require"
            )

    def test_accepts_dedicated_local_socket_connection(self) -> None:
        validate_migration_dsn(
            "postgresql://archivability_migration@/archive?host=/var/run/postgresql"
        )

    def test_error_never_echoes_secret(self) -> None:
        secret = "do-not-print-this-password"
        with self.assertRaises(MigrationConfigurationError) as raised:
            main(
                [],
                environment={
                    "ARCHIVABILITY_MIGRATION_DATABASE_DSN": (
                        f"postgresql://postgres:{secret}@localhost/archive"
                    )
                },
            )
        self.assertNotIn(secret, str(raised.exception))

    def test_runs_migrations_without_accepting_cli_secrets(self) -> None:
        connection = object()
        manager = MagicMock()
        manager.__enter__.return_value = connection
        with (
            patch("archivability.storage.cli.psycopg.connect", return_value=manager),
            patch("archivability.storage.cli.apply_postgresql_migrations") as migrate,
        ):
            self.assertEqual(
                0,
                main(
                    [],
                    environment={
                        "ARCHIVABILITY_MIGRATION_DATABASE_DSN": (
                            "postgresql://archivability_migration@/archive"
                            "?host=/var/run/postgresql"
                        )
                    },
                ),
            )
        migrate.assert_called_once_with(connection)


if __name__ == "__main__":
    unittest.main()
