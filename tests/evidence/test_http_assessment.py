from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from archivability.evidence import (  # noqa: E402
    HttpMetadataAssessmentService,
    Observation,
    derive_http_metadata_indicators,
)
from archivability.lifecycle import (  # noqa: E402
    AnalysisOrchestrator,
    AnalysisState,
    AttemptState,
    finish_attempt,
)
from archivability.methodology import ResultState, load_methodology  # noqa: E402
from archivability.storage import (  # noqa: E402
    AuditContext,
    IntegrityViolation,
    SqliteLifecycleRepository,
    apply_sqlite_migrations,
)

T0 = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)
SUBJECT = "https://example.org/private?token=must-not-be-logged"
METHODOLOGY_PATH = ROOT / "methodology" / "v0.1.0"


def observation_for(analysis_id: str, attempt_id: str) -> Observation:
    return Observation.create(
        observation_id="observation-http-1",
        analysis_id=analysis_id,
        attempt_id=attempt_id,
        kind="http_metadata",
        subject_uri=SUBJECT,
        observed_at=T0 + timedelta(seconds=2),
        probe_id="http-metadata",
        tool_name="arquivabilidade-http",
        tool_version="0.1.0",
        payload_schema_version="1.1",
        payload={
            "status_code": 200,
            "headers": {"content-length": ["5"]},
            "response_bytes_observed": 5,
            "response_byte_limit": 1024,
            "response_truncated": False,
            "redirect_count": 0,
            "final_transport_secure": True,
        },
    )


def crawl_observations_for(
    analysis_id: str,
    attempt_id: str,
) -> tuple[Observation, Observation]:
    http = Observation.create(
        observation_id="observation-http-crawl",
        analysis_id=analysis_id,
        attempt_id=attempt_id,
        kind="http_metadata",
        subject_uri=SUBJECT,
        observed_at=T0 + timedelta(seconds=2),
        probe_id="http-metadata",
        tool_name="arquivabilidade-http",
        tool_version="0.3.0",
        payload_schema_version="1.2",
        payload={
            "status_code": 200,
            "headers": {"content-length": ["5"]},
            "response_bytes_observed": 5,
            "response_byte_limit": 2 * 1024 * 1024,
            "response_truncated": False,
            "redirect_count": 0,
            "final_transport_secure": True,
            "elapsed_milliseconds": 20.0,
            "content_type": "text/html",
            "format_family": "html",
            "html": {
                "parseable": True,
                "link_count": 2,
                "internal_link_count": 2,
                "external_link_count": 0,
                "feed_link_count": 0,
                "canonical_present": False,
                "truncated": False,
            },
            "robots": {
                "outcome": "available",
                "status_code": 200,
                "parseable": True,
                "item_count": 2,
                "truncated": False,
                "elapsed_milliseconds": 5.0,
            },
            "sitemap": {
                "outcome": "http_error",
                "status_code": 404,
                "parseable": False,
                "item_count": 0,
                "truncated": False,
                "elapsed_milliseconds": 5.0,
            },
        },
    )
    crawl = Observation.create(
        observation_id="observation-crawl-1",
        analysis_id=analysis_id,
        attempt_id=attempt_id,
        kind="bounded_crawl",
        subject_uri=SUBJECT,
        observed_at=T0 + timedelta(seconds=2),
        probe_id="http-metadata",
        tool_name="arquivabilidade-http",
        tool_version="0.3.0",
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
            "robots": {
                "disposition": "allow",
                "matched_rule": None,
                "matched_octets": 0,
                "matched_group_count": 1,
                "applicable_rule_count": 2,
                "policy_id": "rfc9309-respect-1",
                "parser_version": "rfc9309-1",
                "product_token": "arquivabilidade-ja",
                "source_state": "parsed",
            },
            "summary": {
                "discovered_references": 2,
                "discovered_unique_internal": 3,
                "enqueued": 3,
                "visited": 3,
                "successful": 2,
                "broken_internal": 1,
                "edge_count": 2,
                "maximum_depth_reached": 1,
                "response_bytes": 200,
                "elapsed_milliseconds": 30.0,
                "status_classes": {"2xx": 2, "4xx": 1},
                "failures": {},
                "discarded": {},
                "termination_reason": "complete",
            },
        },
    )
    return http, crawl


class HttpMetadataAssessmentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        apply_sqlite_migrations(self.connection)
        event_ids = iter(f"event-{index:03d}" for index in range(200))
        self.repository = SqliteLifecycleRepository(
            self.connection,
            clock=lambda: T0,
            event_id_factory=lambda: next(event_ids),
        )
        self.methodology = load_methodology(METHODOLOGY_PATH)
        times = iter((T0, T0 + timedelta(seconds=1)))
        orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: next(times),
            analysis_id_factory=lambda: "analysis-1",
            attempt_id_factory=lambda: "attempt-1",
        )
        analysis = orchestrator.create_analysis(
            subject_uri=SUBJECT,
            methodology=self.methodology,
        )
        attempt = orchestrator.start_attempt(analysis.analysis_id)
        self.observation = observation_for(analysis.analysis_id, attempt.attempt_id)
        succeeded = finish_attempt(
            attempt,
            target=AttemptState.SUCCEEDED,
            occurred_at=T0 + timedelta(seconds=2),
        )
        self.repository.complete_probe_attempt(
            attempt,
            succeeded,
            (self.observation,),
        )
        self.audit = AuditContext(user_id="user-opaque-1")
        self.service = HttpMetadataAssessmentService(
            methodology=self.methodology,
            repository=self.repository,
            clock=lambda: T0 + timedelta(seconds=3),
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_persists_derivation_and_closes_analysis_atomically(self) -> None:
        outcome = self.service.assess(self.observation.observation_id, audit=self.audit)

        self.assertEqual("created", outcome.persistence.outcome)
        self.assertEqual(AnalysisState.COMPLETED, outcome.analysis.state)
        self.assertEqual(outcome.analysis, self.repository.get_analysis("analysis-1"))
        self.assertEqual(45, self.connection.execute("SELECT count(*) FROM evidence").fetchone()[0])
        self.assertEqual(
            45,
            self.connection.execute("SELECT count(*) FROM indicator_results").fetchone()[0],
        )
        unavailable = next(
            item for item in outcome.derivation.evidence if item.indicator_id == "D02"
        )
        self.assertEqual("measurement_unavailable", unavailable.kind)
        self.assertEqual("measurement_not_implemented", unavailable.data["reason"])
        result = next(
            item
            for item in outcome.derivation.indicator_results
            if item.indicator_id == "D02"
        )
        self.assertEqual(ResultState.UNKNOWN, result.state)
        self.assertEqual(0.0, result.confidence)
        events = self.connection.execute(
            "SELECT action, result, extra_json FROM audit_events ORDER BY event_id"
        ).fetchall()
        self.assertIn(
            ("job.http_metadata_assessment", "success"),
            [row[:2] for row in events],
        )
        serialized = json.dumps(events)
        self.assertNotIn("example.org", serialized)
        self.assertNotIn("must-not-be-logged", serialized)

    def test_exact_retry_is_an_idempotent_replay(self) -> None:
        first = self.service.assess(self.observation.observation_id, audit=self.audit)
        second = self.service.assess(self.observation.observation_id, audit=self.audit)

        self.assertEqual("created", first.persistence.outcome)
        self.assertEqual("replayed", second.persistence.outcome)
        self.assertEqual(first.analysis, second.analysis)
        self.assertEqual(45, self.connection.execute("SELECT count(*) FROM evidence").fetchone()[0])
        outcomes = [
            json.loads(row[0])["outcome"]
            for row in self.connection.execute(
                """
                SELECT extra_json FROM audit_events
                WHERE action = 'job.http_metadata_assessment'
                ORDER BY event_id
                """
            ).fetchall()
        ]
        self.assertEqual(["created", "replayed"], outcomes)

    def test_partial_preexisting_derivation_is_rejected_without_finalizing(self) -> None:
        derivation = derive_http_metadata_indicators(self.methodology, self.observation)
        self.repository.save_evidence(derivation.evidence[0], audit=self.audit)

        with self.assertRaises(IntegrityViolation):
            self.service.assess(self.observation.observation_id, audit=self.audit)

        self.assertEqual(AnalysisState.RUNNING, self.repository.get_analysis("analysis-1").state)
        self.assertEqual(1, self.connection.execute("SELECT count(*) FROM evidence").fetchone()[0])
        self.assertEqual(
            0,
            self.connection.execute("SELECT count(*) FROM indicator_results").fetchone()[0],
        )
        event = self.connection.execute(
            """
            SELECT result, extra_json FROM audit_events
            WHERE action = 'job.http_metadata_assessment'
            ORDER BY event_id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual("failure", event[0])
        self.assertEqual("IntegrityViolation", json.loads(event[1])["error_type"])

    def test_assesses_paired_crawl_observation_atomically_and_idempotently(self) -> None:
        times = iter((T0, T0 + timedelta(seconds=1)))
        orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: next(times),
            analysis_id_factory=lambda: "analysis-crawl-1",
            attempt_id_factory=lambda: "attempt-crawl-1",
        )
        analysis = orchestrator.create_analysis(
            subject_uri=SUBJECT,
            methodology=self.methodology,
        )
        attempt = orchestrator.start_attempt(analysis.analysis_id)
        http, crawl = crawl_observations_for(
            analysis.analysis_id,
            attempt.attempt_id,
        )
        succeeded = finish_attempt(
            attempt,
            target=AttemptState.SUCCEEDED,
            occurred_at=T0 + timedelta(seconds=2),
        )
        self.repository.complete_probe_attempt(
            attempt,
            succeeded,
            (http, crawl),
        )

        first = self.service.assess(http.observation_id, audit=self.audit)
        second = self.service.assess(http.observation_id, audit=self.audit)

        self.assertEqual("created", first.persistence.outcome)
        self.assertEqual("replayed", second.persistence.outcome)
        self.assertEqual(45, first.persistence.evidence_count)
        self.assertEqual(45, first.persistence.indicator_result_count)
        by_id = {
            item.indicator_id: item for item in first.derivation.indicator_results
        }
        self.assertEqual("pass", by_id["D03"].state.value)
        self.assertEqual("pass", by_id["D08"].state.value)
        self.assertEqual("fail", by_id["R01"].state.value)
        self.assertEqual("warning", by_id["R03"].state.value)
        events = self.connection.execute(
            """
            SELECT extra_json FROM audit_events
            WHERE action = 'job.http_metadata_assessment'
              AND resource_id = 'analysis-crawl-1'
            ORDER BY event_id
            """
        ).fetchall()
        self.assertEqual(2, len(events))
        self.assertNotIn("example.org", json.dumps(events))
        self.assertNotIn("must-not-be-logged", json.dumps(events))


if __name__ == "__main__":
    unittest.main()
