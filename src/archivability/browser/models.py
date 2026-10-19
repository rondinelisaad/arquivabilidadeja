from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_URI_PATTERN = re.compile(r"[\x00-\x20\x7f\\]")


class BrowserContractError(ValueError):
    """Raised when a browser-worker contract is unsafe or inconsistent."""


def _integer(value: int, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise BrowserContractError(f"{field} must be between {minimum} and {maximum}")
    return value


def _identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise BrowserContractError(f"{field} is invalid")
    return value


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BrowserContractError(f"{field} must include a timezone")
    return value


@dataclass(frozen=True, slots=True)
class BrowserBudgets:
    navigation_timeout_milliseconds: int = 15_000
    observation_window_milliseconds: int = 5_000
    total_timeout_milliseconds: int = 30_000
    max_requests: int = 500
    max_response_bytes: int = 5 * 1024 * 1024
    max_total_transfer_bytes: int = 50 * 1024 * 1024
    max_dom_bytes: int = 2 * 1024 * 1024
    max_console_events: int = 100

    def __post_init__(self) -> None:
        _integer(self.navigation_timeout_milliseconds, "navigation timeout", 1_000, 60_000)
        _integer(self.observation_window_milliseconds, "observation window", 0, 30_000)
        _integer(self.total_timeout_milliseconds, "total timeout", 1_000, 120_000)
        _integer(self.max_requests, "request budget", 1, 2_000)
        _integer(self.max_response_bytes, "response byte budget", 1, 20 * 1024 * 1024)
        _integer(
            self.max_total_transfer_bytes,
            "total transfer byte budget",
            1,
            200 * 1024 * 1024,
        )
        _integer(self.max_dom_bytes, "DOM byte budget", 1, 10 * 1024 * 1024)
        _integer(self.max_console_events, "console event budget", 0, 1_000)
        if self.max_response_bytes > self.max_total_transfer_bytes:
            raise BrowserContractError("response byte budget exceeds total transfer budget")
        if (
            self.navigation_timeout_milliseconds
            + self.observation_window_milliseconds
            > self.total_timeout_milliseconds
        ):
            raise BrowserContractError("browser phase budgets exceed the total timeout")

    def to_dict(self) -> dict[str, int]:
        return {
            "navigation_timeout_milliseconds": self.navigation_timeout_milliseconds,
            "observation_window_milliseconds": self.observation_window_milliseconds,
            "total_timeout_milliseconds": self.total_timeout_milliseconds,
            "max_requests": self.max_requests,
            "max_response_bytes": self.max_response_bytes,
            "max_total_transfer_bytes": self.max_total_transfer_bytes,
            "max_dom_bytes": self.max_dom_bytes,
            "max_console_events": self.max_console_events,
        }


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    profile_id: str = "chromium-baseline-1"
    viewport_width: int = 1365
    viewport_height: int = 768
    user_agent: str = "arquivabilidade-ja-browser/0.1"
    locale: str = "en-US"
    timezone_id: str = "UTC"
    color_scheme: str = "light"
    reduced_motion: str = "reduce"
    service_workers: str = "block"
    cache_enabled: bool = False
    javascript_enabled: bool = True

    def __post_init__(self) -> None:
        _identifier(self.profile_id, "browser profile_id")
        _integer(self.viewport_width, "viewport width", 320, 3840)
        _integer(self.viewport_height, "viewport height", 240, 2160)
        if self.user_agent != "arquivabilidade-ja-browser/0.1":
            raise BrowserContractError("browser User-Agent must be explicit and honest")
        if self.locale != "en-US" or self.timezone_id != "UTC":
            raise BrowserContractError("browser locale and timezone must be deterministic")
        if self.color_scheme not in {"light", "dark"}:
            raise BrowserContractError("browser color scheme is invalid")
        if self.reduced_motion != "reduce" or self.service_workers != "block":
            raise BrowserContractError("browser side effects must be constrained")
        if self.cache_enabled or not self.javascript_enabled:
            raise BrowserContractError("browser cache must be off and JavaScript on")

    def to_dict(self) -> dict[str, str | int | bool]:
        return {
            "profile_id": self.profile_id,
            "viewport_width": self.viewport_width,
            "viewport_height": self.viewport_height,
            "user_agent": self.user_agent,
            "locale": self.locale,
            "timezone_id": self.timezone_id,
            "color_scheme": self.color_scheme,
            "reduced_motion": self.reduced_motion,
            "service_workers": self.service_workers,
            "cache_enabled": self.cache_enabled,
            "javascript_enabled": self.javascript_enabled,
        }


@dataclass(frozen=True, slots=True)
class BrowserIsolationAttestation:
    policy_id: str
    sandbox_enabled: bool
    running_as_root: bool
    read_only_root_filesystem: bool
    ephemeral_profile: bool
    host_mounts_present: bool
    secrets_available: bool
    docker_socket_available: bool

    def __post_init__(self) -> None:
        _identifier(self.policy_id, "isolation policy_id")
        boolean_fields = (
            self.sandbox_enabled,
            self.running_as_root,
            self.read_only_root_filesystem,
            self.ephemeral_profile,
            self.host_mounts_present,
            self.secrets_available,
            self.docker_socket_available,
        )
        if any(not isinstance(value, bool) for value in boolean_fields):
            raise BrowserContractError("isolation attestations must be booleans")
        if not self.sandbox_enabled or self.running_as_root:
            raise BrowserContractError("browser sandbox or user isolation is unsafe")
        if not self.read_only_root_filesystem or not self.ephemeral_profile:
            raise BrowserContractError("browser filesystem isolation is unsafe")
        if self.host_mounts_present or self.secrets_available or self.docker_socket_available:
            raise BrowserContractError("browser worker exposes privileged host resources")

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "policy_id": self.policy_id,
            "sandbox_enabled": self.sandbox_enabled,
            "running_as_root": self.running_as_root,
            "read_only_root_filesystem": self.read_only_root_filesystem,
            "ephemeral_profile": self.ephemeral_profile,
            "host_mounts_present": self.host_mounts_present,
            "secrets_available": self.secrets_available,
            "docker_socket_available": self.docker_socket_available,
        }


