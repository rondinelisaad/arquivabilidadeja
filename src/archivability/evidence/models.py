from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


class EvidenceValidationError(ValueError):
    """Raised when observation or evidence provenance is invalid."""


def _required_text(value: str, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise EvidenceValidationError(
            f"{field} must be a non-empty string of at most {maximum} characters"
        )
    if _CONTROL_CHARACTER_PATTERN.search(value):
        raise EvidenceValidationError(f"{field} cannot contain control characters")
    return value


def _aware_datetime(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise EvidenceValidationError(f"{field} must include a timezone")
    return value


def _canonical_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _subject_uri(value: str) -> str:
    _required_text(value, "subject_uri", maximum=2048)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise EvidenceValidationError("subject_uri must be an absolute HTTP(S) URI")
    if parsed.username is not None or parsed.password is not None:
        raise EvidenceValidationError("subject_uri cannot contain credentials")
    return value


def _freeze_json(value: Any, path: str = "payload") -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EvidenceValidationError(f"{path} cannot contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise EvidenceValidationError(f"{path} object keys must be strings")
            frozen[key] = _freeze_json(item, f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, f"{path}[]") for item in value)
    raise EvidenceValidationError(f"{path} must contain only JSON-compatible values")


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _content_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    # SHA-256 is used for integrity and reproducibility, never for password storage.
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class Observation:
    observation_id: str
    analysis_id: str
    attempt_id: str
    kind: str
    subject_uri: str
    observed_at: datetime
    probe_id: str
    tool_name: str
    tool_version: str
    payload_schema_version: str
    payload: Mapping[str, Any]
    content_hash: str
    error_code: str | None = None
    truncated: bool = False

    def __post_init__(self) -> None:
        for field in (
            "observation_id",
            "analysis_id",
            "attempt_id",
            "kind",
            "probe_id",
            "tool_name",
            "tool_version",
            "payload_schema_version",
        ):
            _required_text(getattr(self, field), field)
        _subject_uri(self.subject_uri)
        _aware_datetime(self.observed_at, "observed_at")
        if self.error_code is not None:
            _required_text(self.error_code, "error_code")
        object.__setattr__(self, "payload", _freeze_json(self.payload))
        if not _SHA256_PATTERN.fullmatch(self.content_hash):
            raise EvidenceValidationError("content_hash must be a lowercase SHA-256 digest")
        if self.content_hash != self.calculate_content_hash():
            raise EvidenceValidationError("observation content_hash does not match its content")

    @classmethod
    def create(
        cls,
        *,
        observation_id: str,
        analysis_id: str,
        attempt_id: str,
        kind: str,
        subject_uri: str,
        observed_at: datetime,
        probe_id: str,
        tool_name: str,
        tool_version: str,
        payload_schema_version: str,
        payload: Mapping[str, Any],
        error_code: str | None = None,
        truncated: bool = False,
    ) -> Observation:
        frozen_payload = _freeze_json(payload)
        values = {
            "observation_id": observation_id,
            "analysis_id": analysis_id,
            "attempt_id": attempt_id,
            "kind": kind,
            "subject_uri": subject_uri,
            "observed_at": _canonical_datetime(_aware_datetime(observed_at, "observed_at")),
            "probe_id": probe_id,
            "tool_name": tool_name,
            "tool_version": tool_version,
            "payload_schema_version": payload_schema_version,
            "payload": _plain_json(frozen_payload),
            "error_code": error_code,
            "truncated": truncated,
        }
        return cls(
            observation_id=observation_id,
            analysis_id=analysis_id,
            attempt_id=attempt_id,
            kind=kind,
            subject_uri=subject_uri,
            observed_at=observed_at,
            probe_id=probe_id,
            tool_name=tool_name,
            tool_version=tool_version,
            payload_schema_version=payload_schema_version,
            payload=frozen_payload,
            content_hash=_content_hash(values),
            error_code=error_code,
            truncated=truncated,
        )

    def _hashable_content(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "analysis_id": self.analysis_id,
            "attempt_id": self.attempt_id,
            "kind": self.kind,
            "subject_uri": self.subject_uri,
            "observed_at": _canonical_datetime(self.observed_at),
            "probe_id": self.probe_id,
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "payload_schema_version": self.payload_schema_version,
            "payload": _plain_json(self.payload),
            "error_code": self.error_code,
            "truncated": self.truncated,
        }

    def calculate_content_hash(self) -> str:
        return _content_hash(self._hashable_content())

    def to_dict(self) -> dict[str, Any]:
        return {**self._hashable_content(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    observation_id: str
    content_hash: str
    probe_id: str
    tool_name: str
    tool_version: str
    observed_at: datetime

    def __post_init__(self) -> None:
        for field in ("observation_id", "probe_id", "tool_name", "tool_version"):
            _required_text(getattr(self, field), field)
        if not _SHA256_PATTERN.fullmatch(self.content_hash):
            raise EvidenceValidationError("source content_hash must be a SHA-256 digest")
        _aware_datetime(self.observed_at, "observed_at")

    def to_dict(self) -> dict[str, str]:
        return {
            "observation_id": self.observation_id,
            "content_hash": self.content_hash,
            "probe_id": self.probe_id,
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "observed_at": _canonical_datetime(self.observed_at),
        }


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    analysis_id: str
    indicator_id: str
    kind: str
    subject_uri: str
    method_id: str
    method_version: str
    created_at: datetime
    confidence: float
    sources: tuple[EvidenceSource, ...]
    data: Mapping[str, Any]
    summary: str
    content_hash: str

    def __post_init__(self) -> None:
        for field in (
            "evidence_id",
            "analysis_id",
            "indicator_id",
            "kind",
            "method_id",
            "method_version",
        ):
            _required_text(getattr(self, field), field)
        _subject_uri(self.subject_uri)
        _aware_datetime(self.created_at, "created_at")
        if not 0 <= self.confidence <= 1:
            raise EvidenceValidationError("confidence must be between 0 and 1")
        _required_text(self.summary, "summary", maximum=2000)
        if not self.sources:
            raise EvidenceValidationError("evidence must reference at least one observation")
        if len({source.observation_id for source in self.sources}) != len(self.sources):
            raise EvidenceValidationError("evidence sources must be unique")
        object.__setattr__(self, "data", _freeze_json(self.data, "data"))
        if not _SHA256_PATTERN.fullmatch(self.content_hash):
            raise EvidenceValidationError("content_hash must be a lowercase SHA-256 digest")
        if self.content_hash != self.calculate_content_hash():
            raise EvidenceValidationError("evidence content_hash does not match its content")

    @classmethod
    def create(
        cls,
        *,
        evidence_id: str,
        analysis_id: str,
        indicator_id: str,
        kind: str,
        subject_uri: str,
        method_id: str,
        method_version: str,
        created_at: datetime,
        confidence: float,
        observations: Sequence[Observation],
        data: Mapping[str, Any],
        summary: str,
    ) -> Evidence:
        if not observations:
            raise EvidenceValidationError("evidence must reference at least one observation")
        if any(item.analysis_id != analysis_id for item in observations):
            raise EvidenceValidationError("all observations must belong to the evidence analysis")
        if any(item.subject_uri != subject_uri for item in observations):
            raise EvidenceValidationError("all observations must target the evidence subject")
        sources = tuple(
            EvidenceSource(
                observation_id=item.observation_id,
                content_hash=item.content_hash,
                probe_id=item.probe_id,
                tool_name=item.tool_name,
                tool_version=item.tool_version,
                observed_at=item.observed_at,
            )
            for item in observations
        )
        frozen_data = _freeze_json(data, "data")
        hashable = {
            "evidence_id": evidence_id,
            "analysis_id": analysis_id,
            "indicator_id": indicator_id,
            "kind": kind,
            "subject_uri": subject_uri,
            "method_id": method_id,
            "method_version": method_version,
            "created_at": _canonical_datetime(_aware_datetime(created_at, "created_at")),
            "confidence": confidence,
            "sources": [item.to_dict() for item in sources],
            "data": _plain_json(frozen_data),
            "summary": summary,
        }
        return cls(
            evidence_id=evidence_id,
            analysis_id=analysis_id,
            indicator_id=indicator_id,
            kind=kind,
            subject_uri=subject_uri,
            method_id=method_id,
            method_version=method_version,
            created_at=created_at,
            confidence=confidence,
            sources=sources,
            data=frozen_data,
            summary=summary,
            content_hash=_content_hash(hashable),
        )

    def _hashable_content(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "analysis_id": self.analysis_id,
            "indicator_id": self.indicator_id,
            "kind": self.kind,
            "subject_uri": self.subject_uri,
            "method_id": self.method_id,
            "method_version": self.method_version,
            "created_at": _canonical_datetime(self.created_at),
            "confidence": self.confidence,
            "sources": [item.to_dict() for item in self.sources],
            "data": _plain_json(self.data),
            "summary": self.summary,
        }

    def calculate_content_hash(self) -> str:
        return _content_hash(self._hashable_content())

    def to_dict(self) -> dict[str, Any]:
        return {**self._hashable_content(), "content_hash": self.content_hash}
