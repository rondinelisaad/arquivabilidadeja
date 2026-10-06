from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from archivability.evidence.models import Observation

if TYPE_CHECKING:
    from archivability.probes.ports import AddressResolver
    from archivability.probes.security import SsrfPolicy


class ProbeValidationError(ValueError):
    """Raised when a probe request or its approved network target is unsafe."""


def _identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ProbeValidationError(
            f"{field} must be a non-empty string of at most 256 characters"
        )


def _aware(value: datetime, field: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProbeValidationError(f"{field} must include a timezone")


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    analysis_id: str
    attempt_id: str
    subject_uri: str
    requested_at: datetime
    timeout_seconds: float = 10.0
    max_response_bytes: int = 5 * 1024 * 1024
    max_redirects: int = 3

    def __post_init__(self) -> None:
        _identifier(self.analysis_id, "analysis_id")
        _identifier(self.attempt_id, "attempt_id")
        if not isinstance(self.subject_uri, str) or not self.subject_uri:
            raise ProbeValidationError("subject_uri must be a non-empty string")
        _aware(self.requested_at, "requested_at")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ) or not (
            0.1 <= self.timeout_seconds <= 30
        ):
            raise ProbeValidationError("timeout_seconds must be between 0.1 and 30")
        if isinstance(self.max_response_bytes, bool) or not isinstance(
            self.max_response_bytes, int
        ) or not (
            1 <= self.max_response_bytes <= 10 * 1024 * 1024
        ):
            raise ProbeValidationError(
                "max_response_bytes must be between 1 byte and 10 MiB"
            )
        if (
            isinstance(self.max_redirects, bool)
            or not isinstance(self.max_redirects, int)
            or not 0 <= self.max_redirects <= 5
        ):
            raise ProbeValidationError("max_redirects must be between 0 and 5")

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_id": self.analysis_id,
            "attempt_id": self.attempt_id,
            "subject_uri": self.subject_uri,
            "requested_at": self.requested_at.isoformat(),
            "timeout_seconds": self.timeout_seconds,
            "max_response_bytes": self.max_response_bytes,
            "max_redirects": self.max_redirects,
        }


@dataclass(frozen=True, slots=True)
class ApprovedTarget:
    normalized_uri: str
    scheme: str
    hostname: str
    port: int
    addresses: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.normalized_uri:
            raise ProbeValidationError("normalized_uri must not be empty")
        if self.scheme not in {"http", "https"}:
            raise ProbeValidationError("approved scheme must be HTTP(S)")
        _identifier(self.hostname, "hostname")
        if not 1 <= self.port <= 65535:
            raise ProbeValidationError("port must be between 1 and 65535")
        try:
            parsed = urlsplit(self.normalized_uri)
            parsed_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise ProbeValidationError("normalized_uri is invalid") from exc
        if (
            parsed.scheme != self.scheme
            or parsed.hostname != self.hostname
            or parsed_port != self.port
            or parsed.username is not None
            or parsed.password is not None
            or bool(parsed.fragment)
        ):
            raise ProbeValidationError(
                "normalized_uri does not match the approved target authority"
            )
        if not isinstance(self.addresses, tuple):
            raise ProbeValidationError("approved addresses must be a tuple")
        if not self.addresses or len(set(self.addresses)) != len(self.addresses):
            raise ProbeValidationError("approved addresses must be non-empty and unique")
        try:
            parsed_addresses = tuple(ipaddress.ip_address(value) for value in self.addresses)
        except ValueError as exc:
            raise ProbeValidationError("approved target contains an invalid IP") from exc
        if any(not value.is_global for value in parsed_addresses):
            raise ProbeValidationError("approved target contains a non-global IP")

    def to_dict(self) -> dict[str, Any]:
        return {
            "normalized_uri": self.normalized_uri,
            "scheme": self.scheme,
            "hostname": self.hostname,
            "port": self.port,
            "addresses": list(self.addresses),
        }


@dataclass(frozen=True, slots=True)
class ProbeContext:
    request: ProbeRequest
    target: ApprovedTarget
    policy: SsrfPolicy
    resolver: AddressResolver
    redirect_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.redirect_count, int) or not (
            0 <= self.redirect_count <= self.request.max_redirects
        ):
            raise ProbeValidationError("redirect_count is outside the request limit")

    def redirect(self, location: str) -> ProbeContext:
        if self.redirect_count >= self.request.max_redirects:
            raise ProbeValidationError("redirect limit has been reached")
        target = self.policy.approve_redirect(self.target, location, self.resolver)
        return ProbeContext(
            request=self.request,
            target=target,
            policy=self.policy,
            resolver=self.resolver,
            redirect_count=self.redirect_count + 1,
        )


@dataclass(frozen=True, slots=True)
class ProbeRunResult:
    target: ApprovedTarget
    observations: tuple[Observation, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.observations, tuple) or not self.observations:
            raise ProbeValidationError("probe must produce at least one observation")
