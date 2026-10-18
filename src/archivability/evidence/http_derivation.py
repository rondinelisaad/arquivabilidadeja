from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from archivability.evidence.derivation import derive_indicator_result
from archivability.evidence.models import Evidence, EvidenceValidationError, Observation
from archivability.methodology.models import (
    IndicatorResult,
    MethodologyConfig,
    ResultState,
)

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
_DISCOVERY_PAYLOAD_KEYS = _EXPECTED_PAYLOAD_KEYS | {
    "elapsed_milliseconds",
    "content_type",
    "format_family",
    "html",
    "robots",
    "sitemap",
}
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
    """Derive deterministic HTTP and discovery indicators from one observation."""
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

    evidence: list[Evidence] = [accessibility, completeness]
    results: list[IndicatorResult] = [
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
    ]

    if observation.payload_schema_version == "1.2":
        html = payload["html"]
        robots = payload["robots"]
        sitemap = payload["sitemap"]

        def add(
            indicator_id: str,
            state: ResultState,
            method_id: str,
            data: Mapping[str, Any],
            summary: str,
        ) -> None:
            item = _evidence(
                observation,
                indicator_id=indicator_id,
                method_id=method_id,
                data=data,
                summary=summary,
            )
            evidence.append(item)
            results.append(
                derive_indicator_result(
                    methodology,
                    indicator_id=indicator_id,
                    state=state,
                    evidence=(item,),
                )
            )

        add(
            "D02",
            _auxiliary_state(robots),
            "robots-availability-parseability",
            {
                "outcome": robots["outcome"],
                "http_status": robots["status_code"],
                "parseable": robots["parseable"],
                "directive_count": robots["item_count"],
                "parser_version": "robots-lines-1",
            },
            "Bounded robots.txt availability and syntax outcome.",
        )
        add(
            "D04",
            _auxiliary_state(sitemap, require_items=True),
            "sitemap-conventional-discovery",
            {
                "searched_locations": 1,
                "outcome": sitemap["outcome"],
                "http_status": sitemap["status_code"],
                "parseable": sitemap["parseable"],
                "declared_url_count": sitemap["item_count"],
            },
            "Conventional sitemap discovery with bounded XML parsing.",
        )
        add(
            "D05",
            ResultState.PASS if html["feed_link_count"] else ResultState.NOT_APPLICABLE,
            "html-feed-discovery",
            {
                "feed_count": html["feed_link_count"],
                "discovery_source": "homepage_link_elements",
                "parse_result": html["parseable"],
            },
            "RSS and Atom declarations discovered in bounded homepage HTML.",
        )
        link_state = (
            ResultState.UNKNOWN
            if html["parseable"] is None
            else ResultState.PASS
            if html["link_count"] > 0
            else ResultState.WARNING
        )
        add(
            "D06",
            link_state,
            "initial-html-link-yield",
            {
                "url_counts_by_type": {
                    "internal": html["internal_link_count"],
                    "external": html["external_link_count"],
                },
                "total": html["link_count"],
                "truncated": html["truncated"],
                "invalid_references": None,
            },
            "Aggregate URL yield from raw homepage HTML without persisting URLs.",
        )
        add(
            "R02",
            ResultState.PASS,
            "bounded-redirect-integrity",
            {
                "redirect_count": payload["redirect_count"],
                "termination_reason": "response",
                "validated_destinations": payload["redirect_count"] + 1,
            },
            "Redirect chain terminated within validated destinations and budget.",
        )
        add(
            "R04",
            accessibility_state,
            "http-status-distribution",
            {
                "status_distribution": {str(status_code): 1},
                "resource_classes": {"homepage": 1},
                "attempt_count": 1,
            },
            "HTTP status distribution for the bounded homepage sample.",
        )
        content_type = payload["content_type"]
        add(
            "R05",
            ResultState.PASS if content_type else ResultState.WARNING,
            "content-type-consistency",
            {
                "declared_type": content_type,
                "detected_type": payload["format_family"],
                "detection_confidence": "declared_only",
                "parser_result": html["parseable"],
            },
            "Declared MIME is compared with the bounded parser outcome.",
        )
        html_state = (
            ResultState.NOT_APPLICABLE
            if content_type != "text/html"
            else ResultState.PASS
            if html["parseable"] is True
            else ResultState.WARNING
        )
        add(
            "S01",
            html_state,
            "tolerant-html-parseability",
            {
                "parser_version": "stdlib-htmlparser",
                "parse_errors": None,
                "dom_created": html["parseable"],
                "validator_result": "not_run",
            },
            "Tolerant parser outcome for bounded raw homepage HTML.",
        )
        add(
            "S03",
            _auxiliary_state(sitemap),
            "safe-sitemap-xml-parseability",
            {
                "resource_type": "sitemap",
                "parse_result": sitemap["parseable"],
                "urls_extracted": sitemap["item_count"],
                "parser_version": "stdlib-elementtree-no-doctype-1",
            },
            "Sitemap XML parsed with DTD and entity declarations rejected.",
        )
        add(
            "S04",
            ResultState.CONTEXT,
            "homepage-format-inventory",
            {
                "format_counts": {payload["format_family"]: 1},
                "byte_counts": {payload["format_family"]: payload["response_bytes_observed"]},
                "identification_confidence": "declared_mime",
            },
            "Context-only format inventory for the homepage response.",
        )
        add(
            "N01",
            ResultState.CONTEXT,
            "http-total-elapsed-time",
            {
                "total_milliseconds": payload["elapsed_milliseconds"],
                "dns_milliseconds": None,
                "tcp_milliseconds": None,
                "tls_milliseconds": None,
                "ttfb_milliseconds": None,
                "sample_size": 1,
            },
            "Context-only total elapsed time for one bounded HTTP attempt.",
        )

    return HttpMetadataDerivation(
        evidence=tuple(evidence), indicator_results=tuple(results)
    )


