from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class JobError(ValueError):
    """Raised when durable job data or a transition is invalid."""


class AssessmentJobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _text(value: str, field: str, *, maximum: int = 256) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise JobError(f"{field} must be a non-empty string of at most {maximum} characters")
    if _CONTROL_CHARACTER_PATTERN.search(value):
        raise JobError(f"{field} cannot contain control characters")


def _timestamp(value: datetime | None, field: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise JobError(f"{field} must include a timezone")


@dataclass(frozen=True, slots=True)
class AssessmentJob:
    job_id: str
    analysis_id: str
    observation_id: str
    state: AssessmentJobState
    attempt_count: int
    max_attempts: int
    available_at: datetime
    created_at: datetime
    updated_at: datetime
    claimed_by: str | None = None
    lease_expires_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: str | None = None
    revision: int = 0

    def __post_init__(self) -> None:
        for field in ("job_id", "analysis_id", "observation_id"):
            _text(getattr(self, field), field)
        if not isinstance(self.state, AssessmentJobState):
            raise JobError("state must be an AssessmentJobState")
        if not isinstance(self.max_attempts, int) or not 1 <= self.max_attempts <= 10:
            raise JobError("max_attempts must be between 1 and 10")
        if (
            not isinstance(self.attempt_count, int)
            or not 0 <= self.attempt_count <= self.max_attempts
        ):
            raise JobError("attempt_count must be within the configured limit")
        if not isinstance(self.revision, int) or self.revision < 0:
            raise JobError("revision must be a non-negative integer")
        for field in (
            "available_at",
            "created_at",
            "updated_at",
            "lease_expires_at",
            "completed_at",
        ):
            _timestamp(getattr(self, field), field)
        if self.updated_at < self.created_at or self.available_at < self.created_at:
            raise JobError("job timestamps are inconsistent")
        if self.error_code is not None and not _ERROR_CODE_PATTERN.fullmatch(
            self.error_code
        ):
            raise JobError("error_code must be uppercase and machine-readable")

        if self.state is AssessmentJobState.PENDING:
            if (
                self.claimed_by is not None
                or self.lease_expires_at is not None
                or self.completed_at is not None
            ):
                raise JobError("pending job cannot have claim or completion fields")
        elif self.state is AssessmentJobState.RUNNING:
            if self.claimed_by is None or self.lease_expires_at is None:
                raise JobError("running job requires a worker and lease")
            _text(self.claimed_by, "claimed_by", maximum=128)
            if self.completed_at is not None or self.error_code is not None:
                raise JobError("running job cannot have completion fields")
            if self.lease_expires_at <= self.updated_at:
                raise JobError("running job lease must expire after its update time")
        elif self.state is AssessmentJobState.SUCCEEDED:
            if (
                self.claimed_by is not None
                or self.lease_expires_at is not None
                or self.completed_at is None
                or self.error_code is not None
            ):
                raise JobError("successful job has inconsistent terminal fields")
        elif self.state is AssessmentJobState.FAILED:
            if (
                self.claimed_by is not None
                or self.lease_expires_at is not None
                or self.completed_at is None
                or self.error_code is None
                or self.attempt_count != self.max_attempts
            ):
                raise JobError("failed job has inconsistent terminal fields")

    @classmethod
    def create(
        cls,
        *,
        job_id: str,
        analysis_id: str,
        observation_id: str,
        created_at: datetime,
        max_attempts: int = 3,
    ) -> AssessmentJob:
        return cls(
            job_id=job_id,
            analysis_id=analysis_id,
            observation_id=observation_id,
            state=AssessmentJobState.PENDING,
            attempt_count=0,
            max_attempts=max_attempts,
            available_at=created_at,
            created_at=created_at,
            updated_at=created_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "analysis_id": self.analysis_id,
            "observation_id": self.observation_id,
            "state": self.state.value,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "available_at": self.available_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "claimed_by": self.claimed_by,
            "lease_expires_at": (
                self.lease_expires_at.isoformat() if self.lease_expires_at else None
            ),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error_code": self.error_code,
            "revision": self.revision,
        }