@dataclass(frozen=True, slots=True)
class BrowserRenderRequest:
    analysis_id: str
    attempt_id: str
    subject_uri: str
    requested_at: datetime
    budgets: BrowserBudgets = BrowserBudgets()
    profile: BrowserProfile = BrowserProfile()

    def __post_init__(self) -> None:
        for field in ("analysis_id", "attempt_id"):
            value = getattr(self, field)
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 256
                or _CONTROL_CHARACTER_PATTERN.search(value)
            ):
                raise BrowserContractError(f"{field} is invalid")
        if (
            not isinstance(self.subject_uri, str)
            or not self.subject_uri
            or len(self.subject_uri) > 2048
            or _UNSAFE_URI_PATTERN.search(self.subject_uri)
        ):
            raise BrowserContractError("browser subject URI is invalid")
        try:
            parsed = urlsplit(self.subject_uri)
            port = parsed.port
        except ValueError as exc:
            raise BrowserContractError("browser subject URI is invalid") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or port == 0
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise BrowserContractError("browser subject must be an HTTP(S) URI without credentials")
        _aware(self.requested_at, "requested_at")
        if not isinstance(self.budgets, BrowserBudgets) or not isinstance(
            self.profile, BrowserProfile
        ):
            raise BrowserContractError("browser request contracts are invalid")


