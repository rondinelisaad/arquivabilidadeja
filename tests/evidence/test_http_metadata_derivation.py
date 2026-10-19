from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from archivability.evidence import (  # noqa: E402
    HttpMetadataDerivationError,
    Observation,
    derive_http_metadata_indicators,
)
from archivability.methodology import (  # noqa: E402
    ResultState,
    ScoringEngine,
    load_methodology,
)

NOW = datetime(2026, 10, 7, 1, 0, tzinfo=timezone.utc)
METHODOLOGY_PATH = ROOT / "methodology" / "v0.1.0"


def http_observation(
    *,
    status_code: int = 200,
    headers: dict[str, list[str]] | None = None,
    observed_bytes: int = 5,
    byte_limit: int = 1024,
    truncated: bool = False,
    kind: str = "http_metadata",
    schema_version: str = "1.1",
) -> Observation:
    return Observation.create(
        observation_id="observation-http-1",
        analysis_id="analysis-1",
        attempt_id="attempt-1",
        kind=kind,
        subject_uri="https://example.org/private?token=must-not-be-logged",
        observed_at=NOW,
        probe_id="http-metadata",
        tool_name="arquivabilidade-http",
        tool_version="0.1.0",
        payload_schema_version=schema_version,
        payload={
            "status_code": status_code,
            "headers": headers if headers is not None else {"content-length": ["5"]},
            "response_bytes_observed": observed_bytes,
            "response_byte_limit": byte_limit,
            "response_truncated": truncated,
            "redirect_count": 1,
            "final_transport_secure": True,
        },
        truncated=truncated,
    )


def discovery_observation() -> Observation:
    base = http_observation()
    payload = dict(base.payload)
    payload.update(
        {
            "elapsed_milliseconds": 42.5,
            "content_type": "text/html",
            "format_family": "html",
            "html": {
                "parseable": True,
                "link_count": 3,
                "internal_link_count": 2,
                "external_link_count": 1,
                "feed_link_count": 1,
                "canonical_present": True,
                "truncated": False,
            },
            "robots": {
                "outcome": "available",
                "status_code": 200,
                "parseable": True,
                "item_count": 4,
                "truncated": False,
                "elapsed_milliseconds": 10.0,
            },
            "sitemap": {
                "outcome": "available",
                "status_code": 200,
                "parseable": True,
                "item_count": 8,
                "truncated": False,
                "elapsed_milliseconds": 11.0,
            },
        }
    )
    return Observation.create(
        observation_id=base.observation_id,
        analysis_id=base.analysis_id,
        attempt_id=base.attempt_id,
        kind=base.kind,
        subject_uri=base.subject_uri,
        observed_at=base.observed_at,
        probe_id=base.probe_id,
        tool_name=base.tool_name,
        tool_version="0.2.0",
        payload_schema_version="1.2",
        payload=payload,
    )


def latency_observation(*, successful_samples: int = 5) -> Observation:
    base = discovery_observation()
    payload = dict(base.payload)
    sample = {
        "outcome": "success",
        "status_code": 200,
        "total_milliseconds": 40.0,
        "dns_milliseconds": None,
        "tcp_milliseconds": 5.0,
        "tls_milliseconds": 10.0,
        "ttfb_milliseconds": 30.0,
        "null_reasons": {
            "dns": "resolution_precedes_transport_timer",
            "tcp": None,
            "tls": None,
            "ttfb": None,
        },
        "address_family": "ipv4",
        "redirect_exchanges": 1,
    }
    failure = {
        "outcome": "failure",
        "failure_class": "timeout",
        "status_code": None,
        "total_milliseconds": None,
        "dns_milliseconds": None,
        "tcp_milliseconds": None,
        "tls_milliseconds": None,
        "ttfb_milliseconds": None,
        "null_reasons": {
            "total": "sample_failed_timeout",
            "dns": "sample_failed_timeout",
            "tcp": "sample_failed_timeout",
            "tls": "sample_failed_timeout",
            "ttfb": "sample_failed_timeout",
        },
        "address_family": None,
        "redirect_exchanges": 0,
    }
    deviation = 0.0 if successful_samples >= 2 else None
    payload["latency"] = {
        "primary": sample,
        "profile": {
            "method": "GET",
            "response_byte_limit": 1,
            "cache_state": "unknown",
            "connection_reuse": False,
            "interval_milliseconds": 200.0,
        },
        "requested_samples": 5,
        "samples": [sample] * successful_samples + [failure] * (5 - successful_samples),
        "summary": {
            "sample_size": successful_samples,
            "success_rate": successful_samples / 5,
            "minimum_milliseconds": 40.0 if successful_samples else None,
            "maximum_milliseconds": 40.0 if successful_samples else None,
            "median_milliseconds": 40.0 if successful_samples else None,
            "sample_standard_deviation_milliseconds": deviation,
            "sample_standard_deviation_null_reason": (
                None
                if successful_samples >= 2
                else "minimum_2_successful_samples_required"
            ),
            "p95_milliseconds": None,
            "p95_null_reason": "minimum_20_successful_samples_required",
        },
    }
    return Observation.create(
        observation_id=base.observation_id,
        analysis_id=base.analysis_id,
        attempt_id=base.attempt_id,
        kind=base.kind,
        subject_uri=base.subject_uri,
        observed_at=base.observed_at,
        probe_id=base.probe_id,
        tool_name=base.tool_name,
        tool_version="0.3.0",
        payload_schema_version="1.3",
        payload=payload,
    )


