from __future__ import annotations

import json
import unittest

from archivability.crawler import RobotsError, RobotsPolicy, RobotsRules


class RobotsRulesTests(unittest.TestCase):
    def test_exact_groups_are_merged_and_override_wildcard(self) -> None:
        rules = RobotsRules.parse(
            b"""
            User-agent: *
            Disallow: /

            User-agent: Arquivabilidade-JA
            Disallow: /private

            User-agent: ARQUIVABILIDADE-ja
            Allow: /private/public
            """
        )

        private = rules.decide("https://example.org/private/document")
        public = rules.decide("https://example.org/private/public/document")
        other = rules.decide("https://example.org/other")

        self.assertFalse(private.allowed)
        self.assertTrue(public.allowed)
        self.assertTrue(other.allowed)
        self.assertEqual(2, private.matched_group_count)
        self.assertEqual(2, private.applicable_rule_count)

    def test_longest_match_and_allow_tie_win(self) -> None:
        rules = RobotsRules.parse(
            b"""
            User-agent: *
            Disallow: /folder
            Disallow: /folder/same
            Allow: /folder/same
            Allow: /folder/open
            """
        )

        self.assertFalse(rules.decide("https://example.org/folder/closed").allowed)
        self.assertTrue(rules.decide("https://example.org/folder/open/page").allowed)
        tied = rules.decide("https://example.org/folder/same")
        self.assertTrue(tied.allowed)
        self.assertEqual("allow", tied.matched_rule)

    def test_wildcard_end_anchor_query_and_implicit_robots_allow(self) -> None:
        rules = RobotsRules.parse(
            b"""
            User-agent: *
            Disallow: /*.gif$
            Disallow: /search?private=*
            Disallow: /robots.txt
            """
        )

        self.assertFalse(rules.decide("https://example.org/image.gif").allowed)
        self.assertTrue(rules.decide("https://example.org/image.gif?size=large").allowed)
        self.assertFalse(rules.decide("https://example.org/search?private=yes").allowed)
        self.assertTrue(rules.decide("https://example.org/robots.txt").allowed)

    def test_percent_encoding_and_unicode_are_normalized(self) -> None:
        rules = RobotsRules.parse(
            """
            User-agent: *
            Disallow: /caf%C3%A9
            Disallow: /plain/baz
            """.encode()
        )

        self.assertFalse(rules.decide("https://example.org/caf\u00e9").allowed)
        self.assertFalse(rules.decide("https://example.org/plain/%62%61%7A").allowed)

    def test_invalid_lines_do_not_hide_parseable_rules(self) -> None:
        rules = RobotsRules.parse(
            b"""
            Disallow: /outside
            User-agent: invalid token
            Disallow: /ignored
            User-agent: *
            Unknown: value
            Disallow: /blocked
            """
        )

        self.assertFalse(rules.decide("https://example.org/blocked").allowed)
        self.assertTrue(rules.decide("https://example.org/outside").allowed)
        self.assertTrue(rules.decide("https://example.org/ignored").allowed)

    def test_decision_is_versioned_and_does_not_expose_uri_or_rule(self) -> None:
        rules = RobotsRules.parse(b"User-agent: *\nDisallow: /secret\n")
        decision = rules.decide("https://example.org/secret?token=not-persisted").to_dict()

        self.assertEqual("rfc9309-respect-1", decision["policy_id"])
        self.assertEqual("rfc9309-1", decision["parser_version"])
        self.assertEqual("parsed", decision["source_state"])
        serialized = json.dumps(decision)
        self.assertNotIn("example.org", serialized)
        self.assertNotIn("not-persisted", serialized)
        self.assertNotIn("/secret", serialized)

    def test_http_unavailable_allows_and_unreachable_disallows(self) -> None:
        unavailable = RobotsRules.from_http(404)
        unreachable = RobotsRules.from_http(503)
        network_failure = RobotsRules.unreachable()

        self.assertTrue(unavailable.decide("https://example.org/page").allowed)
        self.assertEqual(
            "unavailable",
            unavailable.decide("https://example.org/page").source_state,
        )
        self.assertFalse(unreachable.decide("https://example.org/page").allowed)
        self.assertFalse(network_failure.decide("https://example.org/page").allowed)
        self.assertTrue(unreachable.decide("https://example.org/robots.txt").allowed)
        with self.assertRaises(RobotsError):
            RobotsRules.from_http(302)

    def test_limits_encoding_policy_and_uri_are_validated(self) -> None:
        with self.assertRaises(RobotsError):
            RobotsPolicy(product_token="invalid token")
        with self.assertRaises(RobotsError):
            RobotsRules.parse(b"x" * (512 * 1024 + 1))
        with self.assertRaises(RobotsError):
            RobotsRules.parse(b"\xff")

        rules = RobotsRules.parse(b"")
        with self.assertRaises(RobotsError):
            rules.decide("file:///tmp/private")
        with self.assertRaises(RobotsError):
            rules.decide("https://user:password@example.org/")
        with self.assertRaises(RobotsError):
            rules.decide("https://example.org/unsafe path")
        with self.assertRaises(RobotsError):
            rules.decide("https://example.org:0/")


if __name__ == "__main__":
    unittest.main()
