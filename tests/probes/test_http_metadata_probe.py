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

from archivability.lifecycle import AnalysisOrchestrator  # noqa: E402
from archivability.methodology import load_methodology  # noqa: E402
from archivability.probes import (  # noqa: E402
    ApprovedTarget,
    HttpFetchResult,
    HttpMetadataProbe,
    HttpResponse,
    HttpTransportError,
    ProbeRequest,
    ProbeRunner,
    SsrfPolicy,
)
from archivability.storage import (  # noqa: E402
    SqliteLifecycleRepository,
    apply_sqlite_migrations,
)


NOW = datetime(2026, 10, 6, 21, 0, tzinfo=timezone.utc)
METHODOLOGY = ROOT / "methodology" / "v0.1.0"
PUBLIC_V4 = "93.184.216.34"
SUBJECT = "https://example.org/private?token=must-not-be-logged"


class StaticResolver:
    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        del hostname, port
        return (PUBLIC_V4,)


class FakeHttpFetcher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.contexts = []

    def fetch(self, context):
        self.contexts.append(context)
        if self.fail:
            raise HttpTransportError("must-not-be-logged")
        return HttpFetchResult(
            target=ApprovedTarget(
                normalized_uri="https://example.org/final",
                scheme="https",
                hostname="example.org",
                port=443,
                addresses=(PUBLIC_V4,),
            ),
            response=HttpResponse(
                status_code=200,
                reason="OK",
                headers=(
                    ("content-type", "text/html; charset=utf-8"),
                    ("cache-control", "max-age=60"),
                ),
                body=b"body-must-not-be-persisted",
                truncated=True,
            ),
            redirect_count=1,
        )


class HttpMetadataProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        apply_sqlite_migrations(self.connection)
        event_ids = iter(f"event-{index}" for index in range(100))
        self.repository = SqliteLifecycleRepository(
            self.connection,
            clock=lambda: NOW,
            event_id_factory=lambda: next(event_ids),
        )
        orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: NOW,
            analysis_id_factory=lambda: "analysis-1",
            attempt_id_factory=lambda: "attempt-1",
        )
        analysis = orchestrator.create_analysis(
            subject_uri=SUBJECT,
            methodology=load_methodology(METHODOLOGY),
        )
        orchestrator.start_attempt(analysis.analysis_id)
        self.request = ProbeRequest(
            analysis_id="analysis-1",
            attempt_id="attempt-1",
            subject_uri=SUBJECT,
            requested_at=NOW,
        )
        self.runner = ProbeRunner(
            policy=SsrfPolicy(),
            resolver=StaticResolver(),
            event_recorder=self.repository,
            authorizer=self.repository,
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_creates_provenanced_observation_without_response_body(self) -> None:
        fetcher = FakeHttpFetcher()
        probe = HttpMetadataProbe(
            fetcher,
            clock=lambda: NOW,
            observation_id_factory=lambda: "observation-http-1",
        )

        result = self.runner.run(self.request, probe)

        self.assertEqual(1, len(fetcher.contexts))
        observation = result.observations[0]
        self.assertEqual("observation-http-1", observation.observation_id)
        self.assertEqual("http_metadata", observation.kind)
        self.assertEqual("http-metadata", observation.probe_id)
        self.assertEqual(200, observation.payload["status_code"])
        self.assertEqual(1, observation.payload["redirect_count"])
        self.assertTrue(observation.payload["final_transport_secure"])
        self.assertTrue(observation.truncated)
        serialized = json.dumps(observation.to_dict())
        self.assertNotIn("body-must-not-be-persisted", serialized)
        self.assertNotIn("/final", serialized)

    def test_payload_schema_is_valid_json(self) -> None:
        schema = ROOT / "schemas" / "probes" / "v1" / "http-metadata.schema.json"
        document = json.loads(schema.read_text(encoding="utf-8"))
        self.assertEqual("object", document["type"])
        self.assertFalse(document["additionalProperties"])

    def test_transport_failure_is_audited_without_error_or_url(self) -> None:
        probe = HttpMetadataProbe(
            FakeHttpFetcher(fail=True),
            clock=lambda: NOW,
            observation_id_factory=lambda: "unused",
        )

        with self.assertRaises(HttpTransportError):
            self.runner.run(self.request, probe)

        event = self.connection.execute(
            """
            SELECT result, extra_json
            FROM audit_events
            WHERE action = 'job.probe'
            ORDER BY timestamp DESC, event_id DESC
            LIMIT 1
            """
        ).fetchone()
        self.assertEqual("failure", event[0])
        self.assertEqual(
            {
                "analysis_id": "analysis-1",
                "error_code": "HTTPTRANSPORTERROR",
                "probe_id": "http-metadata",
            },
            json.loads(event[1]),
        )
        self.assertNotIn("example.org", json.dumps(event))
        self.assertNotIn("must-not-be-logged", json.dumps(event))


if __name__ == "__main__":
    unittest.main()