def crawl_observation(
    *,
    robots: dict[str, object] | None = None,
    summary: dict[str, object] | None = None,
    attempt_id: str = "attempt-1",
    kind: str = "bounded_crawl",
    truncated: bool = False,
) -> Observation:
    robots_payload = robots or {
        "disposition": "allow",
        "matched_rule": None,
        "matched_octets": 0,
        "matched_group_count": 1,
        "applicable_rule_count": 2,
        "policy_id": "rfc9309-respect-1",
        "parser_version": "rfc9309-1",
        "product_token": "arquivabilidade-ja",
        "source_state": "parsed",
    }
    summary_payload = summary or {
        "discovered_references": 3,
        "discovered_unique_internal": 3,
        "enqueued": 3,
        "visited": 3,
        "successful": 2,
        "broken_internal": 1,
        "edge_count": 2,
        "maximum_depth_reached": 2,
        "response_bytes": 1000,
        "elapsed_milliseconds": 100.0,
        "status_classes": {"2xx": 2, "4xx": 1},
        "failures": {},
        "discarded": {"external_origin": 1},
        "termination_reason": "complete",
    }
    return Observation.create(
        observation_id="observation-crawl-1",
        analysis_id="analysis-1",
        attempt_id=attempt_id,
        kind=kind,
        subject_uri="https://example.org/private?token=must-not-be-logged",
        observed_at=NOW,
        probe_id="http-metadata",
        tool_name="arquivabilidade-http",
        tool_version="0.2.0",
        payload_schema_version="1.0",
        payload={
            "limits": {
                "max_depth": 2,
                "max_urls": 10,
                "max_response_bytes": 2 * 1024 * 1024,
                "max_total_bytes": 20 * 1024 * 1024,
                "max_total_elapsed_milliseconds": 60_000,
                "max_links_per_document": 1000,
            },
            "robots": robots_payload,
            "summary": summary_payload,
        },
        truncated=truncated,
    )


class HttpMetadataDerivationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.methodology = load_methodology(METHODOLOGY_PATH)

    def test_derives_d01_and_r06_with_stable_complete_provenance(self) -> None:
        source = http_observation()

        first = derive_http_metadata_indicators(self.methodology, source)
        second = derive_http_metadata_indicators(self.methodology, source)

        self.assertEqual(
            ["D01", "R06"],
            [item.indicator_id for item in first.indicator_results],
        )
        self.assertEqual(
            [ResultState.PASS, ResultState.PASS],
            [item.state for item in first.indicator_results],
        )
        self.assertEqual(
            [item.to_dict() for item in first.evidence],
            [item.to_dict() for item in second.evidence],
        )
        self.assertTrue(
            all(item.sources[0].content_hash == source.content_hash for item in first.evidence)
        )
        self.assertEqual(
            "complete",
            first.evidence[1].data["termination_reason"],
        )
        serialized = json.dumps([item.to_dict() for item in first.evidence])
        self.assertNotIn('"secret"', serialized)

    def test_status_mapping_is_explicit(self) -> None:
        for status_code, expected in (
            (103, ResultState.UNKNOWN),
            (204, ResultState.PASS),
            (302, ResultState.WARNING),
            (401, ResultState.FAIL),
            (503, ResultState.FAIL),
        ):
            with self.subTest(status_code=status_code):
                result = derive_http_metadata_indicators(
                    self.methodology,
                    http_observation(status_code=status_code),
                )
                self.assertEqual(expected, result.indicator_results[0].state)

    def test_discovery_contract_derives_http_phase_indicators(self) -> None:
        derived = derive_http_metadata_indicators(
            self.methodology, discovery_observation()
        )

        self.assertEqual(
            ["D01", "R06", "D02", "D04", "D05", "D06", "R02", "R04", "R05", "S01", "S03", "S04", "N01"],
            [item.indicator_id for item in derived.indicator_results],
        )
        by_id = {item.indicator_id: item for item in derived.indicator_results}
        self.assertEqual(ResultState.PASS, by_id["D02"].state)
        self.assertEqual(ResultState.PASS, by_id["D04"].state)
        self.assertEqual(ResultState.CONTEXT, by_id["S04"].state)
        self.assertEqual(ResultState.CONTEXT, by_id["N01"].state)
        serialized = json.dumps([item.to_dict() for item in derived.evidence])
        self.assertNotIn("discovered_urls", serialized)

    def test_latency_contract_derives_phase_and_distribution_context(self) -> None:
        derived = derive_http_metadata_indicators(
            self.methodology, latency_observation()
        )

        by_id = {item.indicator_id: item for item in derived.indicator_results}
        evidence = {item.indicator_id: item for item in derived.evidence}
        self.assertEqual(ResultState.CONTEXT, by_id["N01"].state)
        self.assertEqual(ResultState.CONTEXT, by_id["N02"].state)
        self.assertEqual(ResultState.PASS, by_id["O01"].state)
        self.assertEqual(5.0, evidence["N01"].data["timings"]["tcp_milliseconds"])
        self.assertEqual(5, evidence["N02"].data["sample_size"])
        self.assertIsNone(evidence["N02"].data["statistics"]["p95_milliseconds"])
        self.assertEqual(
            "minimum_20_successful_samples_required",
            evidence["N02"].data["null_reasons"]["p95"],
        )

    def test_retrieval_repetition_scores_failures_not_latency_magnitude(self) -> None:
        warning = derive_http_metadata_indicators(
            self.methodology, latency_observation(successful_samples=4)
        )
        failure = derive_http_metadata_indicators(
            self.methodology, latency_observation(successful_samples=3)
        )

        warning_results = {
            item.indicator_id: item for item in warning.indicator_results
        }
        failure_results = {
            item.indicator_id: item for item in failure.indicator_results
        }
        failure_evidence = {item.indicator_id: item for item in failure.evidence}
        self.assertEqual(ResultState.WARNING, warning_results["O01"].state)
        self.assertEqual(ResultState.FAIL, failure_results["O01"].state)
        self.assertEqual({"timeout": 2}, failure_evidence["O01"].data["failure_classes"])

    def test_crawl_contract_derives_d03_d08_r01_and_r03_without_urls(self) -> None:
        derived = derive_http_metadata_indicators(
            self.methodology,
            discovery_observation(),
            crawl_observation=crawl_observation(),
        )

        by_id = {item.indicator_id: item for item in derived.indicator_results}
        self.assertEqual(ResultState.PASS, by_id["D03"].state)
        self.assertEqual(ResultState.PASS, by_id["D08"].state)
        self.assertEqual(ResultState.FAIL, by_id["R01"].state)
        self.assertEqual(ResultState.WARNING, by_id["R03"].state)
        crawl_evidence = {
            item.indicator_id: item
            for item in derived.evidence
            if item.indicator_id in {"D03", "D08", "R01", "R03"}
        }
        self.assertEqual(3, crawl_evidence["R01"].data["sample_size"])
        self.assertEqual(2, crawl_evidence["R01"].data["success_count"])
        self.assertEqual(2, crawl_evidence["R03"].data["denominator"])
        self.assertEqual(1, crawl_evidence["R03"].data["broken_count"])
        serialized = json.dumps(
            [item.to_dict()["data"] for item in crawl_evidence.values()]
        )
        self.assertNotIn("example.org", serialized)
        self.assertNotIn("must-not-be-logged", serialized)

    def test_crawl_states_distinguish_policy_and_budget_outcomes(self) -> None:
        cases = (
            (
                {
                    "disposition": "disallow",
                    "matched_rule": "disallow",
                    "matched_octets": 1,
                    "matched_group_count": 1,
                    "applicable_rule_count": 1,
                    "policy_id": "rfc9309-respect-1",
                    "parser_version": "rfc9309-1",
                    "product_token": "arquivabilidade-ja",
                    "source_state": "parsed",
                },
                ResultState.FAIL,
            ),
            (
                {
                    "disposition": "allow",
                    "matched_rule": None,
                    "matched_octets": 0,
                    "matched_group_count": 0,
                    "applicable_rule_count": 0,
                    "policy_id": "rfc9309-respect-1",
                    "parser_version": "rfc9309-1",
                    "product_token": "arquivabilidade-ja",
                    "source_state": "unavailable",
                },
                ResultState.NOT_APPLICABLE,
            ),
            (
                {
                    "disposition": "disallow",
                    "matched_rule": None,
                    "matched_octets": 0,
                    "matched_group_count": 0,
                    "applicable_rule_count": 0,
                    "policy_id": "rfc9309-respect-1",
                    "parser_version": "rfc9309-1",
                    "product_token": "arquivabilidade-ja",
                    "source_state": "unreachable",
                },
                ResultState.UNKNOWN,
            ),
        )
        for robots, expected in cases:
            with self.subTest(source_state=robots["source_state"]):
                derived = derive_http_metadata_indicators(
                    self.methodology,
                    discovery_observation(),
                    crawl_observation=crawl_observation(robots=robots),
                )
                by_id = {
                    item.indicator_id: item for item in derived.indicator_results
                }
                self.assertEqual(expected, by_id["D03"].state)

        budget_summary = dict(crawl_observation().payload["summary"])
        budget_summary.update(
            {
                "discovered_unique_internal": 4,
                "enqueued": 4,
                "discarded": {"external_origin": 1, "time_budget": 1},
                "termination_reason": "time_budget",
            }
        )
        budget = crawl_observation(summary=budget_summary, truncated=True)
        derived = derive_http_metadata_indicators(
            self.methodology,
            discovery_observation(),
            crawl_observation=budget,
        )
        by_id = {item.indicator_id: item for item in derived.indicator_results}
        self.assertEqual(ResultState.WARNING, by_id["D08"].state)

        partial_policy = dict(crawl_observation().payload["summary"])
        partial_policy.update(
            {
                "visited": 2,
                "successful": 2,
                "broken_internal": 0,
                "status_classes": {"2xx": 2},
                "discarded": {"external_origin": 1, "robots_policy": 1},
            }
        )
        derived = derive_http_metadata_indicators(
            self.methodology,
            discovery_observation(),
            crawl_observation=crawl_observation(summary=partial_policy),
        )
        by_id = {item.indicator_id: item for item in derived.indicator_results}
        self.assertEqual(ResultState.WARNING, by_id["D03"].state)
        self.assertEqual(ResultState.PASS, by_id["R03"].state)
        self.assertEqual(ResultState.PASS, by_id["R01"].state)

        all_broken = dict(crawl_observation().payload["summary"])
        all_broken.update(
            {
                "successful": 1,
                "broken_internal": 2,
                "status_classes": {"2xx": 1, "4xx": 2},
            }
        )
        derived = derive_http_metadata_indicators(
            self.methodology,
            discovery_observation(),
            crawl_observation=crawl_observation(summary=all_broken),
        )
        by_id = {item.indicator_id: item for item in derived.indicator_results}
        self.assertEqual(ResultState.FAIL, by_id["R03"].state)
        self.assertEqual(ResultState.FAIL, by_id["R01"].state)

        mostly_successful = dict(crawl_observation().payload["summary"])
        mostly_successful.update(
            {
                "discovered_references": 5,
                "discovered_unique_internal": 5,
                "enqueued": 5,
                "visited": 5,
                "successful": 4,
                "broken_internal": 1,
                "edge_count": 4,
                "status_classes": {"2xx": 4, "4xx": 1},
            }
        )
        derived = derive_http_metadata_indicators(
            self.methodology,
            discovery_observation(),
            crawl_observation=crawl_observation(summary=mostly_successful),
        )
        by_id = {item.indicator_id: item for item in derived.indicator_results}
        self.assertEqual(ResultState.WARNING, by_id["R01"].state)

        no_visit = dict(crawl_observation().payload["summary"])
        no_visit.update(
            {
                "enqueued": 1,
                "visited": 0,
                "successful": 0,
                "broken_internal": 0,
                "status_classes": {},
                "discarded": {"robots_policy": 1},
            }
        )
        derived = derive_http_metadata_indicators(
            self.methodology,
            discovery_observation(),
            crawl_observation=crawl_observation(summary=no_visit),
        )
        by_id = {item.indicator_id: item for item in derived.indicator_results}
        self.assertEqual(ResultState.UNKNOWN, by_id["R01"].state)

    def test_rejects_inconsistent_or_unpaired_crawl_observation(self) -> None:
        invalid_summary = dict(crawl_observation().payload["summary"])
        invalid_summary["successful"] = 3
        invalid = (
            crawl_observation(attempt_id="attempt-other"),
            crawl_observation(kind="other"),
            crawl_observation(summary=invalid_summary),
        )
        for source in invalid:
            with self.subTest(kind=source.kind, attempt_id=source.attempt_id):
                with self.assertRaises(HttpMetadataDerivationError):
                    derive_http_metadata_indicators(
                        self.methodology,
                        discovery_observation(),
                        crawl_observation=source,
                    )

    def test_truncation_and_invalid_lengths_are_warnings(self) -> None:
        cases = (
            (
                http_observation(
                    headers={"content-length": ["5000"]},
                    observed_bytes=1024,
                    byte_limit=1024,
                    truncated=True,
                ),
                "response_byte_limit",
            ),
            (
                http_observation(headers={"content-length": ["five"]}),
                "invalid_content_length",
            ),
            (
                http_observation(headers={"content-length": ["4", "5"]}),
                "invalid_content_length",
            ),
            (
                http_observation(headers={"content-length": ["6"]}),
                "content_length_mismatch",
            ),
        )
        for source, reason in cases:
            with self.subTest(reason=reason):
                result = derive_http_metadata_indicators(self.methodology, source)
                self.assertEqual(ResultState.WARNING, result.indicator_results[1].state)
                self.assertEqual(reason, result.evidence[1].data["termination_reason"])

    def test_rejects_wrong_contract_or_inconsistent_limits(self) -> None:
        invalid = (
            http_observation(kind="other"),
            http_observation(schema_version="2.0"),
            http_observation(observed_bytes=6, byte_limit=5),
        )
        for source in invalid:
            with self.subTest(kind=source.kind, schema=source.payload_schema_version):
                with self.assertRaises(HttpMetadataDerivationError):
                    derive_http_metadata_indicators(self.methodology, source)

    def test_results_feed_the_existing_scoring_engine(self) -> None:
        derived = derive_http_metadata_indicators(
            self.methodology,
            http_observation(),
        )

        assessment = ScoringEngine(self.methodology).evaluate(
            derived.indicator_results,
            ["D01", "R06"],
        )

        self.assertEqual(100, assessment.dimensions["discoverability"].score)
        self.assertEqual(100, assessment.dimensions["retrievability"].score)
        self.assertEqual(1, assessment.dimensions["discoverability"].coverage)
        self.assertEqual(1, assessment.dimensions["retrievability"].coverage)

    def test_evidence_schemas_are_valid_json(self) -> None:
        directory = ROOT / "schemas" / "evidence" / "v1"
        documents = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*.json"))
        ]
        self.assertEqual(2, len(documents))
        self.assertTrue(all(document["additionalProperties"] is False for document in documents))


if __name__ == "__main__":
    unittest.main()
