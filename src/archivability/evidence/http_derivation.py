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
_CRAWL_PAYLOAD_KEYS = frozenset({"limits", "robots", "summary"})
_CRAWL_LIMIT_KEYS = frozenset(
    {
        "max_depth",
        "max_urls",
        "max_response_bytes",
        "max_total_bytes",
        "max_total_elapsed_milliseconds",
        "max_links_per_document",
    }
)
_CRAWL_ROBOTS_KEYS = frozenset(
    {
        "disposition",
        "matched_rule",
        "matched_octets",
        "matched_group_count",
        "applicable_rule_count",
        "policy_id",
        "parser_version",
        "product_token",
        "source_state",
    }
)
_CRAWL_SUMMARY_KEYS = frozenset(
    {
        "discovered_references",
        "discovered_unique_internal",
        "enqueued",
        "visited",
        "successful",
        "broken_internal",
        "edge_count",
        "maximum_depth_reached",
        "response_bytes",
        "elapsed_milliseconds",
        "status_classes",
        "failures",
        "discarded",
        "termination_reason",
    }
)
_CRAWL_FAILURE_CLASSES = frozenset(
    {"dns", "network", "policy", "timeout", "tls", "tool"}
)
_CRAWL_DISCARD_REASONS = frozenset(
    {
        "byte_budget",
        "depth_limit",
        "duplicate",
        "external_origin",
        "invalid",
        "robots_policy",
        "time_budget",
        "url_budget",
    }
)
_ROBOTS_PRODUCT_TOKEN = re.compile(r"^[A-Za-z_-]+$")


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
    *,
    crawl_observation: Observation | None = None,
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

    if crawl_observation is not None:
        crawl_derivation = _derive_bounded_crawl_indicators(
            methodology,
            observation,
            crawl_observation,
        )
        evidence.extend(crawl_derivation.evidence)
        results.extend(crawl_derivation.indicator_results)

    return HttpMetadataDerivation(
        evidence=tuple(evidence), indicator_results=tuple(results)
    )


def _derive_bounded_crawl_indicators(
    methodology: MethodologyConfig,
    http_observation: Observation,
    crawl_observation: Observation,
) -> HttpMetadataDerivation:
    payload = _validate_crawl_payload(http_observation, crawl_observation)
    limits = payload["limits"]
    robots = payload["robots"]
    summary = payload["summary"]
    discarded = summary["discarded"]

    if robots["source_state"] == "unreachable":
        robots_state = ResultState.UNKNOWN
    elif robots["source_state"] == "unavailable":
        robots_state = ResultState.NOT_APPLICABLE
    elif robots["disposition"] == "disallow":
        robots_state = ResultState.FAIL
    elif discarded.get("robots_policy", 0):
        robots_state = ResultState.WARNING
    else:
        robots_state = ResultState.PASS

    checked_internal = max(0, summary["visited"] - 1)
    broken_internal = summary["broken_internal"]
    if checked_internal == 0:
        broken_state = ResultState.NOT_APPLICABLE
    elif broken_internal == 0:
        broken_state = ResultState.PASS
    elif broken_internal == checked_internal:
        broken_state = ResultState.FAIL
    else:
        broken_state = ResultState.WARNING

    items = (
        (
            "D03",
            robots_state,
            "bounded-robots-scope-policy",
            {
                "user_agent": robots["product_token"],
                "scope": "same_origin_bounded",
                "decision": robots["disposition"],
                "source_state": robots["source_state"],
                "matched_rules": {
                    "kind": robots["matched_rule"],
                    "matched_octets": robots["matched_octets"],
                    "applicable_count": robots["applicable_rule_count"],
                    "group_count": robots["matched_group_count"],
                },
                "discarded_by_policy": discarded.get("robots_policy", 0),
                "policy_id": robots["policy_id"],
                "parser_version": robots["parser_version"],
            },
            "Applicable robots policy evaluated for the bounded same-origin crawl.",
        ),
        (
            "D08",
            (
                ResultState.PASS
                if summary["termination_reason"] == "complete"
                else ResultState.WARNING
            ),
            "bounded-crawl-frontier-coverage",
            {
                "scope": "same_origin",
                "budgets": dict(limits),
                "frontier_counts": {
                    "discovered_references": summary["discovered_references"],
                    "discovered_unique_internal": summary[
                        "discovered_unique_internal"
                    ],
                    "enqueued": summary["enqueued"],
                    "visited": summary["visited"],
                    "edge_count": summary["edge_count"],
                    "maximum_depth_reached": summary["maximum_depth_reached"],
                },
                "discard_reasons": dict(discarded),
                "termination_reason": summary["termination_reason"],
            },
            "Operational frontier coverage within explicit crawl budgets.",
        ),
        (
            "R03",
            broken_state,
            "bounded-internal-link-retrieval",
            {
                "denominator": checked_internal,
                "broken_count": broken_internal,
                "failure_classes": dict(summary["failures"]),
                "status_classes": dict(summary["status_classes"]),
                "sample_strategy": "bounded_same_origin_frontier",
            },
            "Broken internal links classified within the bounded crawl sample.",
        ),
    )
    evidence = tuple(
        _crawl_evidence(
            crawl_observation,
            indicator_id=indicator_id,
            method_id=method_id,
            data=data,
            summary=evidence_summary,
        )
        for indicator_id, _state, method_id, data, evidence_summary in items
    )
    results = tuple(
        derive_indicator_result(
            methodology,
            indicator_id=indicator_id,
            state=state,
            evidence=(item,),
        )
        for (indicator_id, state, _method_id, _data, _summary), item in zip(
            items, evidence, strict=True
        )
    )
    return HttpMetadataDerivation(evidence=evidence, indicator_results=results)


