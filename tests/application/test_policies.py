from __future__ import annotations

import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from archivability import (  # noqa: E402
    ApiPrincipal,
    ApiValidationError,
    InMemoryTokenBucketRateLimiter,
    PermissionAuthorizationPolicy,
    RateLimitRule,
)


class MutableClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def principal(user_id: str, *permissions: str) -> ApiPrincipal:
    return ApiPrincipal(
        user_id=user_id,
        session_id=f"session-{user_id}",
        permissions=frozenset(permissions),
    )


class PermissionAuthorizationPolicyTests(unittest.TestCase):
    def test_permissions_are_enforced_independently(self) -> None:
        policy = PermissionAuthorizationPolicy()
        creator = principal("creator", "analysis:create")
        reader = principal("reader", "analysis:read")

        self.assertTrue(policy.can_create_analysis(creator))
        self.assertFalse(policy.can_read_analysis(creator, "analysis-1"))
        self.assertFalse(policy.can_create_analysis(reader))
        self.assertTrue(policy.can_read_analysis(reader, "analysis-1"))

    def test_invalid_or_mutable_principal_permissions_are_rejected(self) -> None:
        with self.assertRaises(ApiValidationError):
            ApiPrincipal(
                user_id="user-1",
                session_id="session-1",
                permissions={"analysis:read"},  # type: ignore[arg-type]
            )
        with self.assertRaises(ApiValidationError):
            principal("user-1", "permission with spaces")


class InMemoryTokenBucketRateLimiterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.rules = {
            "analysis.create": RateLimitRule(capacity=2, refill_seconds=10),
            "analysis.read": RateLimitRule(capacity=4, refill_seconds=10),
        }
        self.limiter = InMemoryTokenBucketRateLimiter(
            self.rules,
            clock=self.clock,
        )
        self.user = principal("user-1")

    def test_capacity_is_consumed_and_refilled_over_time(self) -> None:
        self.assertTrue(self.limiter.allow(self.user, "analysis.create"))
        self.assertTrue(self.limiter.allow(self.user, "analysis.create"))
        self.assertFalse(self.limiter.allow(self.user, "analysis.create"))

        self.clock.now += 5
        self.assertTrue(self.limiter.allow(self.user, "analysis.create"))
        self.assertFalse(self.limiter.allow(self.user, "analysis.create"))

    def test_invalid_rules_and_bounds_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RateLimitRule(capacity=0, refill_seconds=60)
        with self.assertRaises(ValueError):
            RateLimitRule(capacity=1, refill_seconds=0)
        with self.assertRaises(ValueError):
            InMemoryTokenBucketRateLimiter({})
        with self.assertRaises(ValueError):
            InMemoryTokenBucketRateLimiter(
                {"Invalid Operation": RateLimitRule(capacity=1, refill_seconds=1)}
            )
        with self.assertRaises(ValueError):
            InMemoryTokenBucketRateLimiter(self.rules, max_buckets=0)

    def test_buckets_are_isolated_by_user_and_operation(self) -> None:
        other = principal("user-2")
        for _ in range(2):
            self.assertTrue(self.limiter.allow(self.user, "analysis.create"))

        self.assertFalse(self.limiter.allow(self.user, "analysis.create"))
        self.assertTrue(self.limiter.allow(other, "analysis.create"))
        self.assertTrue(self.limiter.allow(self.user, "analysis.read"))

    def test_unknown_operation_and_clock_failure_are_denied(self) -> None:
        self.assertFalse(self.limiter.allow(self.user, "analysis.delete"))
        broken = InMemoryTokenBucketRateLimiter(
            self.rules,
            clock=lambda: (_ for _ in ()).throw(RuntimeError("clock failed")),
        )
        self.assertFalse(broken.allow(self.user, "analysis.create"))

    def test_memory_bound_denies_new_bucket_until_stale_entry_is_pruned(self) -> None:
        limiter = InMemoryTokenBucketRateLimiter(
            {"analysis.create": self.rules["analysis.create"]},
            max_buckets=1,
            clock=self.clock,
        )
        other = principal("user-2")

        self.assertTrue(limiter.allow(self.user, "analysis.create"))
        self.assertFalse(limiter.allow(other, "analysis.create"))
        self.clock.now += 10
        self.assertTrue(limiter.allow(other, "analysis.create"))

    def test_concurrent_requests_cannot_exceed_capacity(self) -> None:
        limiter = InMemoryTokenBucketRateLimiter(
            {"analysis.create": RateLimitRule(capacity=10, refill_seconds=60)},
            clock=self.clock,
        )
        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(
                executor.map(
                    lambda _: limiter.allow(self.user, "analysis.create"),
                    range(100),
                )
            )

        self.assertEqual(10, sum(results))


if __name__ == "__main__":
    unittest.main()