def _validate_payload(observation: Observation) -> Mapping[str, Any]:
    if observation.kind != "http_metadata" or observation.payload_schema_version not in {"1.1", "1.2"}:
        raise HttpMetadataDerivationError(
            "derivation requires an HTTP metadata observation with schema version 1.1 or 1.2"
        )
    payload = observation.payload
    expected = _DISCOVERY_PAYLOAD_KEYS if observation.payload_schema_version == "1.2" else _EXPECTED_PAYLOAD_KEYS
    if set(payload) != expected:
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
    if observation.payload_schema_version == "1.1" and observation.truncated is not payload["response_truncated"]:
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
    if observation.payload_schema_version == "1.2":
        _validate_discovery_payload(payload)
    return payload


def _validate_discovery_payload(payload: Mapping[str, Any]) -> None:
    elapsed = payload["elapsed_milliseconds"]
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not 0 <= elapsed <= 120_000:
        raise HttpMetadataDerivationError("elapsed_milliseconds is invalid")
    if payload["content_type"] is not None and not isinstance(payload["content_type"], str):
        raise HttpMetadataDerivationError("content_type is invalid")
    if payload["format_family"] not in {
        "html", "xml", "css", "javascript", "image", "pdf", "audio",
        "video", "font", "other", "unknown",
    }:
        raise HttpMetadataDerivationError("format_family is invalid")
    html = payload["html"]
    if not isinstance(html, Mapping) or set(html) != {
        "parseable", "link_count", "internal_link_count", "external_link_count",
        "feed_link_count", "canonical_present", "truncated",
    }:
        raise HttpMetadataDerivationError("HTML discovery summary is invalid")
    for field in ("link_count", "internal_link_count", "external_link_count", "feed_link_count"):
        _integer(html[field], field, 0, 1000)
    if html["internal_link_count"] + html["external_link_count"] != html["link_count"]:
        raise HttpMetadataDerivationError("HTML link counts are inconsistent")
    if html["parseable"] not in {True, False, None} or not isinstance(html["canonical_present"], bool) or not isinstance(html["truncated"], bool):
        raise HttpMetadataDerivationError("HTML discovery flags are invalid")
    for name in ("robots", "sitemap"):
        value = payload[name]
        if not isinstance(value, Mapping) or set(value) != {
            "outcome", "status_code", "parseable", "item_count", "truncated",
            "elapsed_milliseconds",
        }:
            raise HttpMetadataDerivationError(f"{name} summary is invalid")
        if value["outcome"] not in {"available", "http_error", "unavailable"}:
            raise HttpMetadataDerivationError(f"{name} outcome is invalid")
        if value["status_code"] is not None:
            _integer(value["status_code"], f"{name}.status_code", 100, 599)
        _integer(value["item_count"], f"{name}.item_count", 0, 1000)
        if value["parseable"] not in {True, False, None} or not isinstance(value["truncated"], bool):
            raise HttpMetadataDerivationError(f"{name} flags are invalid")


def _auxiliary_state(
    value: Mapping[str, Any], *, require_items: bool = False
) -> ResultState:
    if value["outcome"] == "unavailable":
        return ResultState.UNKNOWN
    if value["outcome"] == "http_error":
        return ResultState.WARNING
    if value["truncated"] or value["parseable"] is not True:
        return ResultState.WARNING
    if require_items and value["item_count"] == 0:
        return ResultState.WARNING
    return ResultState.PASS


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
    method_version = (
        "1.1.0" if observation.payload_schema_version == "1.2" else "1.0.0"
    )
    identifier_version = method_version.replace(".", "-")
    return Evidence.create(
        evidence_id=(
            f"http-metadata-{indicator_id.lower()}-v{identifier_version}-{observation.content_hash}"
        ),
        analysis_id=observation.analysis_id,
        indicator_id=indicator_id,
        kind="http_indicator_measurement",
        subject_uri=observation.subject_uri,
        method_id=method_id,
        method_version=method_version,
        created_at=observation.observed_at,
        confidence=1.0,
        observations=(observation,),
        data=data,
        summary=summary,
    )
