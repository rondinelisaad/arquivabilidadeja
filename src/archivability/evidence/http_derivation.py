from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from archivability.evidence.derivation import derive_indicator_result
from archivability.evidence.models import Evidence, EvidenceValidationError, Observation
from archivability.methodology.models import IndicatorResult, MethodologyConfig, ResultState


_METHOD_VERSION = "1.0.0"
_EXPECTED_PAYLOAD_KEYS = frozenset(
    {
        "status_code",
        "headers",
        "response_bytes_observed",
        "response_byte_limit",
        "response_truncated",
        "redirect_count",
        "final_transport_secure",
    }
)
_CONTENT_LENGTH_PATTERN = re.compile(r"^[0-9]{1,20}$")
_MAX_CONTENT_LENGTH = (1 << 63) - 1
_ALLOWED_HEADERS = frozenset(
    {
        "accept-ranges",
        "age",
        "cache-control",
        "content-encoding",
        "content-language",
        "content-length",
        "content-type",
        "date",
        "etag",
        "expires",
        "last-modified",
        "link",
        "server",
        "vary",
    }
)


class HttpMetadataDerivationError(EvidenceValidationError):
    """Raised when HTTP metadata cannot support deterministic derivation."""


@dataclass(frozen=True, slots=True)
class HttpMetadataDerivation:
    evidence: tuple[Evidence, ...]
    indicator_results: tuple[IndicatorResult, ...]

    def __post_init__(self) -> None:
        evidence_ids = {item.evidence_id for item in self.evidence}
        indicator_ids = {item.indicator_id for item in self.indicator_results}
        evidence_by_id = {item.evidence_id: item for item in self.evidence}
        referenced = {
            evidence_id
            for result in self.indicator_results
            for evidence_id in result.evidence_ids
        }
        if not self.evidence or len(evidence_ids) != len(self.evidence):
            raise HttpMetadataDerivationError("derived evidence must be non-empty and unique")
        if (
            not self.indicator_results
            or len(self.indicator_results) != len(self.evidence)
            or len(indicator_ids) != len(self.indicator_results)
            or referenced != evidence_ids
        ):
            raise HttpMetadataDerivationError(
                "indicator results must reference all and only the derived evidence"
            )
        if any(
            len(result.evidence_ids) != 1
            or evidence_by_id[result.evidence_ids[0]].indicator_id != result.indicator_id
            for result in self.indicator_results
        ):
            raise HttpMetadataDerivationError(
                "each result must reference evidence for the same indicator"
            )


def derive_http_metadata_indicators(
    methodology: MethodologyConfig,
    observation: Observation,
) -> HttpMetadataDerivation:
    """Derive the D01 and R06 candidates from one validated HTTP observation."""
    payload = _validate_payload(observation)
    status_code = payload["status_code"]
    headers = payload["headers"]

    accessibility_state = _accessibility_state(status_code)
    accessibility = _evidence(
        observation,
        indicator_id="D01",
        method_id="http-homepage-accessibility",
        data={
            "attempt_count": 1,
            "status_code": status_code,
            "status_class": status_code // 100,
            "redirect_count": payload["redirect_count"],
            "final_transport_secure": payload["final_transport_secure"],
        },
        summary="Bounded homepage retrieval outcome derived from one HTTP attempt.",
    )

    content_length, content_length_status = _content_length(headers)
    completeness_state, termination_reason = _completeness_state(
        truncated=payload["response_truncated"],
        observed_bytes=payload["response_bytes_observed"],
        content_length=content_length,
        content_length_status=content_length_status,
    )
    completeness = _evidence(
        observation,
        indicator_id="R06",
        method_id="http-response-completeness",
        data={
            "bytes_observed": payload["response_bytes_observed"],
            "content_length": content_length,
            "content_length_status": content_length_status,
            "content_length_matches_observed": (
                content_length == payload["response_bytes_observed"]
                if content_length is not None
                else None
            ),
            "termination_reason": termination_reason,
            "limits": {"response_byte_limit": payload["response_byte_limit"]},
        },
        summary="Response completeness derived from bounded transfer metadata.",
    )

    evidence = (accessibility, completeness)
    results = (
        derive_indicator_result(
            methodology,
            indicator_id="D01",
            state=accessibility_state,
            evidence=(accessibility,),
        ),
        derive_indicator_result(
            methodology,
            indicator_id="R06",
            state=completeness_state,
            evidence=(completeness,),
        ),
    )
    return HttpMetadataDerivation(evidence=evidence, indicator_results=results)


