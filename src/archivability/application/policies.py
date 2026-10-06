from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from archivability.application.api import ApiPrincipal


_OPERATION_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")


class PermissionAuthorizationPolicy:
    """Coarse backend authorization based on permissions from a verified token."""

    def __init__(
        self,
        *,
        create_permission: str = "analysis:create",
        read_permission: str = "analysis:read",
    ) -> None:
        for value in (create_permission, read_permission):
            if not isinstance(value, str) or not _OPERATION_PATTERN.fullmatch(value):
                raise ValueError("authorization permission is invalid")
        self._create_permission = create_permission
        self._read_permission = read_permission

    def can_create_analysis(self, principal: ApiPrincipal) -> bool:
        return isinstance(principal, ApiPrincipal) and (
            self._create_permission in principal.permissions
        )

    def can_read_analysis(self, principal: ApiPrincipal, analysis_id: str) -> bool:
        del analysis_id
        return isinstance(principal, ApiPrincipal) and (
            self._read_permission in principal.permissions
        )


class AnalysisOwnershipReader(Protocol):
    def get_analysis_owner(self, analysis_id: str) -> str | None: ...


class OwnershipAuthorizationPolicy:
    """Authorize reads by immutable ownership or an explicit global permission."""

    def __init__(
        self,
        ownership: AnalysisOwnershipReader,
        *,
        create_permission: str = "analysis:create",
        read_permission: str = "analysis:read",
        read_any_permission: str = "analysis:read:any",
    ) -> None:
        for value in (create_permission, read_permission, read_any_permission):
            if not isinstance(value, str) or not _OPERATION_PATTERN.fullmatch(value):
                raise ValueError("authorization permission is invalid")
        self._ownership = ownership
        self._create_permission = create_permission
        self._read_permission = read_permission
        self._read_any_permission = read_any_permission

    def can_create_analysis(self, principal: ApiPrincipal) -> bool:
        return isinstance(principal, ApiPrincipal) and (
            self._create_permission in principal.permissions
        )

    def can_read_analysis(self, principal: ApiPrincipal, analysis_id: str) -> bool:
        if not isinstance(principal, ApiPrincipal):
            return False
        if self._read_any_permission in principal.permissions:
            return True
        if self._read_permission not in principal.permissions:
            return False
        return self._ownership.get_analysis_owner(analysis_id) == principal.user_id


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    capacity: int
    refill_seconds: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.capacity, int)
            or isinstance(self.capacity, bool)
            or not 1 <= self.capacity <= 10_000
        ):
            raise ValueError("rate limit capacity must be between 1 and 10000")
        if (
            not isinstance(self.refill_seconds, int)
            or isinstance(self.refill_seconds, bool)
            or not 1 <= self.refill_seconds <= 86_400
        ):
            raise ValueError("rate limit refill_seconds must be between 1 and 86400")


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class InMemoryTokenBucketRateLimiter:
    """Thread-safe, bounded per-user limiter for one application process."""

    def __init__(
        self,
        rules: Mapping[str, RateLimitRule],
        *,
        max_buckets: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
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
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._rules = normalized
        self._max_buckets = max_buckets
        self._clock = clock
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, principal: ApiPrincipal, operation: str) -> bool:
        if not isinstance(principal, ApiPrincipal) or not isinstance(operation, str):
            return False
        rule = self._rules.get(operation)
        if rule is None:
            return False
        try:
            now = float(self._clock())
        except Exception:
            return False
        if not math.isfinite(now):
            return False
        key = (principal.user_id, operation)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_buckets:
                    self._prune_fully_refilled(now)
                if len(self._buckets) >= self._max_buckets:
                    return False
                self._buckets[key] = _Bucket(
                    tokens=float(rule.capacity - 1),
                    updated_at=now,
                )
                return True
            elapsed = now - bucket.updated_at
            if elapsed < 0:
                return False
            available = min(
                float(rule.capacity),
                bucket.tokens + elapsed * rule.capacity / rule.refill_seconds,
            )
            if available < 1.0:
                bucket.tokens = available
                bucket.updated_at = now
                return False
            bucket.tokens = available - 1.0
            bucket.updated_at = now
            return True

    def _prune_fully_refilled(self, now: float) -> None:
        expired = [
            key
            for key, bucket in self._buckets.items()
            if now >= bucket.updated_at
            and now - bucket.updated_at >= self._rules[key[1]].refill_seconds
        ]
        for key in expired:
            del self._buckets[key]
