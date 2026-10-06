from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from archivability.evidence import Evidence, Observation, derive_indicator_result  # noqa: E402
from archivability.methodology import ResultState, load_methodology  # noqa: E402
from archivability.methodology.models import IndicatorResult  # noqa: E402
from archivability.storage import (  # noqa: E402
    AuditContext,
    DuplicateRecordError,
    IntegrityViolation,
    SqliteEvidenceRepository,
    apply_sqlite_migrations,
)


NOW = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)
METHODOLOGY = ROOT / "methodology" / "v0.1.0"


def build_chain() -> tuple[Observation, Evidence, object]:
    observation = Observation.create(
        observation_id="obs-1",
        analysis_id="analysis-1",
        attempt_id="attempt-1",
        kind="http_response",
        subject_uri="https://example.org/private?token=must-not-be-logged",
        observed_at=NOW,
        probe_id="probe-http",
        tool_name="http-probe",
        tool_version="1.2.0",
        payload_schema_version="1.0",
        payload={"status": 200, "secret": "must-not-be-logged"},
    )
    evidence = Evidence.create(
        evidence_id="ev-1",
        analysis_id="analysis-1",
        indicator_id="D01",
        kind="derived_measurement",
        subject_uri=observation.subject_uri,
        method_id="discoverability-check",
        method_version="1.0.0",
        created_at=NOW,
        confidence=0.9,
        observations=[observation],
        data={"found": True},
        summary="Origem declarada localizada.",
    )
    result = derive_indicator_result(
        load_methodology(METHODOLOGY),
        indicator_id="D01",
        state=ResultState.PASS,
        evidence=[evidence],
    )
    return observation, evidence, result


class SqliteEvidenceRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        apply_sqlite_migrations(self.connection)
        counter = iter(range(100))
        self.repository = SqliteEvidenceRepository(
            self.connection,
            clock=lambda: NOW,
            event_id_factory=lambda: f"event-{next(counter)}",
        )
        self.observation, self.evidence, self.result = build_chain()

    def tearDown(self) -> None:
        self.connection.close()

    def test_atomic_chain_round_trip_and_audit(self) -> None:
        self.repository.save_chain(
            observations=[self.observation],
            evidence=[self.evidence],
            indicator_results=[self.result],
            audit=AuditContext(
                user_id="user-opaque-1",
                ip_address="192.0.2.10",
                session_id="session-opaque-1",
            ),
        )

        loaded_observation = self.repository.get_observation("obs-1")
        loaded_evidence = self.repository.get_evidence("ev-1")
        loaded_result = self.repository.get_indicator_result("analysis-1", "D01")
        self.assertEqual(self.observation.to_dict(), loaded_observation.to_dict())
        self.assertEqual(self.evidence.to_dict(), loaded_evidence.to_dict())
        self.assertEqual(self.result.to_dict(), loaded_result.to_dict())

        audit_rows = self.connection.execute(
            "SELECT result, user_id, after_json, extra_json FROM audit_events ORDER BY event_id"
        ).fetchall()
        self.assertEqual(3, len(audit_rows))
        self.assertTrue(all(row[0] == "success" for row in audit_rows))
        self.assertTrue(all(row[1] == "user-opaque-1" for row in audit_rows))
        serialized_audit = json.dumps(audit_rows)
        self.assertNotIn("must-not-be-logged", serialized_audit)
        self.assertNotIn("example.org", serialized_audit)

    def test_database_blocks_update_and_delete(self) -> None:
        self.repository.save_observation(self.observation)
        for statement in (
            "UPDATE observations SET analysis_id = 'changed' WHERE observation_id = 'obs-1'",
            "DELETE FROM observations WHERE observation_id = 'obs-1'",
            "UPDATE audit_events SET result = 'failure' WHERE resource_id = 'obs-1'",
            "DELETE FROM audit_events WHERE resource_id = 'obs-1'",
        ):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                self.connection.execute(statement)
            self.connection.rollback()

    def test_duplicate_is_rejected_and_failure_is_audited(self) -> None:
        self.repository.save_observation(self.observation)
        with self.assertRaises(DuplicateRecordError):
            self.repository.save_observation(self.observation)
        results = self.connection.execute(
            "SELECT result, extra_json FROM audit_events ORDER BY event_id"
        ).fetchall()
        self.assertEqual(["success", "failure"], [row[0] for row in results])
        self.assertEqual("IntegrityError", json.loads(results[1][1])["error_type"])

    def test_chain_rolls_back_when_provenance_is_missing(self) -> None:
        with self.assertRaises(IntegrityViolation):
            self.repository.save_chain(
                observations=[],
                evidence=[self.evidence],
                indicator_results=[self.result],
            )
        self.assertEqual(0, self.connection.execute("SELECT count(*) FROM evidence").fetchone()[0])
        audit = self.connection.execute(
            "SELECT result, resource FROM audit_events"
        ).fetchall()
        self.assertEqual([("failure", "analysis_chain")], audit)

    def test_hash_mismatch_is_detected_during_read(self) -> None:
        self.repository.save_observation(self.observation)
        self.connection.execute("DROP TRIGGER observations_no_update")
        document = self.observation.to_dict()
        document["payload"]["status"] = 500
        self.connection.execute(
            "UPDATE observations SET document_json = ? WHERE observation_id = ?",
            (json.dumps(document), self.observation.observation_id),
        )
        self.connection.commit()
        with self.assertRaises(IntegrityViolation):
            self.repository.get_observation(self.observation.observation_id)

    def test_evidence_requires_matching_stored_observation_hash(self) -> None:
        altered_observation = Observation.create(
            observation_id="obs-1",
            analysis_id="analysis-1",
            attempt_id="attempt-1",
            kind="http_response",
            subject_uri=self.observation.subject_uri,
            observed_at=NOW,
            probe_id="probe-http",
            tool_name="http-probe",
            tool_version="1.2.0",
            payload_schema_version="1.0",
            payload={"status": 404},
        )
        self.repository.save_observation(altered_observation)
        with self.assertRaises(IntegrityViolation):
            self.repository.save_evidence(self.evidence)

    def test_result_cannot_reference_evidence_from_another_indicator(self) -> None:
        self.repository.save_observation(self.observation)
        self.repository.save_evidence(self.evidence)
        inconsistent = IndicatorResult(
            indicator_id="D02",
            state=ResultState.PASS,
            confidence=0.9,
            analysis_id="analysis-1",
            evidence_ids=(self.evidence.evidence_id,),
            evidence_hashes=(self.evidence.content_hash,),
            measured_at=NOW,
            probe_ids=("probe-http",),
            tool_versions=("http-probe@1.2.0",),
            measurement_method_version="discoverability-check@1.0.0",
        )
        with self.assertRaises(IntegrityViolation):
            self.repository.save_indicator_result(inconsistent)


if __name__ == "__main__":
    unittest.main()
