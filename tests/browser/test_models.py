from __future__ import annotations

import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from archivability.browser import (  # noqa: E402
    BrowserBudgets,
    BrowserContractError,
    BrowserIsolationAttestation,
    BrowserProfile,
    BrowserRenderAggregate,
    BrowserRenderRequest,
    BrowserRenderResult,
    ExternalOriginEvidence,
)

NOW = datetime(2026, 10, 18, 23, 0, tzinfo=timezone.utc)
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def safe_isolation() -> BrowserIsolationAttestation:
    return BrowserIsolationAttestation(
        policy_id="browser-container-1",
        sandbox_enabled=True,
        running_as_root=False,
        read_only_root_filesystem=True,
        ephemeral_profile=True,
        host_mounts_present=False,
        secrets_available=False,
        docker_socket_available=False,
    )


def aggregate(**changes: object) -> BrowserRenderAggregate:
    values = {
        "raw_dom_hash": DIGEST_A,
        "rendered_dom_hash": DIGEST_B,
        "raw_url_set_hash": DIGEST_A,
        "rendered_url_set_hash": DIGEST_B,
        "raw_url_count": 3,
        "rendered_url_count": 7,
        "additional_url_count": 4,
        "request_count": 12,
        "internal_request_count": 8,
        "external_request_count": 4,
        "api_request_count": 2,
        "external_api_request_count": 1,
        "failed_request_count": 1,
        "blocked_request_count": 0,
        "transfer_bytes": 4096,
        "raw_dom_bytes": 1024,
        "rendered_dom_bytes": 2048,
        "console_event_count": 2,
        "elapsed_milliseconds": 3500,
        "termination_reason": "complete",
    }
    values.update(changes)
    return BrowserRenderAggregate(**values)  # type: ignore[arg-type]


class BrowserContractTests(unittest.TestCase):
    def test_default_contract_is_deterministic_and_immutable(self) -> None:
        request = BrowserRenderRequest(
            analysis_id="analysis-1",
            attempt_id="attempt-1",
            subject_uri="https://example.org/private?token=never-exported",
            requested_at=NOW,
        )

        self.assertEqual("UTC", request.profile.timezone_id)
        self.assertEqual("arquivabilidade-ja-browser/0.1", request.profile.user_agent)
        self.assertEqual("block", request.profile.service_workers)
        self.assertFalse(request.profile.cache_enabled)
        self.assertEqual(30_000, request.budgets.total_timeout_milliseconds)
        with self.assertRaises(FrozenInstanceError):
            request.profile.locale = "pt-BR"  # type: ignore[misc]

    def test_request_rejects_credentials_and_invalid_budget_combinations(self) -> None:
        with self.assertRaisesRegex(BrowserContractError, "without credentials"):
            BrowserRenderRequest(
                analysis_id="analysis-1",
                attempt_id="attempt-1",
                subject_uri="https://user:password@example.org/",
                requested_at=NOW,
            )
        with self.assertRaisesRegex(BrowserContractError, "phase budgets"):
            BrowserBudgets(
                navigation_timeout_milliseconds=20_000,
                observation_window_milliseconds=20_000,
                total_timeout_milliseconds=30_000,
            )
        with self.assertRaisesRegex(BrowserContractError, "deterministic"):
            BrowserProfile(locale="pt-BR")

    def test_isolation_attestation_fails_closed(self) -> None:
        for change in (
            {"sandbox_enabled": False},
            {"running_as_root": True},
            {"read_only_root_filesystem": False},
            {"ephemeral_profile": False},
            {"host_mounts_present": True},
            {"secrets_available": True},
            {"docker_socket_available": True},
        ):
            with self.subTest(change=change), self.assertRaises(BrowserContractError):
                replace(safe_isolation(), **change)

    def test_aggregate_rejects_inconsistent_or_over_budget_results(self) -> None:
        with self.assertRaisesRegex(BrowserContractError, "scope counts"):
            aggregate(
                request_count=2,
                internal_request_count=2,
                external_request_count=1,
            )
        with self.assertRaisesRegex(BrowserContractError, "dependency counts"):
            aggregate(
                dependency_graph_observed=True,
                external_origin_count=1,
                external_script_count=1,
            )
        with self.assertRaisesRegex(BrowserContractError, "transfer budget"):
            aggregate(transfer_bytes=5000).validate_against(
                BrowserBudgets(max_total_transfer_bytes=4096)
            )

    def test_external_origin_evidence_is_explicit_and_path_free(self) -> None:
        origin = ExternalOriginEvidence.from_value(
            {
                "origin": "https://cdn.example.org:8443",
                "request_count": 3,
                "api_count": 1,
                "script_count": 1,
                "stylesheet_count": 0,
                "image_count": 1,
                "font_count": 0,
                "frame_count": 0,
                "other_count": 0,
            }
        )
        result = aggregate(
            dependency_graph_observed=True,
            external_origin_count=1,
            external_api_request_count=1,
            external_script_count=1,
            external_image_count=1,
            external_other_count=1,
            external_origin_evidence=(origin,),
        )

        self.assertEqual("https://cdn.example.org:8443", origin.origin)
        self.assertEqual(origin.to_dict(), result.to_dict()["external_origin_evidence"][0])
        with self.assertRaisesRegex(BrowserContractError, "path or credentials"):
            replace(origin, origin="https://cdn.example.org/private?token=secret")
        with self.assertRaisesRegex(BrowserContractError, "canonical"):
            replace(origin, origin="https://CDN.EXAMPLE.ORG:443")
        with self.assertRaisesRegex(BrowserContractError, "counts are inconsistent"):
            replace(origin, request_count=4)

    def test_external_origin_evidence_must_be_complete_or_marked_truncated(self) -> None:
        origin = ExternalOriginEvidence(
            origin="https://cdn.example.org",
            request_count=4,
            api_count=1,
            script_count=1,
            stylesheet_count=0,
            image_count=1,
            font_count=0,
            frame_count=0,
            other_count=1,
        )
        with self.assertRaisesRegex(BrowserContractError, "is incomplete"):
            aggregate(
                dependency_graph_observed=True,
                external_origin_count=2,
                external_api_request_count=1,
                external_script_count=1,
                external_image_count=1,
                external_other_count=1,
                external_origin_evidence=(origin,),
            )
        aggregate(
            dependency_graph_observed=True,
            external_origin_count=2,
            external_api_request_count=1,
            external_script_count=1,
            external_image_count=1,
            external_other_count=1,
            external_origin_evidence=(origin,),
            external_origin_evidence_truncated=True,
        )

    def test_result_requires_matching_identity_safe_isolation_and_budgets(self) -> None:
        request = BrowserRenderRequest(
            analysis_id="analysis-1",
            attempt_id="attempt-1",
            subject_uri="https://example.org/",
            requested_at=NOW,
        )
        result = BrowserRenderResult(
            analysis_id="analysis-1",
            attempt_id="attempt-1",
            engine_name="chromium",
            engine_version="140.0.1",
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=4),
            isolation=safe_isolation(),
            aggregate=aggregate(),
        )

        result.validate_against(request)
        with self.assertRaisesRegex(BrowserContractError, "does not match"):
            replace(result, attempt_id="attempt-other").validate_against(request)


if __name__ == "__main__":
    unittest.main()
