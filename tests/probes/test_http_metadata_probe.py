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

from archivability.crawler import CrawlLimits  # noqa: E402
from archivability.lifecycle import AnalysisOrchestrator  # noqa: E402
from archivability.methodology import load_methodology  # noqa: E402
from archivability.probes import (  # noqa: E402
    ApprovedTarget,
    HttpFetchResult,
    HttpMetadataProbe,
    HttpResponse,
    HttpTransportError,
    ProbeExecutionService,
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


class RoutingHttpFetcher:
    def fetch(self, context):
        path = context.target.normalized_uri.split("example.org", 1)[-1]
        if path == "/robots.txt":
            body = b"User-agent: *\nDisallow:\nSitemap: https://example.org/sitemap.xml\n"
            content_type = "text/plain"
        elif path == "/sitemap.xml":
            body = b"<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'><url><loc>https://example.org/a</loc></url></urlset>"
            content_type = "application/xml"
        else:
            body = b"<html><head><link rel='alternate' type='application/rss+xml' href='/feed.xml'></head><body><a href='/a'>A</a><img src='https://cdn.example.net/a.png'></body></html>"
            content_type = "text/html; charset=utf-8"
        return HttpFetchResult(
            target=context.target,
            response=HttpResponse(
                status_code=200,
                reason="OK",
                headers=(("content-type", content_type),),
                body=body,
            ),
            redirect_count=0,
            elapsed_milliseconds=12.5,
        )


class CrawlRoutingHttpFetcher:
    def __init__(self) -> None:
        self.crawled_paths: list[str] = []

    def fetch(self, context):
        path = context.target.normalized_uri.split("example.org", 1)[-1]
        if path == "/robots.txt":
            body = b"User-agent: arquivabilidade-ja\nDisallow: /blocked\n"
            content_type = "text/plain"
            status = 200
        elif path == "/sitemap.xml":
            body = b"<urlset/>"
            content_type = "application/xml"
            status = 200
        else:
            body = (
                b"<html><body><a href='/allowed'>Allowed</a>"
                b"<a href='/blocked'>Blocked</a>"
                b"<a href='https://cdn.example.net/external'>External</a></body></html>"
            )
            content_type = "text/html"
            status = 200
        return self._result(context, status, content_type, body)

    def fetch_same_origin(self, context):
        path = context.target.normalized_uri.split("example.org", 1)[-1]
        self.crawled_paths.append(path)
        if path == "/allowed":
            return self._result(
                context,
                200,
                "text/html",
                b"<html><body><a href='/deep'>Deep</a></body></html>",
            )
        if path == "/deep":
            return self._result(context, 404, "text/html", b"not found")
        raise AssertionError(f"unexpected crawl path: {path}")

    @staticmethod
    def _result(context, status, content_type, body):
        return HttpFetchResult(
            target=context.target,
            response=HttpResponse(
                status_code=status,
                reason="OK",
                headers=(("content-type", content_type),),
                body=body,
            ),
            redirect_count=0,
            elapsed_milliseconds=12.5,
        )


class UnreachableRobotsHttpFetcher(CrawlRoutingHttpFetcher):
    def fetch(self, context):
        path = context.target.normalized_uri.split("example.org", 1)[-1]
        if path == "/robots.txt":
            return self._result(context, 503, "text/plain", b"")
        return super().fetch(context)


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
            probe_id="probe-br-sp-http",
        )

        result = self.runner.run(self.request, probe)

        self.assertEqual(3, len(fetcher.contexts))
        observation = result.observations[0]
        self.assertEqual("observation-http-1", observation.observation_id)
        self.assertEqual("http_metadata", observation.kind)
        self.assertEqual("probe-br-sp-http", observation.probe_id)
        self.assertEqual("1.2", observation.payload_schema_version)
        self.assertEqual(200, observation.payload["status_code"])
        self.assertEqual(5 * 1024 * 1024, observation.payload["response_byte_limit"])
        self.assertEqual(1, observation.payload["redirect_count"])
        self.assertTrue(observation.payload["final_transport_secure"])
        self.assertEqual("text/html", observation.payload["content_type"])
        self.assertEqual("html", observation.payload["format_family"])
        self.assertIn("robots", observation.payload)
        self.assertIn("sitemap", observation.payload)
        self.assertTrue(observation.truncated)
        serialized = json.dumps(observation.to_dict())
        self.assertNotIn("body-must-not-be-persisted", serialized)
        self.assertNotIn("/final", serialized)

    def test_payload_schema_is_valid_json(self) -> None:
        directory = ROOT / "schemas" / "probes" / "v1"
        documents = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*.schema.json"))
        ]
        self.assertEqual(4, len(documents))
        self.assertTrue(all(document["type"] == "object" for document in documents))
        self.assertTrue(
            all(document["additionalProperties"] is False for document in documents)
        )

    def test_collects_bounded_discovery_aggregates_without_urls(self) -> None:
        observation = self.runner.run(
            self.request,
            HttpMetadataProbe(
                RoutingHttpFetcher(),
                clock=lambda: NOW,
                observation_id_factory=lambda: "observation-discovery-1",
            ),
        ).observations[0]

        self.assertEqual(3, observation.payload["html"]["link_count"])
        self.assertEqual(1, observation.payload["html"]["feed_link_count"])
        self.assertTrue(observation.payload["robots"]["parseable"])
        self.assertEqual(3, observation.payload["robots"]["item_count"])
        self.assertTrue(observation.payload["sitemap"]["parseable"])
        self.assertEqual(1, observation.payload["sitemap"]["item_count"])
        serialized = json.dumps(observation.to_dict()["payload"])
        self.assertNotIn("cdn.example.net", serialized)
        self.assertNotIn("/feed.xml", serialized)

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

    def test_integrates_robots_and_bounded_frontier_without_persisting_urls(self) -> None:
        fetcher = CrawlRoutingHttpFetcher()
        identifiers = iter(("observation-http-crawl", "observation-crawl-1"))
        times = iter((0.0, 0.001, 0.002, 0.003))
        probe = HttpMetadataProbe(
            fetcher,
            clock=lambda: NOW,
            monotonic_clock=lambda: next(times),
            observation_id_factory=identifiers.__next__,
            crawl_limits=CrawlLimits(max_depth=2, max_urls=10),
            crawl_event_recorder=self.repository,
        )

        result = ProbeExecutionService(
            runner=self.runner,
            repository=self.repository,
            clock=lambda: NOW,
        ).execute(self.request, probe)

        self.assertEqual(2, len(result.observations))
        crawl = result.observations[1]
        self.assertEqual("bounded_crawl", crawl.kind)
        self.assertEqual("1.0", crawl.payload_schema_version)
        summary = crawl.payload["summary"]
        self.assertEqual(3, summary["visited"])
        self.assertEqual(1, summary["broken_internal"])
        self.assertEqual(1, summary["discarded"]["external_origin"])
        self.assertEqual(1, summary["discarded"]["robots_policy"])
        self.assertEqual(["/allowed", "/deep"], fetcher.crawled_paths)
        serialized = json.dumps(crawl.to_dict()["payload"])
        self.assertNotIn("example.org", serialized)
        self.assertNotIn("must-not-be-logged", serialized)
        self.assertNotIn("/blocked", serialized)
        self.assertIsNotNone(self.repository.get_observation("observation-http-crawl"))
        self.assertIsNotNone(self.repository.get_observation("observation-crawl-1"))

        audit_rows = self.connection.execute(
            """
            SELECT extra_json FROM audit_events
            WHERE action = 'job.bounded_crawl'
            ORDER BY event_id
            """
        ).fetchall()
        self.assertEqual(2, len(audit_rows))
        self.assertNotIn("example.org", json.dumps(audit_rows))

    def test_unreachable_robots_fails_closed_before_child_connections(self) -> None:
        fetcher = UnreachableRobotsHttpFetcher()
        identifiers = iter(("observation-http-unreachable", "observation-crawl-2"))
        probe = HttpMetadataProbe(
            fetcher,
            clock=lambda: NOW,
            observation_id_factory=identifiers.__next__,
            crawl_limits=CrawlLimits(max_depth=2, max_urls=10),
            crawl_event_recorder=self.repository,
        )

        result = ProbeExecutionService(
            runner=self.runner,
            repository=self.repository,
            clock=lambda: NOW,
        ).execute(self.request, probe)

        crawl = result.observations[1].payload
        self.assertEqual("unreachable", crawl["robots"]["source_state"])
        self.assertEqual("disallow", crawl["robots"]["disposition"])
        self.assertEqual(1, crawl["summary"]["visited"])
        self.assertEqual(2, crawl["summary"]["discarded"]["robots_policy"])
        self.assertEqual([], fetcher.crawled_paths)


if __name__ == "__main__":
    unittest.main()