def _validate_crawl_payload(
    http_observation: Observation,
    crawl_observation: Observation,
) -> Mapping[str, Any]:
    if http_observation.payload_schema_version != "1.2":
        raise HttpMetadataDerivationError(
            "bounded crawl derivation requires HTTP metadata schema version 1.2"
        )
    payload = validate_bounded_crawl_observation(crawl_observation)
    if any(
        getattr(crawl_observation, field) != getattr(http_observation, field)
        for field in (
            "analysis_id",
            "attempt_id",
            "subject_uri",
            "probe_id",
            "tool_name",
            "tool_version",
        )
    ):
        raise HttpMetadataDerivationError(
            "HTTP metadata and bounded crawl observations do not share provenance"
        )
    return payload


def validate_bounded_crawl_observation(
    crawl_observation: Observation,
) -> Mapping[str, Any]:
    """Validate and return the URL-free aggregate payload of a crawl observation."""
    if (
        crawl_observation.kind != "bounded_crawl"
        or crawl_observation.payload_schema_version != "1.0"
        or crawl_observation.error_code is not None
    ):
        raise HttpMetadataDerivationError(
            "crawl derivation requires a successful bounded crawl observation version 1.0"
        )
    payload = crawl_observation.payload
    _mapping_keys(payload, _CRAWL_PAYLOAD_KEYS, "bounded crawl payload")
    limits = payload["limits"]
    robots = payload["robots"]
    summary = payload["summary"]
    _mapping_keys(limits, _CRAWL_LIMIT_KEYS, "bounded crawl limits")
    _mapping_keys(robots, _CRAWL_ROBOTS_KEYS, "bounded crawl robots decision")
    _mapping_keys(summary, _CRAWL_SUMMARY_KEYS, "bounded crawl summary")

    _integer(limits["max_depth"], "max_depth", 0, 5)
    _integer(limits["max_urls"], "max_urls", 1, 100)
    _integer(limits["max_response_bytes"], "max_response_bytes", 1, 10 * 1024 * 1024)
    _integer(limits["max_total_bytes"], "max_total_bytes", 1, 100 * 1024 * 1024)
    _integer(limits["max_links_per_document"], "max_links_per_document", 1, 10_000)
    _number(
        limits["max_total_elapsed_milliseconds"],
        "max_total_elapsed_milliseconds",
        100,
        300_000,
    )
    if limits["max_response_bytes"] > limits["max_total_bytes"]:
        raise HttpMetadataDerivationError(
            "crawl response byte limit exceeds its total byte limit"
        )

    if robots["disposition"] not in {"allow", "disallow"}:
        raise HttpMetadataDerivationError("robots disposition is invalid")
    if robots["matched_rule"] not in {None, "allow", "disallow"}:
        raise HttpMetadataDerivationError("robots matched rule is invalid")
    for field in (
        "matched_octets",
        "matched_group_count",
        "applicable_rule_count",
    ):
        _integer(robots[field], field, 0, 1_000_000)
    for field in ("policy_id", "parser_version"):
        _printable_identifier(robots[field], field)
    if (
        not isinstance(robots["product_token"], str)
        or not _ROBOTS_PRODUCT_TOKEN.fullmatch(robots["product_token"])
    ):
        raise HttpMetadataDerivationError("robots product token is invalid")
    if robots["source_state"] not in {"parsed", "unavailable", "unreachable"}:
        raise HttpMetadataDerivationError("robots source state is invalid")
    if robots["matched_rule"] is None and robots["matched_octets"] != 0:
        raise HttpMetadataDerivationError("robots matched rule metadata is inconsistent")
    if robots["matched_rule"] is not None and (
        robots["matched_rule"] != robots["disposition"]
        or robots["matched_octets"] == 0
    ):
        raise HttpMetadataDerivationError("robots matched rule metadata is inconsistent")
    fallback = {
        "unavailable": "allow",
        "unreachable": "disallow",
    }.get(robots["source_state"])
    if fallback is not None and (
        robots["disposition"] != fallback
        or robots["matched_rule"] is not None
        or robots["matched_group_count"] != 0
        or robots["applicable_rule_count"] != 0
    ):
        raise HttpMetadataDerivationError("robots fallback decision is inconsistent")

    maximum_references = limits["max_urls"] * limits["max_links_per_document"]
    for field in (
        "discovered_references",
        "discovered_unique_internal",
        "edge_count",
    ):
        _integer(summary[field], field, 0, maximum_references + 1)
    for field in ("enqueued", "visited", "successful", "broken_internal"):
        _integer(summary[field], field, 0, limits["max_urls"])
    _integer(
        summary["maximum_depth_reached"],
        "maximum_depth_reached",
        0,
        limits["max_depth"],
    )
    _integer(
        summary["response_bytes"],
        "response_bytes",
        0,
        limits["max_total_bytes"],
    )
    if summary["discovered_unique_internal"] < 1:
        raise HttpMetadataDerivationError(
            "crawl must include its entry in discovered unique internal URLs"
        )
    _number(
        summary["elapsed_milliseconds"],
        "elapsed_milliseconds",
        0,
        limits["max_total_elapsed_milliseconds"],
    )
    status_classes = _counts(
        summary["status_classes"],
        frozenset({"1xx", "2xx", "3xx", "4xx", "5xx"}),
        "status_classes",
    )
    failures = _counts(summary["failures"], _CRAWL_FAILURE_CLASSES, "failures")
    discarded = _counts(summary["discarded"], _CRAWL_DISCARD_REASONS, "discarded")
    if summary["termination_reason"] not in {
        "complete",
        "url_budget",
        "byte_budget",
        "time_budget",
    }:
        raise HttpMetadataDerivationError("crawl termination reason is invalid")
    expected_truncated = summary["termination_reason"] != "complete"
    if crawl_observation.truncated is not expected_truncated:
        raise HttpMetadataDerivationError(
            "crawl observation truncation does not match its termination reason"
        )
    if not (
        summary["visited"] <= summary["enqueued"] <= limits["max_urls"]
        and summary["successful"] <= summary["visited"]
        and summary["broken_internal"] <= max(0, summary["visited"] - 1)
        and summary["maximum_depth_reached"] <= limits["max_depth"]
        and summary["response_bytes"] <= limits["max_total_bytes"]
        and summary["discovered_unique_internal"] >= summary["enqueued"]
        and summary["edge_count"] <= summary["discovered_references"]
    ):
        raise HttpMetadataDerivationError("bounded crawl aggregate counts are inconsistent")
    if sum(status_classes.values()) + sum(failures.values()) != summary["visited"]:
        raise HttpMetadataDerivationError(
            "crawl outcomes do not account for every visited URL"
        )
    if status_classes.get("2xx", 0) + status_classes.get("3xx", 0) != summary[
        "successful"
    ]:
        raise HttpMetadataDerivationError("crawl success count is inconsistent")
    terminal_discards = sum(
        discarded.get(reason, 0)
        for reason in ("robots_policy", "byte_budget", "time_budget")
    )
    if summary["visited"] + terminal_discards != summary["enqueued"]:
        raise HttpMetadataDerivationError(
            "crawl queue outcomes do not account for every enqueued URL"
        )
    return payload