def _validate_payload(observation: Observation) -> Mapping[str, Any]:
    if observation.kind != "http_metadata" or observation.payload_schema_version != "1.1":
        raise HttpMetadataDerivationError(
            "derivation requires an HTTP metadata observation with schema version 1.1"
        )
    payload = observation.payload
    if set(payload) != _EXPECTED_PAYLOAD_KEYS:
        raise HttpMetadataDerivationError("HTTP metadata payload fields are invalid")
    _integer(payload["status_code"], "status_code", 100, 599)
    _integer(
        payload["response_bytes_observed"],
        "response_bytes_observed",
        0,
        10 * 1024 * 1024,
    )
    _integer(
        payload["response_byte_limit"],
        "response_byte_limit",
        1,
        10 * 1024 * 1024,
    )
    _integer(payload["redirect_count"], "redirect_count", 0, 5)
    if payload["response_bytes_observed"] > payload["response_byte_limit"]:
        raise HttpMetadataDerivationError("observed bytes exceed the declared response limit")
    for field in ("response_truncated", "final_transport_secure"):
        if not isinstance(payload[field], bool):
            raise HttpMetadataDerivationError(f"{field} must be boolean")
    if observation.truncated is not payload["response_truncated"]:
        raise HttpMetadataDerivationError("observation truncation flags do not match")
    if (
        payload["response_truncated"]
        and payload["response_bytes_observed"] != payload["response_byte_limit"]
    ):
        raise HttpMetadataDerivationError(
            "truncated response must reach the declared response limit"
        )
    headers = payload["headers"]
    if not isinstance(headers, Mapping):
        raise HttpMetadataDerivationError("headers must be an object")
    for name, values in headers.items():
        if not isinstance(name, str) or name not in _ALLOWED_HEADERS:
            raise HttpMetadataDerivationError("header name is not allowlisted")
        if not isinstance(values, tuple) or any(
            not isinstance(value, str) or len(value) > 8192 for value in values
        ):
            raise HttpMetadataDerivationError("header values must be immutable strings")
    return payload


def _integer(value: Any, field: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise HttpMetadataDerivationError(
            f"{field} must be an integer between {minimum} and {maximum}"
        )


def _accessibility_state(status_code: int) -> ResultState:
    if 200 <= status_code <= 299:
        return ResultState.PASS
    if 300 <= status_code <= 399:
        return ResultState.WARNING
    if 400 <= status_code <= 599:
        return ResultState.FAIL
    return ResultState.UNKNOWN


def _content_length(headers: Mapping[str, Any]) -> tuple[int | None, str]:
    raw_values = headers.get("content-length", ())
    if not raw_values:
        return None, "absent"
    tokens = [token.strip() for value in raw_values for token in value.split(",")]
    if not tokens or any(not _CONTENT_LENGTH_PATTERN.fullmatch(token) for token in tokens):
        return None, "invalid"
    values = {int(token) for token in tokens}
    if any(value > _MAX_CONTENT_LENGTH for value in values):
        return None, "invalid"
    if len(values) != 1:
        return None, "conflicting"
    return next(iter(values)), "valid"


def _completeness_state(
    *,
    truncated: bool,
    observed_bytes: int,
    content_length: int | None,
    content_length_status: str,
) -> tuple[ResultState, str]:
    if truncated:
        return ResultState.WARNING, "response_byte_limit"
    if content_length_status in {"invalid", "conflicting"}:
        return ResultState.WARNING, "invalid_content_length"
    if content_length is not None and content_length != observed_bytes:
        return ResultState.WARNING, "content_length_mismatch"
    return ResultState.PASS, "complete"


def _evidence(
    observation: Observation,
    *,
    indicator_id: str,
    method_id: str,
    data: Mapping[str, Any],
    summary: str,
) -> Evidence:
    return Evidence.create(
        evidence_id=(
            f"http-metadata-{indicator_id.lower()}-v1-0-0-{observation.content_hash}"
        ),
        analysis_id=observation.analysis_id,
        indicator_id=indicator_id,
        kind="http_indicator_measurement",
        subject_uri=observation.subject_uri,
        method_id=method_id,
        method_version=_METHOD_VERSION,
        created_at=observation.observed_at,
        confidence=1.0,
        observations=(observation,),
        data=data,
        summary=summary,
    )
