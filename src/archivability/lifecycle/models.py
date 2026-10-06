from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit


_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_FAILURE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class LifecycleError(ValueError):
    """Raised when lifecycle data or a state transition is invalid."""


class AnalysisState(StrEnum):
    REQUESTED = "requested"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


ANALYSIS_TERMINAL_STATES = frozenset(
    {
        AnalysisState.COMPLETED,
        AnalysisState.PARTIALLY_COMPLETED,
        AnalysisState.FAILED,
        AnalysisState.CANCELLED,
    }
)
ATTEMPT_TERMINAL_STATES = frozenset(
    {AttemptState.SUCCEEDED, AttemptState.FAILED, AttemptState.CANCELLED}
)


def _text(value: str, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise LifecycleError(
            f"{field} must be a non-empty string of at most {maximum} characters"
        )
    if _CONTROL_CHARACTER_PATTERN.search(value):
        raise LifecycleError(f"{field} cannot contain control characters")
    return value


def _timestamp(value: datetime | None, field: str) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise LifecycleError(f"{field} must include a timezone")
    return value


def _subject_uri(value: str) -> str:
    _text(value, "subject_uri", 2048)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LifecycleError("subject_uri must be an absolute HTTP(S) URI")
    if parsed.username is not None or parsed.password is not None:
        raise LifecycleError("subject_uri cannot contain credentials")
    return value


@dataclass(frozen=True, slots=True)
class Analysis:
    analysis_id: str
    subject_uri: str
    methodology_id: str
    methodology_version: str
    state: AnalysisState
    max_attempts: int
    attempt_ids: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    revision: int = 0

    def __post_init__(self) -> None:
        for field in ("analysis_id", "methodology_id", "methodology_version"):
            _text(getattr(self, field), field)
        _subject_uri(self.subject_uri)
        if not isinstance(self.state, AnalysisState):
            raise LifecycleError("state must be an AnalysisState")
        if not isinstance(self.max_attempts, int) or not 1 <= self.max_attempts <= 10:
            raise LifecycleError("max_attempts must be between 1 and 10")
        if len(self.attempt_ids) > self.max_attempts:
            raise LifecycleError("attempt_ids exceed max_attempts")
        if len(set(self.attempt_ids)) != len(self.attempt_ids):
            raise LifecycleError("attempt_ids must be unique")
        for attempt_id in self.attempt_ids:
            _text(attempt_id, "attempt_id")
        for field in ("created_at", "updated_at", "started_at", "finished_at"):
            _timestamp(getattr(self, field), field)
        if self.updated_at < self.created_at:
            raise LifecycleError("updated_at cannot precede created_at")
        if self.started_at is not None and not self.created_at <= self.started_at <= self.updated_at:
            raise LifecycleError("started_at must be within the analysis lifetime")
        if self.finished_at is not None and not self.created_at <= self.finished_at <= self.updated_at:
            raise LifecycleError("finished_at must be within the analysis lifetime")
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise LifecycleError("finished_at cannot precede started_at")
        if not isinstance(self.revision, int) or self.revision < 0:
            raise LifecycleError("revision must be a non-negative integer")
        if self.state is AnalysisState.REQUESTED:
            if self.started_at is not None or self.finished_at is not None:
                raise LifecycleError("requested analysis cannot have execution timestamps")
        elif self.state is AnalysisState.RUNNING:
            if self.started_at is None or self.finished_at is not None:
                raise LifecycleError("running analysis requires only started_at")
        elif self.state in ANALYSIS_TERMINAL_STATES:
            if self.finished_at is None:
                raise LifecycleError("terminal analysis requires finished_at")
            if self.state is not AnalysisState.CANCELLED and self.started_at is None:
                raise LifecycleError("completed or failed analysis requires started_at")

    @classmethod
    def create(
        cls,
        *,
        analysis_id: str,
        subject_uri: str,
        methodology_id: str,
        methodology_version: str,
        created_at: datetime,
        max_attempts: int = 3,
    ) -> Analysis:
        return cls(
            analysis_id=analysis_id,
            subject_uri=subject_uri,
            methodology_id=methodology_id,
            methodology_version=methodology_version,
            state=AnalysisState.REQUESTED,
            max_attempts=max_attempts,
            attempt_ids=(),
            created_at=created_at,
            updated_at=created_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_id": self.analysis_id,
            "subject_uri": self.subject_uri,
            "methodology_id": self.methodology_id,
            "methodology_version": self.methodology_version,
            "state": self.state.value,
            "max_attempts": self.max_attempts,
            "attempt_ids": list(self.attempt_ids),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class Attempt:
    attempt_id: str
    analysis_id: str
    sequence: int
    state: AttemptState
    created_at: datetime
    updated_at: datetime
    started_at: datetime
    finished_at: datetime | None = None
    failure_code: str | None = None
    revision: int = 0

    def __post_init__(self) -> None:
        _text(self.attempt_id, "attempt_id")
        _text(self.analysis_id, "analysis_id")
        if not isinstance(self.sequence, int) or self.sequence < 1:
            raise LifecycleError("sequence must be a positive integer")
        if not isinstance(self.state, AttemptState):
            raise LifecycleError("state must be an AttemptState")
        for field in ("created_at", "updated_at", "started_at", "finished_at"):
            _timestamp(getattr(self, field), field)
        if self.updated_at < self.created_at or self.started_at < self.created_at:
            raise LifecycleError("attempt timestamps are inconsistent")
        if self.started_at > self.updated_at:
            raise LifecycleError("started_at cannot follow updated_at")
        if self.finished_at is not None and not self.started_at <= self.finished_at <= self.updated_at:
            raise LifecycleError("finished_at must be within the attempt lifetime")
        if not isinstance(self.revision, int) or self.revision < 0:
            raise LifecycleError("revision must be a non-negative integer")
        if self.state is AttemptState.RUNNING:
            if self.finished_at is not None or self.failure_code is not None:
                raise LifecycleError("running attempt cannot have terminal fields")
        else:
            if self.finished_at is None:
                raise LifecycleError("terminal attempt requires finished_at")
            if self.state is AttemptState.FAILED:
                if self.failure_code is None:
                    raise LifecycleError("failed attempt requires failure_code")
                if not _FAILURE_CODE_PATTERN.fullmatch(self.failure_code):
                    raise LifecycleError(
                        "failure_code must be an uppercase machine-readable code"
                    )
            elif self.failure_code is not None:
                raise LifecycleError("only failed attempts can have failure_code")

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "analysis_id": self.analysis_id,
            "sequence": self.sequence,
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "failure_code": self.failure_code,
            "revision": self.revision,
        }
