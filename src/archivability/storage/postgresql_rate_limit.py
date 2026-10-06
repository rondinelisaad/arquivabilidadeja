from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import psycopg

from archivability.application.api import ApiPrincipal
from archivability.application.policies import RateLimitRule
from archivability.storage.errors import PersistenceError

_OPERATION_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_BUCKET_CREATION_LOCK_ID = 7_903_184_221


class PostgreSqlTokenBucketRateLimiter:
    """Transactional token buckets shared by every application replica."""

    def __init__(
        self,
        connection: psycopg.Connection[Any],
        rules: Mapping[str, RateLimitRule],
        *,
        max_buckets: int = 100_000,
    ) -> None:
        if not connection.autocommit:
            raise PersistenceError(
                "PostgreSQL runtime connections require autocommit to be enabled"
            )
        if not isinstance(rules, Mapping) or not rules:
            raise ValueError("at least one rate limit rule is required")
        normalized: dict[str, RateLimitRule] = {}
        for operation, rule in rules.items():
            if (
                not isinstance(operation, str)
                or not _OPERATION_PATTERN.fullmatch(operation)
                or not isinstance(rule, RateLimitRule)
            ):
                raise ValueError("rate limit rule is invalid")
            normalized[operation] = rule
        if (
            not isinstance(max_buckets, int)
            or isinstance(max_buckets, bool)
            or not 1 <= max_buckets <= 1_000_000
        ):
            raise ValueError("max_buckets must be between 1 and 1000000")
        self._connection = connection
        self._rules = normalized
        self._max_buckets = max_buckets

    def allow(self, principal: ApiPrincipal, operation: str) -> bool:
        if not isinstance(principal, ApiPrincipal) or not isinstance(operation, str):
            return False
        rule = self._rules.get(operation)
        if rule is None:
            return False
        try:
            with self._connection.transaction():
                bucket = self._get_locked_bucket(principal.user_id, operation)
                if bucket is None:
                    return self._create_bucket(
                        principal.user_id,
                        operation,
                        rule,
                    )
                return self._consume_existing_bucket(
                    principal.user_id,
                    operation,
                    rule,
                    bucket,
                )
        except psycopg.Error as exc:
            raise PersistenceError("PostgreSQL rate limit transaction failed") from exc

    def _get_locked_bucket(
        self, user_id: str, operation: str
    ) -> tuple[float, datetime, datetime] | None:
        row = self._connection.execute(
            """
            SELECT tokens, updated_at, clock_timestamp()
            FROM archivability.rate_limit_buckets
            WHERE user_id = %s AND operation = %s
            FOR UPDATE
            """,
            (user_id, operation),
        ).fetchone()
        return row if row is not None else None

    def _create_bucket(
        self,
        user_id: str,
        operation: str,
        rule: RateLimitRule,
    ) -> bool:
        self._connection.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (_BUCKET_CREATION_LOCK_ID,),
        )
        existing = self._get_locked_bucket(user_id, operation)
        if existing is not None:
            return self._consume_existing_bucket(
                user_id,
                operation,
                rule,
                existing,
            )
        count = self._connection.execute(
            "SELECT count(*) FROM archivability.rate_limit_buckets"
        ).fetchone()
        if count is None:
            raise PersistenceError("PostgreSQL rate limit count is unavailable")
        if int(count[0]) >= self._max_buckets:
            self._connection.execute(
                """
                DELETE FROM archivability.rate_limit_buckets
                WHERE updated_at <= clock_timestamp()
                    - make_interval(secs => refill_seconds)
                """
            )
            count = self._connection.execute(
                "SELECT count(*) FROM archivability.rate_limit_buckets"
            ).fetchone()
            if count is None or int(count[0]) >= self._max_buckets:
                return False
        self._connection.execute(
            """
            INSERT INTO archivability.rate_limit_buckets
                (user_id, operation, tokens, capacity, refill_seconds, updated_at)
            VALUES (%s, %s, %s, %s, %s, clock_timestamp())
            """,
            (
                user_id,
                operation,
                float(rule.capacity - 1),
                rule.capacity,
                rule.refill_seconds,
            ),
        )
        return True

    def _consume_existing_bucket(
        self,
        user_id: str,
        operation: str,
        rule: RateLimitRule,
        bucket: tuple[float, datetime, datetime],
    ) -> bool:
        tokens, updated_at, now = bucket
        elapsed = (now - updated_at).total_seconds()
        if elapsed < 0:
            return False
        available = min(
            float(rule.capacity),
            float(tokens) + elapsed * rule.capacity / rule.refill_seconds,
        )
        if not math.isfinite(available):
            raise PersistenceError("PostgreSQL rate limit state is invalid")
        allowed = available >= 1.0
        remaining = available - 1.0 if allowed else available
        self._connection.execute(
            """
            UPDATE archivability.rate_limit_buckets
            SET tokens = %s, capacity = %s, refill_seconds = %s, updated_at = %s
            WHERE user_id = %s AND operation = %s
            """,
            (
                remaining,
                rule.capacity,
                rule.refill_seconds,
                now,
                user_id,
                operation,
            ),
        )
        return allowed
