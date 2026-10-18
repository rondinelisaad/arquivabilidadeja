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
