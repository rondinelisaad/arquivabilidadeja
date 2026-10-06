from __future__ import annotations

import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import psycopg  # noqa: E402

from archivability import (  # noqa: E402
    ApiPrincipal,
    PostgreSqlTokenBucketRateLimiter,
    RateLimitRule,
    apply_postgresql_migrations,
)

POSTGRES_DSN = os.environ.get("ARCHIVABILITY_TEST_POSTGRES_DSN")
POSTGRES_RUNTIME_DSN = os.environ.get("ARCHIVABILITY_TEST_POSTGRES_RUNTIME_DSN")


@unittest.skipUnless(
    POSTGRES_DSN and POSTGRES_RUNTIME_DSN,
    "PostgreSQL migration and restricted runtime DSNs are not configured",
)
class PostgreSqlRateLimiterIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert POSTGRES_DSN is not None
        with psycopg.connect(POSTGRES_DSN) as connection:
            apply_postgresql_migrations(connection)

    def setUp(self) -> None:
        assert POSTGRES_RUNTIME_DSN is not None
        self.connection = psycopg.connect(POSTGRES_RUNTIME_DSN, autocommit=True)
        self.connection.execute("DELETE FROM archivability.rate_limit_buckets")
        self.principal = ApiPrincipal(user_id="rate-user-1", session_id="session-1")

    def tearDown(self) -> None:
        self.connection.close()

    def test_bucket_is_shared_and_denies_after_capacity(self) -> None:
        rules = {"analysis.create": RateLimitRule(capacity=2, refill_seconds=60)}
        first = PostgreSqlTokenBucketRateLimiter(self.connection, rules)
        second = PostgreSqlTokenBucketRateLimiter(self.connection, rules)

        self.assertTrue(first.allow(self.principal, "analysis.create"))
        self.assertTrue(second.allow(self.principal, "analysis.create"))
        self.assertFalse(first.allow(self.principal, "analysis.create"))
        self.assertFalse(first.allow(self.principal, "unknown"))

    def test_concurrent_replicas_cannot_exceed_capacity(self) -> None:
        assert POSTGRES_RUNTIME_DSN is not None
        principal = ApiPrincipal(
            user_id="rate-concurrent-user",
            session_id="session-concurrent",
        )
        rules = {"analysis.create": RateLimitRule(capacity=1, refill_seconds=60)}
        barrier = Barrier(2)

        def consume() -> bool:
            with psycopg.connect(POSTGRES_RUNTIME_DSN, autocommit=True) as connection:
                limiter = PostgreSqlTokenBucketRateLimiter(connection, rules)
                barrier.wait()
                return limiter.allow(principal, "analysis.create")

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: consume(), range(2)))

        self.assertEqual([False, True], sorted(results))

    def test_capacity_is_bounded_and_idle_bucket_is_pruned(self) -> None:
        rules = {"analysis.read": RateLimitRule(capacity=1, refill_seconds=1)}
        limiter = PostgreSqlTokenBucketRateLimiter(
            self.connection,
            rules,
            max_buckets=1,
        )
        other = ApiPrincipal(user_id="rate-user-2", session_id="session-2")

        self.assertTrue(limiter.allow(self.principal, "analysis.read"))
        self.connection.execute(
            """
            UPDATE archivability.rate_limit_buckets
            SET updated_at = clock_timestamp() - interval '2 seconds'
            WHERE user_id = %s AND operation = %s
            """,
            (self.principal.user_id, "analysis.read"),
        )
        self.assertTrue(limiter.allow(other, "analysis.read"))
        count = self.connection.execute(
            "SELECT count(*) FROM archivability.rate_limit_buckets"
        ).fetchone()
        self.assertEqual(1, count[0])


if __name__ == "__main__":
    unittest.main()