@dataclass(frozen=True, slots=True)
class BrowserRenderAggregate:
    raw_dom_hash: str
    rendered_dom_hash: str
    raw_url_set_hash: str
    rendered_url_set_hash: str
    raw_url_count: int
    rendered_url_count: int
    additional_url_count: int
    request_count: int
    internal_request_count: int
    external_request_count: int
    api_request_count: int
    external_api_request_count: int
    failed_request_count: int
    blocked_request_count: int
    transfer_bytes: int
    raw_dom_bytes: int
    rendered_dom_bytes: int
    console_event_count: int
    elapsed_milliseconds: int
    termination_reason: str
    external_origin_count: int = 0
    external_script_count: int = 0
    external_stylesheet_count: int = 0
    external_image_count: int = 0
    external_font_count: int = 0
    external_frame_count: int = 0
    external_other_count: int = 0
    dependency_graph_observed: bool = False

    def __post_init__(self) -> None:
        for field in (
            "raw_dom_hash",
            "rendered_dom_hash",
            "raw_url_set_hash",
            "rendered_url_set_hash",
        ):
            if not _SHA256_PATTERN.fullmatch(getattr(self, field)):
                raise BrowserContractError(f"{field} must be a lowercase SHA-256 digest")
        for field in (
            "raw_url_count",
            "rendered_url_count",
            "additional_url_count",
            "request_count",
            "internal_request_count",
            "external_request_count",
            "api_request_count",
            "external_api_request_count",
            "failed_request_count",
            "blocked_request_count",
            "transfer_bytes",
            "raw_dom_bytes",
            "rendered_dom_bytes",
            "console_event_count",
            "elapsed_milliseconds",
            "external_origin_count",
            "external_script_count",
            "external_stylesheet_count",
            "external_image_count",
            "external_font_count",
            "external_frame_count",
            "external_other_count",
        ):
            _integer(getattr(self, field), field, 0, 1_000_000_000)
        if self.additional_url_count > self.rendered_url_count:
            raise BrowserContractError("rendered URL gain is inconsistent")
        if self.internal_request_count + self.external_request_count > self.request_count:
            raise BrowserContractError("browser request scope counts are inconsistent")
        if any(
            value > self.request_count
            for value in (
                self.api_request_count,
                self.failed_request_count,
                self.blocked_request_count,
            )
        ):
            raise BrowserContractError("browser request subtype counts are inconsistent")
        if self.external_api_request_count > min(
            self.api_request_count, self.external_request_count
        ):
            raise BrowserContractError("external API request count is inconsistent")
        categorized_external = sum(
            (
                self.external_api_request_count,
                self.external_script_count,
                self.external_stylesheet_count,
                self.external_image_count,
                self.external_font_count,
                self.external_frame_count,
                self.external_other_count,
            )
        )
        if not isinstance(self.dependency_graph_observed, bool):
            raise BrowserContractError("dependency graph observation flag is invalid")
        if (
            self.dependency_graph_observed
            and categorized_external != self.external_request_count
        ):
            raise BrowserContractError("external dependency counts are inconsistent")
        if self.external_origin_count > self.external_request_count:
            raise BrowserContractError("external origin count is inconsistent")
        if self.termination_reason not in {
            "complete",
            "request_budget",
            "byte_budget",
            "time_budget",
            "dom_budget",
        }:
            raise BrowserContractError("browser termination reason is invalid")

    def validate_against(self, budgets: BrowserBudgets) -> None:
        if self.request_count > budgets.max_requests:
            raise BrowserContractError("browser request budget was exceeded")
        if self.transfer_bytes > budgets.max_total_transfer_bytes:
            raise BrowserContractError("browser transfer budget was exceeded")
        if self.rendered_dom_bytes > budgets.max_dom_bytes:
            raise BrowserContractError("browser DOM budget was exceeded")
        if self.raw_dom_bytes > budgets.max_dom_bytes:
            raise BrowserContractError("raw DOM budget was exceeded")
        if self.console_event_count > budgets.max_console_events:
            raise BrowserContractError("browser console event budget was exceeded")
        if self.elapsed_milliseconds > budgets.total_timeout_milliseconds:
            raise BrowserContractError("browser time budget was exceeded")

    def to_dict(self) -> dict[str, str | int]:
        return {
            "raw_dom_hash": self.raw_dom_hash,
            "rendered_dom_hash": self.rendered_dom_hash,
            "raw_url_set_hash": self.raw_url_set_hash,
            "rendered_url_set_hash": self.rendered_url_set_hash,
            "raw_url_count": self.raw_url_count,
            "rendered_url_count": self.rendered_url_count,
            "additional_url_count": self.additional_url_count,
            "request_count": self.request_count,
            "internal_request_count": self.internal_request_count,
            "external_request_count": self.external_request_count,
            "api_request_count": self.api_request_count,
            "external_api_request_count": self.external_api_request_count,
            "failed_request_count": self.failed_request_count,
            "blocked_request_count": self.blocked_request_count,
            "transfer_bytes": self.transfer_bytes,
            "raw_dom_bytes": self.raw_dom_bytes,
            "rendered_dom_bytes": self.rendered_dom_bytes,
            "console_event_count": self.console_event_count,
            "elapsed_milliseconds": self.elapsed_milliseconds,
            "termination_reason": self.termination_reason,
            "external_origin_count": self.external_origin_count,
            "external_script_count": self.external_script_count,
            "external_stylesheet_count": self.external_stylesheet_count,
            "external_image_count": self.external_image_count,
            "external_font_count": self.external_font_count,
            "external_frame_count": self.external_frame_count,
            "external_other_count": self.external_other_count,
            "dependency_graph_observed": self.dependency_graph_observed,
        }


@dataclass(frozen=True, slots=True)
class BrowserRenderResult:
    analysis_id: str
    attempt_id: str
    engine_name: str
    engine_version: str
    started_at: datetime
    finished_at: datetime
    isolation: BrowserIsolationAttestation
    aggregate: BrowserRenderAggregate

    def validate_against(self, request: BrowserRenderRequest) -> None:
        if self.analysis_id != request.analysis_id or self.attempt_id != request.attempt_id:
            raise BrowserContractError("browser result does not match its request")
        _identifier(self.engine_name, "browser engine_name")
        _identifier(self.engine_version, "browser engine_version")
        started = _aware(self.started_at, "started_at")
        finished = _aware(self.finished_at, "finished_at")
        if finished < started or started < request.requested_at:
            raise BrowserContractError("browser result timestamps are inconsistent")
        if not isinstance(self.isolation, BrowserIsolationAttestation) or not isinstance(
            self.aggregate, BrowserRenderAggregate
        ):
            raise BrowserContractError("browser result contracts are invalid")
        self.aggregate.validate_against(request.budgets)

