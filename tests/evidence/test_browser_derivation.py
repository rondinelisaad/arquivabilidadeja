from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from archivability.browser import (
    BrowserBudgets,
    BrowserIsolationAttestation,
    BrowserProfile,
    BrowserRenderAggregate,
)
from archivability.evidence import (
    BrowserDerivationError,
    Observation,
    derive_browser_indicators,
)
from archivability.methodology import ResultState, load_methodology

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 19, 4, 0, tzinfo=timezone.utc)
METHODOLOGY = load_methodology(ROOT / "methodology" / "v0.1.0")


def http_observation() -> Observation:
    return Observation.create(
        observation_id="http-browser-derivation",
        analysis_id="analysis-browser-derivation",
        attempt_id="attempt-browser-derivation",
        kind="http_metadata",
        subject_uri="https://example.org/",
        observed_at=NOW,
        probe_id="http-metadata",
        tool_name="test-http",
        tool_version="1.0",
        payload_schema_version="1.1",
        payload={"status_code": 200},
    )


def browser_observation(**changes: object) -> Observation:
    aggregate = {
        "raw_dom_hash": "a" * 64,
        "rendered_dom_hash": "a" * 64,
        "raw_url_set_hash": "b" * 64,
        "rendered_url_set_hash": "b" * 64,
        "raw_url_count": 3,
        "rendered_url_count": 3,
        "additional_url_count": 0,
        "request_count": 4,
        "internal_request_count": 4,
        "external_request_count": 0,
        "api_request_count": 0,
        "external_api_request_count": 0,
        "failed_request_count": 0,
        "blocked_request_count": 0,
        "transfer_bytes": 1024,
        "raw_dom_bytes": 512,
        "rendered_dom_bytes": 512,
        "console_event_count": 0,
        "elapsed_milliseconds": 1000,
        "termination_reason": "complete",
    }
    aggregate.update(changes)
    return Observation.create(
        observation_id="browser-derivation",
        analysis_id="analysis-browser-derivation",
        attempt_id="attempt-browser-derivation",
        kind="browser_render",
        subject_uri="https://example.org/",
        observed_at=NOW,
        probe_id="browser-render",
        tool_name="chromium",
        tool_version="140.0.1",
        payload_schema_version="1.0",
        payload={
            "budgets": BrowserBudgets().to_dict(),
            "profile": BrowserProfile().to_dict(),
            "isolation": BrowserIsolationAttestation(
                policy_id="browser-container-1",
                sandbox_enabled=True,
                running_as_root=False,
                read_only_root_filesystem=True,
                ephemeral_profile=True,
                host_mounts_present=False,
                secrets_available=False,
                docker_socket_available=False,
            ).to_dict(),
            "aggregate": BrowserRenderAggregate(**aggregate).to_dict(),
        },
    )


class BrowserDerivationTests(unittest.TestCase):
    def test_static_page_has_no_observed_browser_dependency(self) -> None:
        derived = derive_browser_indicators(
            METHODOLOGY,
            http_observation(),
            browser_observation(),
        )

        states = {item.indicator_id: item.state for item in derived.indicator_results}
        self.assertEqual(ResultState.PASS, states["D07"])
        self.assertEqual(ResultState.PASS, states["C01"])
        self.assertEqual(ResultState.NOT_APPLICABLE, states["C05"])
        self.assertEqual(ResultState.PASS, states["O05"])

    def test_spa_with_external_api_is_classified_by_explicit_thresholds(self) -> None:
        derived = derive_browser_indicators(
            METHODOLOGY,
            http_observation(),
            browser_observation(
                rendered_dom_hash="c" * 64,
                rendered_url_set_hash="d" * 64,
                raw_url_count=0,
                rendered_url_count=10,
                additional_url_count=10,
                request_count=10,
                internal_request_count=3,
                external_request_count=7,
                api_request_count=4,
                external_api_request_count=3,
                raw_dom_bytes=100,
                rendered_dom_bytes=1000,
            ),
        )

        states = {item.indicator_id: item.state for item in derived.indicator_results}
        self.assertEqual(ResultState.FAIL, states["D07"])
        self.assertEqual(ResultState.WARNING, states["C01"])
        self.assertEqual(ResultState.FAIL, states["C05"])
        self.assertEqual(ResultState.FAIL, states["O05"])
        self.assertTrue(
            all(
                item.data["threshold_basis"] == "project_hypothesis_v1"
                for item in derived.evidence
            )
        )

    def test_budget_termination_and_missing_render_are_unknown(self) -> None:
        bounded = derive_browser_indicators(
            METHODOLOGY,
            http_observation(),
            browser_observation(termination_reason="time_budget"),
        )
        missing = derive_browser_indicators(METHODOLOGY, http_observation(), None)

        self.assertTrue(
            all(item.state is ResultState.UNKNOWN for item in bounded.indicator_results)
        )
        self.assertTrue(
            all(item.state is ResultState.UNKNOWN for item in missing.indicator_results)
        )
        self.assertTrue(all(item.confidence == 0 for item in missing.indicator_results))

    def test_malformed_browser_payload_fails_closed(self) -> None:
        malformed = Observation.create(
            observation_id="browser-malformed",
            analysis_id="analysis-browser-derivation",
            attempt_id="attempt-browser-derivation",
            kind="browser_render",
            subject_uri="https://example.org/",
            observed_at=NOW,
            probe_id="browser-render",
            tool_name="chromium",
            tool_version="140.0.1",
            payload_schema_version="1.0",
            payload={"aggregate": {}},
        )

        with self.assertRaises(BrowserDerivationError):
            derive_browser_indicators(METHODOLOGY, http_observation(), malformed)


if __name__ == "__main__":
    unittest.main()
