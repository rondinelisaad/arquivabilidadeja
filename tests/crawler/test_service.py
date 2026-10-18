from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from archivability.crawler import AuditedBoundedCrawl, CrawlError, CrawlLimits
from archivability.lifecycle import AnalysisOrchestrator
from archivability.methodology import load_methodology
from archivability.storage import (
    AuditContext,
    SqliteLifecycleRepository,
    apply_sqlite_migrations,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 18, 18, 0, tzinfo=timezone.utc)


class AuditedBoundedCrawlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        apply_sqlite_migrations(self.connection)
        event_ids = (f"crawl-event-{index}" for index in range(20))
        self.repository = SqliteLifecycleRepository(
            self.connection,
            clock=lambda: NOW,
            event_id_factory=event_ids.__next__,
        )
        orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: NOW,
            analysis_id_factory=lambda: "analysis-crawl-1",
            attempt_id_factory=lambda: "attempt-crawl-1",
        )
        analysis = orchestrator.create_analysis(
            subject_uri="https://example.org/private?token=not-for-audit",
            methodology=load_methodology(ROOT / "methodology" / "v0.1.0"),
        )
        orchestrator.start_attempt(analysis.analysis_id)

    def tearDown(self) -> None:
        self.connection.close()

    def test_records_sanitized_started_and_succeeded_events(self) -> None:
        crawl = AuditedBoundedCrawl(
            analysis_id="analysis-crawl-1",
            attempt_id="attempt-crawl-1",
            entry_uri="https://example.org/private?token=not-for-audit",
            recorder=self.repository,
            limits=CrawlLimits(max_depth=1, max_urls=2),
            audit=AuditContext(user_id="worker-opaque"),
        )
        entry = crawl.claim_next()
        assert entry is not None
        crawl.record_response(
            entry,
            status_code=200,
            response_bytes=10,
            elapsed_milliseconds=2,
        )
        self.assertIsNone(crawl.claim_next())

        rows = self.connection.execute(
            """
            SELECT result, extra_json FROM audit_events
            WHERE action = 'job.bounded_crawl'
            ORDER BY event_id
            """
        ).fetchall()
        self.assertEqual(["success", "success"], [row[0] for row in rows])
        serialized = json.dumps(rows)
        self.assertIn("started", serialized)
        self.assertIn("succeeded", serialized)
        self.assertNotIn("example.org", serialized)
        self.assertNotIn("not-for-audit", serialized)

    def test_abort_records_only_stable_error_code_and_closes_session(self) -> None:
        crawl = AuditedBoundedCrawl(
            analysis_id="analysis-crawl-1",
            attempt_id="attempt-crawl-1",
            entry_uri="https://example.org/",
            recorder=self.repository,
        )
        crawl.abort("CRAWL_POLICY_UNAVAILABLE")

        with self.assertRaises(CrawlError):
            crawl.claim_next()
        event = self.connection.execute(
            """
            SELECT result, extra_json FROM audit_events
            WHERE action = 'job.bounded_crawl' AND result = 'failure'
            """
        ).fetchone()
        assert event is not None
        self.assertEqual("failure", event[0])
        self.assertEqual("CRAWL_POLICY_UNAVAILABLE", json.loads(event[1])["error_code"])


if __name__ == "__main__":
    unittest.main()
