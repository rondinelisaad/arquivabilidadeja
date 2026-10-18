from __future__ import annotations

import json
import unittest

from archivability.crawler import BoundedCrawlFrontier, CrawlError, CrawlLimits


class BoundedCrawlFrontierTests(unittest.TestCase):
    def test_fifo_frontier_tracks_same_origin_results_without_exposing_urls(
        self,
    ) -> None:
        frontier = BoundedCrawlFrontier(
            "https://example.org/start#ignored",
            limits=CrawlLimits(max_depth=2, max_urls=10),
        )
        entry = frontier.claim_next()
        assert entry is not None
        frontier.record_response(
            entry,
            status_code=200,
            response_bytes=100,
            elapsed_milliseconds=10,
            discovered_links=(
                "/a",
                "/b",
                "https://cdn.example.net/image.png",
                "/a#duplicate",
                "mailto:test@example.org",
            ),
        )

        first = frontier.claim_next()
        assert first is not None
        self.assertEqual("https://example.org/a", first.uri)
        frontier.record_response(
            first,
            status_code=200,
            response_bytes=50,
            elapsed_milliseconds=5,
            discovered_links=("/deep", "/start"),
        )

        second = frontier.claim_next()
        assert second is not None
        self.assertEqual("https://example.org/b", second.uri)
        frontier.record_response(
            second,
            status_code=404,
            response_bytes=20,
            elapsed_milliseconds=3,
        )

        deep = frontier.claim_next()
        assert deep is not None
        self.assertEqual(2, deep.depth)
        frontier.record_response(
            deep,
            status_code=200,
            response_bytes=30,
            elapsed_milliseconds=2,
            discovered_links=("/too-deep",),
        )
        self.assertIsNone(frontier.claim_next())

        summary = frontier.summary().to_dict()
        self.assertEqual("complete", summary["termination_reason"])
        self.assertEqual(4, summary["visited"])
        self.assertEqual(3, summary["successful"])
        self.assertEqual(1, summary["broken_internal"])
        self.assertEqual(2, summary["maximum_depth_reached"])
        self.assertEqual(200, summary["response_bytes"])
        self.assertEqual(20, summary["elapsed_milliseconds"])
        self.assertEqual(
            {
                "depth_limit": 1,
                "duplicate": 2,
                "external_origin": 1,
                "invalid": 1,
            },
            summary["discarded"],
        )
        serialized = json.dumps(summary)
        self.assertNotIn("example.org", serialized)
        self.assertNotIn("/deep", serialized)

    def test_url_budget_stops_expansion_but_drains_accepted_targets(self) -> None:
        frontier = BoundedCrawlFrontier(
            "https://example.org/",
            limits=CrawlLimits(max_depth=2, max_urls=2),
        )
        entry = frontier.claim_next()
        assert entry is not None
        frontier.record_response(
            entry,
            status_code=200,
            response_bytes=1,
            elapsed_milliseconds=1,
            discovered_links=("/a", "/b", "/c"),
        )
        accepted = frontier.claim_next()
        assert accepted is not None
        frontier.record_response(
            accepted,
            status_code=200,
            response_bytes=1,
            elapsed_milliseconds=1,
        )

        self.assertIsNone(frontier.claim_next())
        summary = frontier.summary().to_dict()
        self.assertEqual("url_budget", summary["termination_reason"])
        self.assertEqual(2, summary["enqueued"])
        self.assertEqual(2, summary["discarded"]["url_budget"])

    def test_remaining_byte_budget_is_exposed_and_enforced(self) -> None:
        frontier = BoundedCrawlFrontier(
            "https://example.org/",
            limits=CrawlLimits(
                max_response_bytes=60,
                max_total_bytes=100,
            ),
        )
        entry = frontier.claim_next()
        assert entry is not None
        self.assertEqual(60, entry.max_response_bytes)
        frontier.record_response(
            entry,
            status_code=200,
            response_bytes=60,
            elapsed_milliseconds=1,
            discovered_links=("/a", "/b"),
        )
        next_target = frontier.claim_next()
        assert next_target is not None
        self.assertEqual(40, next_target.max_response_bytes)
        with self.assertRaises(CrawlError):
            frontier.record_response(
                next_target,
                status_code=200,
                response_bytes=41,
                elapsed_milliseconds=1,
            )
        frontier.record_response(
            next_target,
            status_code=200,
            response_bytes=40,
            elapsed_milliseconds=1,
        )

        self.assertIsNone(frontier.claim_next())
        summary = frontier.summary().to_dict()
        self.assertEqual("byte_budget", summary["termination_reason"])
        self.assertEqual(1, summary["discarded"]["byte_budget"])

    def test_sequential_claims_and_failure_classes_are_validated(self) -> None:
        frontier = BoundedCrawlFrontier("https://example.org/")
        target = frontier.claim_next()
        assert target is not None
        with self.assertRaises(CrawlError):
            frontier.claim_next()
        with self.assertRaises(CrawlError):
            frontier.record_failure(
                target,
                failure_class="exception-text",
                elapsed_milliseconds=1,
            )
        frontier.record_failure(
            target,
            failure_class="timeout",
            elapsed_milliseconds=1,
        )
        self.assertIsNone(frontier.claim_next())
        self.assertEqual({"timeout": 1}, frontier.summary().to_dict()["failures"])

    def test_credentials_and_unsafe_characters_are_rejected(self) -> None:
        with self.assertRaises(CrawlError):
            BoundedCrawlFrontier("https://user:password@example.org/")
        with self.assertRaises(CrawlError):
            BoundedCrawlFrontier("https://example.org/unsafe path")

    def test_policy_discard_does_not_count_as_network_visit(self) -> None:
        frontier = BoundedCrawlFrontier("https://example.org/")
        target = frontier.claim_next()
        assert target is not None

        frontier.discard(target, reason="robots_policy")

        self.assertIsNone(frontier.claim_next())
        summary = frontier.summary().to_dict()
        self.assertEqual(0, summary["visited"])
        self.assertEqual({"robots_policy": 1}, summary["discarded"])


if __name__ == "__main__":
    unittest.main()
