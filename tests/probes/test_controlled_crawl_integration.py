from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from archivability.crawler import CrawlLimits  # noqa: E402
from archivability.lifecycle import AnalysisOrchestrator  # noqa: E402
from archivability.methodology import load_methodology  # noqa: E402
from archivability.probes import (  # noqa: E402
    HttpMetadataProbe,
    PinnedHttpClient,
    ProbeContext,
    ProbeExecutionService,
    ProbeRequest,
    ProbeRunner,
    ProbeValidationError,
    SsrfPolicy,
)
from archivability.storage import (  # noqa: E402
    SqliteLifecycleRepository,
    apply_sqlite_migrations,
)
from tests.fixtures.sites import ControlledSite, FixtureResponse  # noqa: E402

NOW = datetime(2026, 10, 18, 18, 0, tzinfo=timezone.utc)
METHODOLOGY_PATH = ROOT / "methodology" / "v0.1.0"


class ControlledCrawlIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        apply_sqlite_migrations(self.connection)
        event_ids = iter(f"fixture-event-{index}" for index in range(200))
        self.repository = SqliteLifecycleRepository(
            self.connection,
            clock=lambda: NOW,
            event_id_factory=lambda: next(event_ids),
        )
        self.methodology = load_methodology(METHODOLOGY_PATH)

    def tearDown(self) -> None:
        self.connection.close()

    def test_real_http_crawl_respects_robots_and_same_origin_redirects(self) -> None:
        routes = {
            "/": FixtureResponse(
                body=(
                    b"<a href='/allowed'>allowed</a>"
                    b"<a href='/blocked'>blocked</a>"
                    b"<a href='/redirect'>redirect</a>"
                    b"<a href='https://outside.example/item'>outside</a>"
                ),
                content_type="text/html",
            ),
            "/robots.txt": FixtureResponse(
                body=b"User-agent: arquivabilidade-ja\nDisallow: /blocked\n"
            ),
            "/sitemap.xml": FixtureResponse(
                body=b"<urlset/>", content_type="application/xml"
            ),
            "/allowed": FixtureResponse(
                body=b"<a href='/deep'>deep</a>", content_type="text/html"
            ),
            "/redirect": FixtureResponse(
                status=302,
                headers=(("Location", "/final"),),
            ),
            "/final": FixtureResponse(body=b"final", content_type="text/html"),
            "/deep": FixtureResponse(status=404, body=b"missing"),
        }
        with ControlledSite(routes) as site:
            outcome = self._execute(site, CrawlLimits(max_depth=2, max_urls=10))

        self.assertTrue(outcome.succeeded, outcome.failure_code)
        self.assertEqual(2, len(outcome.observations))
        crawl = outcome.observations[1].payload["summary"]
        self.assertEqual(4, crawl["visited"])
        self.assertEqual(1, crawl["broken_internal"])
        self.assertEqual(1, crawl["discarded"]["robots_policy"])
        self.assertEqual(1, crawl["discarded"]["external_origin"])
        self.assertNotIn("/blocked", site.requests)
        self.assertIn("/redirect", site.requests)
        self.assertIn("/final", site.requests)
        self.assertTrue(
            all(
                headers.get("user-agent") == PinnedHttpClient.user_agent
                for headers in site.request_headers
            )
        )

    def test_real_http_crawl_stops_at_total_byte_budget(self) -> None:
        routes = {
            "/": FixtureResponse(
                body=b"<a href='/large'>large</a><a href='/later'>later</a>",
                content_type="text/html",
            ),
            "/robots.txt": FixtureResponse(body=b"User-agent: *\nAllow: /\n"),
            "/sitemap.xml": FixtureResponse(status=404),
            "/large": FixtureResponse(body=b"x" * 1024),
            "/later": FixtureResponse(body=b"should not be requested"),
        }
        limits = CrawlLimits(
            max_depth=1,
            max_urls=10,
            max_response_bytes=128,
            max_total_bytes=160,
        )
        with ControlledSite(routes) as site:
            outcome = self._execute(site, limits)

        crawl_observation = outcome.observations[1]
        summary = crawl_observation.payload["summary"]
        self.assertEqual("byte_budget", summary["termination_reason"])
        self.assertEqual(160, summary["response_bytes"])
        self.assertTrue(crawl_observation.truncated)
        self.assertIn("/large", site.requests)
        self.assertNotIn("/later", site.requests)

    def test_real_http_client_blocks_cross_origin_redirect_before_second_socket(self) -> None:
        routes = {
            "/external": FixtureResponse(
                status=302,
                headers=(("Location", "http://other.test/final"),),
            ),
        }
        with ControlledSite(routes) as site:
            location = f"http://other.test:{site.port}/final"
            site.set_route(
                "/external",
                FixtureResponse(
                    status=302,
                    headers=(("Location", location),),
                ),
            )
            policy = SsrfPolicy(allowed_ports=frozenset({site.port}))
            request = ProbeRequest(
                analysis_id="analysis-redirect",
                attempt_id="attempt-redirect",
                subject_uri=site.url("/external"),
                requested_at=NOW,
                timeout_seconds=2,
            )
            context = ProbeContext(
                request=request,
                target=policy.approve(request.subject_uri, site),
                policy=policy,
                resolver=site,
            )
            client = PinnedHttpClient(socket_factory=site.socket_factory)

            with self.assertRaisesRegex(ProbeValidationError, "cross-origin"):
                client.fetch_same_origin(context)

            self.assertEqual(("/external",), site.requests)
            self.assertEqual(1, len(site.socket_calls))

    def _execute(self, site: ControlledSite, limits: CrawlLimits):
        identifiers = iter(("analysis-fixture", "attempt-fixture"))
        orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: NOW,
            analysis_id_factory=lambda: next(identifiers),
            attempt_id_factory=lambda: next(identifiers),
        )
        analysis = orchestrator.create_analysis(
            subject_uri=site.url("/"),
            methodology=self.methodology,
        )
        attempt = orchestrator.start_attempt(analysis.analysis_id)
        policy = SsrfPolicy(allowed_ports=frozenset({site.port}))
        runner = ProbeRunner(
            policy=policy,
            resolver=site,
            event_recorder=self.repository,
            authorizer=self.repository,
        )
        probe = HttpMetadataProbe(
            PinnedHttpClient(socket_factory=site.socket_factory),
            clock=lambda: NOW + timedelta(seconds=1),
            crawl_limits=limits,
            crawl_event_recorder=self.repository,
        )
        return ProbeExecutionService(
            runner=runner,
            repository=self.repository,
            clock=lambda: NOW + timedelta(seconds=2),
        ).execute(
            ProbeRequest(
                analysis_id=analysis.analysis_id,
                attempt_id=attempt.attempt_id,
                subject_uri=analysis.subject_uri,
                requested_at=NOW,
                timeout_seconds=2,
                max_response_bytes=limits.max_response_bytes,
                max_redirects=3,
            ),
            probe,
        )


if __name__ == "__main__":
    unittest.main()
