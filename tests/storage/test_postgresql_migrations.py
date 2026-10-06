from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from archivability import PersistenceError, apply_postgresql_migrations  # noqa: E402


class FakeCursor:
    def __init__(self, applied: list[tuple[str, str]]) -> None:
        self.applied = applied
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False

    def execute(self, query: str, parameters: tuple[Any, ...] = ()) -> None:
        self.executed.append((query, parameters))

    def fetchall(self) -> list[tuple[str, str]]:
        return self.applied

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(
        self,
        applied: list[tuple[str, str]] | None = None,
        *,
        autocommit: bool = False,
        transaction_status: int = 0,
    ) -> None:
        self.autocommit = autocommit
        self.info = SimpleNamespace(transaction_status=transaction_status)
        self.fake_cursor = FakeCursor(applied or [])
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return self.fake_cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class PostgreSqlMigrationTests(unittest.TestCase):
    def test_applies_versioned_migration_under_lock_and_commits(self) -> None:
        connection = FakeConnection()

        apply_postgresql_migrations(connection)

        queries = [query for query, _ in connection.fake_cursor.executed]
        inserts = [
            parameters
            for query, parameters in connection.fake_cursor.executed
            if "INSERT INTO archivability.schema_migrations" in query
        ]
        self.assertTrue(any("pg_advisory_xact_lock" in query for query in queries))
        self.assertTrue(any("unexpected owner" in query for query in queries))
        self.assertTrue(any("applied_by" in query for query in queries))
        self.assertTrue(any("CREATE TABLE analyses" in query for query in queries))
        self.assertEqual(1, connection.commits)
        self.assertEqual(0, connection.rollbacks)
        self.assertTrue(connection.fake_cursor.closed)
        self.assertEqual("001_initial", inserts[0][0])
        self.assertEqual(64, len(inserts[0][1]))

    def test_changed_applied_migration_is_rejected_and_rolled_back(self) -> None:
        connection = FakeConnection([("001_initial", "0" * 64)])

        with self.assertRaisesRegex(PersistenceError, "checksum changed"):
            apply_postgresql_migrations(connection)

        self.assertEqual(0, connection.commits)
        self.assertEqual(1, connection.rollbacks)
        self.assertTrue(connection.fake_cursor.closed)

    def test_requires_idle_non_autocommit_connection(self) -> None:
        with self.assertRaisesRegex(PersistenceError, "autocommit"):
            apply_postgresql_migrations(FakeConnection(autocommit=True))
        with self.assertRaisesRegex(PersistenceError, "idle"):
            apply_postgresql_migrations(FakeConnection(transaction_status=2))

    def test_schema_and_runtime_grants_preserve_least_privilege(self) -> None:
        migration = (
            ROOT
            / "src/archivability/storage/migrations/postgresql/001_initial.sql"
        ).read_text(encoding="utf-8")
        grants = (ROOT / "deploy/postgresql/runtime_grants.sql").read_text(
            encoding="utf-8"
        )

        for table in (
            "analyses",
            "attempts",
            "observations",
            "evidence",
            "assessment_jobs",
            "analysis_ownership",
            "audit_events",
        ):
            self.assertIn(f"CREATE TABLE {table}", migration)
        self.assertIn("JSONB", migration)
        self.assertIn("TIMESTAMPTZ", migration)
        self.assertIn("REVOKE ALL ON ALL TABLES", grants)
        self.assertIn("REVOKE ALL ON ALL FUNCTIONS", grants)
        self.assertIn('REVOKE ALL ON DATABASE :"database_name" FROM PUBLIC', grants)
        self.assertNotIn("GRANT ALL", grants)
        self.assertNotIn("GRANT DELETE", grants)
        self.assertNotIn("CREATE ROLE", grants)
        self.assertNotIn("PASSWORD", grants.upper())


if __name__ == "__main__":
    unittest.main()
