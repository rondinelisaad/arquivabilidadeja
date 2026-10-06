from __future__ import annotations

import json
import math
import sys
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from archivability.evidence import (  # noqa: E402
    Evidence,
    EvidenceValidationError,
    Observation,
    derive_indicator_result,
)
from archivability.methodology import ResultState, load_methodology  # noqa: E402
from archivability.methodology.models import ScoringInputError  # noqa: E402


NOW = datetime(2026, 10, 3, 12, 30, tzinfo=timezone.utc)
METHODOLOGY = ROOT / "methodology" / "v0.1.0"


def observation(
    *,
    observation_id: str = "obs-1",
    analysis_id: str = "analysis-1",
    subject_uri: str = "https://example.org/",
    payload: dict | None = None,
) -> Observation:
    return Observation.create(
        observation_id=observation_id,
        analysis_id=analysis_id,
        attempt_id="attempt-1",
        kind="http_response",
        subject_uri=subject_uri,
        observed_at=NOW,
        probe_id="probe-http",
        tool_name="http-probe",
        tool_version="1.2.0",
        payload_schema_version="1.0",
        payload=payload or {"status": 200, "headers": {"content-type": "text/html"}},
    )


def evidence(
    *,
    item: Observation | None = None,
    evidence_id: str = "ev-1",
    indicator_id: str = "D01",
    confidence: float = 0.9,
) -> Evidence:
    source = item or observation()
    return Evidence.create(
        evidence_id=evidence_id,
        analysis_id=source.analysis_id,
        indicator_id=indicator_id,
        kind="derived_measurement",
        subject_uri=source.subject_uri,
        method_id="discoverability-check",
        method_version="1.0.0",
        created_at=NOW,
        confidence=confidence,
        observations=[source],
        data={"found": True},
        summary="A página foi localizada por uma origem declarada.",
    )


class ObservationTests(unittest.TestCase):
    def test_hash_is_canonical_for_object_key_order(self) -> None:
        first = observation(payload={"b": 2, "a": {"y": 2, "x": 1}})
        second = observation(payload={"a": {"x": 1, "y": 2}, "b": 2})
        self.assertEqual(first.content_hash, second.content_hash)

    def test_payload_is_deeply_immutable(self) -> None:
        item = observation(payload={"headers": {"x-test": ["one"]}})
        with self.assertRaises(TypeError):
            item.payload["new"] = True  # type: ignore[index]
        with self.assertRaises(TypeError):
            item.payload["headers"]["x-test"] = []  # type: ignore[index]
        with self.assertRaises(FrozenInstanceError):
            item.kind = "changed"  # type: ignore[misc]

    def test_naive_timestamp_is_rejected(self) -> None:
        with self.assertRaises(EvidenceValidationError):
            Observation.create(
                observation_id="obs-1", analysis_id="a", attempt_id="t",
                kind="http", subject_uri="https://example.org/",
                observed_at=datetime(2026, 10, 3), probe_id="p",
                tool_name="tool", tool_version="1", payload_schema_version="1",
                payload={},
            )

    def test_uri_credentials_and_non_http_schemes_are_rejected(self) -> None:
        for uri in ("https://user:secret@example.org/", "file:///etc/passwd"):
            with self.subTest(uri=uri), self.assertRaises(EvidenceValidationError):
                observation(subject_uri=uri)

    def test_non_json_and_non_finite_payloads_are_rejected(self) -> None:
        for payload in ({"bad": {1, 2}}, {"bad": math.nan}):
            with self.subTest(payload=payload), self.assertRaises(EvidenceValidationError):
                observation(payload=payload)

    def test_serialization_is_stable_and_hash_verifiable(self) -> None:
        item = observation()
        self.assertEqual(item.content_hash, item.calculate_content_hash())
        self.assertEqual(item.to_dict(), json.loads(json.dumps(item.to_dict())))


class EvidenceAndDerivationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.methodology = load_methodology(METHODOLOGY)

    def test_evidence_rejects_mixed_analyses(self) -> None:
        first = observation(observation_id="obs-1")
        second = observation(observation_id="obs-2", analysis_id="analysis-2")
        with self.assertRaises(EvidenceValidationError):
            Evidence.create(
                evidence_id="ev", analysis_id="analysis-1", indicator_id="D01",
                kind="derived", subject_uri="https://example.org/", method_id="m",
                method_version="1", created_at=NOW, confidence=1,
                observations=[first, second], data={}, summary="summary",
            )

    def test_evidence_rejects_duplicate_sources(self) -> None:
        source = observation()
        with self.assertRaises(EvidenceValidationError):
            Evidence.create(
                evidence_id="ev", analysis_id="analysis-1", indicator_id="D01",
                kind="derived", subject_uri="https://example.org/", method_id="m",
                method_version="1", created_at=NOW, confidence=1,
                observations=[source, source], data={}, summary="summary",
            )

    def test_derivation_preserves_complete_provenance(self) -> None:
        source = observation()
        proof = evidence(item=source)
        result = derive_indicator_result(
            self.methodology,
            indicator_id="D01",
            state=ResultState.PASS,
            evidence=[proof],
        )
        self.assertEqual("analysis-1", result.analysis_id)
        self.assertEqual((proof.evidence_id,), result.evidence_ids)
        self.assertEqual((proof.content_hash,), result.evidence_hashes)
        self.assertEqual(("probe-http",), result.probe_ids)
        self.assertEqual(("http-probe@1.2.0",), result.tool_versions)
        self.assertEqual(NOW, result.measured_at)
        self.assertEqual("discoverability-check@1.0.0", result.measurement_method_version)
        serialized = result.to_dict()
        self.assertEqual([proof.evidence_id], serialized["evidence_ids"])
        self.assertEqual(ResultState.PASS.value, serialized["state"])

    def test_result_confidence_cannot_exceed_evidence(self) -> None:
        with self.assertRaises(EvidenceValidationError):
            derive_indicator_result(
                self.methodology, indicator_id="D01", state=ResultState.PASS,
                evidence=[evidence(confidence=0.6)], confidence=0.7,
            )

    def test_unknown_result_does_not_receive_zero_score(self) -> None:
        result = derive_indicator_result(
            self.methodology, indicator_id="D01", state=ResultState.UNKNOWN,
            evidence=[evidence()],
        )
        self.assertIsNone(result.score)

    def test_context_state_rules_are_enforced(self) -> None:
        context_proof = evidence(indicator_id="N01")
        context = derive_indicator_result(
            self.methodology, indicator_id="N01", state=ResultState.CONTEXT,
            evidence=[context_proof],
        )
        self.assertEqual(ResultState.CONTEXT, context.state)
        with self.assertRaises(ScoringInputError):
            derive_indicator_result(
                self.methodology, indicator_id="N01", state=ResultState.PASS,
                evidence=[context_proof],
            )

    def test_domain_schemas_are_valid_json(self) -> None:
        schema_dir = ROOT / "schemas" / "domain" / "v1"
        documents = [json.loads(path.read_text(encoding="utf-8")) for path in schema_dir.glob("*.json")]
        self.assertEqual(7, len(documents))
        self.assertTrue(all(item["$schema"].endswith("2020-12/schema") for item in documents))


if __name__ == "__main__":
    unittest.main()