def _mapping_keys(value: Any, expected: frozenset[str], field: str) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise HttpMetadataDerivationError(f"{field} fields are invalid")


def _counts(
    value: Any,
    allowed: frozenset[str],
    field: str,
) -> Mapping[str, int]:
    if not isinstance(value, Mapping) or any(key not in allowed for key in value):
        raise HttpMetadataDerivationError(f"{field} is invalid")
    for key, count in value.items():
        if not isinstance(key, str):
            raise HttpMetadataDerivationError(f"{field} is invalid")
        _integer(count, f"{field}.{key}", 1, 1_000_001)
    return value


def _number(value: Any, field: str, minimum: float, maximum: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not minimum <= value <= maximum
    ):
        raise HttpMetadataDerivationError(
            f"{field} must be a number between {minimum} and {maximum}"
        )


def _printable_identifier(value: Any, field: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise HttpMetadataDerivationError(f"{field} is invalid")


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


def _crawl_evidence(
    observation: Observation,
    *,
    indicator_id: str,
    method_id: str,
    data: Mapping[str, Any],
    summary: str,
) -> Evidence:
    return Evidence.create(
        evidence_id=(
            f"bounded-crawl-{indicator_id.lower()}-v1-0-0-{observation.content_hash}"
        ),
        analysis_id=observation.analysis_id,
        indicator_id=indicator_id,
        kind="crawl_indicator_measurement",
        subject_uri=observation.subject_uri,
        method_id=method_id,
        method_version="1.0.0",
        created_at=observation.observed_at,
        confidence=1.0,
        observations=(observation,),
        data=data,
        summary=summary,
    )
