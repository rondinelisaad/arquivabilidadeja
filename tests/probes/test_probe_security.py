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

from archivability.evidence import Observation  # noqa: E402
from archivability.lifecycle import AnalysisOrchestrator  # noqa: E402
from archivability.methodology import load_methodology  # noqa: E402
from archivability.probes import (  # noqa: E402
    ApprovedTarget,
    ProbeContext,
    ProbeRequest,
    ProbeRunner,
    ProbeValidationError,
    SsrfPolicy,
)
from archivability.storage import (  # noqa: E402
    AuditContext,
    IntegrityViolation,
    SqliteLifecycleRepository,
    apply_sqlite_migrations,
)


NOW = datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc)
METHODOLOGY = ROOT / "methodology" / "v0.1.0"
PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:4700:4700::1111"


class StaticResolver:
    def __init__(self, values: dict[str, tuple[str, ...]]) -> None:
        self.values = values
        self.calls: list[tuple[str, int]] = []

    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        self.calls.append((hostname, port))
        return self.values.get(hostname, ())


class SsrfPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = StaticResolver(
            {
                "example.org": (PUBLIC_V4, PUBLIC_V6),
                "xn--exmple-cua.org": (PUBLIC_V4,),
                "mixed.example": (PUBLIC_V4, "127.0.0.1"),
                "private.example": ("10.0.0.10",),
                "numeric.example": ("127.0.0.1",),
            }
        )
        self.policy = SsrfPolicy()

    def test_approves_and_canonicalizes_public_http_target(self) -> None:
        target = self.policy.approve("HTTPS://Example.Org/path?q=1", self.resolver)
        self.assertEqual("https://example.org/path?q=1", target.normalized_uri)
        self.assertEqual((PUBLIC_V4, PUBLIC_V6), target.addresses)
        self.assertEqual([PUBLIC_V4, PUBLIC_V6], target.to_dict()["addresses"])
        self.assertEqual([("example.org", 443)], self.resolver.calls)

    def test_idna_hostname_is_canonicalized_before_resolution(self) -> None:
        target = self.policy.approve("https://exämple.org/", self.resolver)
        self.assertEqual("xn--exmple-cua.org", target.hostname)

    def test_public_ip_literal_is_pinned_without_dns(self) -> None:
        target = self.policy.approve(f"https://{PUBLIC_V4}/", self.resolver)
        self.assertEqual((PUBLIC_V4,), target.addresses)
        self.assertEqual([], self.resolver.calls)

    def test_non_global_address_in_any_dns_answer_rejects_target(self) -> None:
        for uri in (
            "https://mixed.example/",
            "https://private.example/",
            "http://127.0.0.1/",
            "http://[::1]/",
            "http://169.254.169.254/latest/meta-data/",
            "http://100.64.0.1/",
        ):
            with self.subTest(uri=uri), self.assertRaises(ProbeValidationError):
                self.policy.approve(uri, self.resolver)

    def test_ambiguous_or_dangerous_urls_are_rejected(self) -> None:
        values = (
            "file:///etc/passwd",
            "https://user:password@example.org/",
            "https://example.org/#fragment",
            "https://example.org\\@private.example/",
            "https://example.org/ bad",
            "https://localhost/",
            "https://example.org:8443/",
            "https://example.org:99999/",
        )
        for uri in values:
            with self.subTest(uri=uri), self.assertRaises(ProbeValidationError):
                self.policy.approve(uri, self.resolver)

    def test_redirect_is_resolved_then_fully_revalidated(self) -> None:
        target = self.policy.approve("https://example.org/start", self.resolver)
        redirected = self.policy.approve_redirect(target, "/next", self.resolver)
        self.assertEqual("https://example.org/next", redirected.normalized_uri)
        with self.assertRaises(ProbeValidationError):
            self.policy.approve_redirect(
                target, "http://169.254.169.254/latest/meta-data/", self.resolver
            )

    def test_context_enforces_redirect_limit(self) -> None:
        request = ProbeRequest(
            analysis_id="analysis-1",
            attempt_id="attempt-1",
            subject_uri="https://example.org/",
            requested_at=NOW,
            max_redirects=1,
        )
        context = ProbeContext(
            request=request,
            target=self.policy.approve(request.subject_uri, self.resolver),
            policy=self.policy,
            resolver=self.resolver,
        )
        redirected = context.redirect("/next")
        with self.assertRaises(ProbeValidationError):
            redirected.redirect("/again")

    def test_resource_limits_are_bounded(self) -> None:
        for values in (
            {"timeout_seconds": 31},
            {"max_response_bytes": 10 * 1024 * 1024 + 1},
            {"max_redirects": 6},
            {"max_redirects": True},
        ):
            with self.subTest(values=values), self.assertRaises(ProbeValidationError):
                ProbeRequest(
                    analysis_id="analysis-1",
                    attempt_id="attempt-1",
                    subject_uri="https://example.org/",
                    requested_at=NOW,
                    **values,
                )

    def test_approved_target_cannot_be_constructed_with_private_ip(self) -> None:
        with self.assertRaises(ProbeValidationError):
            ApprovedTarget(
                normalized_uri="https://example.org/",
                scheme="https",
                hostname="example.org",
                port=443,
                addresses=("127.0.0.1",),
            )


class FakeProbe:
    probe_id = "http-metadata"
    tool_name = "fake-http-probe"
    tool_version = "1.0.0"

    def __init__(self, *, bad_provenance: bool = False) -> None:
        self.bad_provenance = bad_provenance
        self.contexts: list[ProbeContext] = []

    def execute(self, context: ProbeContext) -> tuple[Observation, ...]:
        self.contexts.append(context)
        request = context.request
        return (
            Observation.create(
                observation_id="observation-1",
                analysis_id=request.analysis_id,
                attempt_id=request.attempt_id,
                kind="http_metadata",
                subject_uri=request.subject_uri,
                observed_at=NOW,
                probe_id="wrong-probe" if self.bad_provenance else self.probe_id,
                tool_name=self.tool_name,
                tool_version=self.tool_version,
                payload_schema_version="1.0",
                payload={"status": 200},
            ),
        )


class InvalidMetadataProbe(FakeProbe):
    probe_id = "invalid probe id"


class ListReturningProbe(FakeProbe):
    def execute(self, context: ProbeContext) -> tuple[Observation, ...]:
        return list(super().execute(context))  # type: ignore[return-value]


class ProbeRunnerTests(unittest.TestCase):
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
            subject_uri="https://example.org/private?token=must-not-be-logged",
            methodology=load_methodology(METHODOLOGY),
        )
        orchestrator.start_attempt(analysis.analysis_id)
        self.resolver = StaticResolver({"example.org": (PUBLIC_V4,)})
        self.runner = ProbeRunner(
            policy=SsrfPolicy(),
            resolver=self.resolver,
            event_recorder=self.repository,
            authorizer=self.repository,
        )
        self.request = ProbeRequest(
            analysis_id="analysis-1",
            attempt_id="attempt-1",
            subject_uri="https://example.org/private?token=must-not-be-logged",
            requested_at=NOW,
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_runner_passes_only_approved_pinned_target_and_audits(self) -> None:
        probe = FakeProbe()
        result = self.runner.run(
            self.request,
            probe,
            audit=AuditContext(user_id="user-opaque-1"),
        )
        self.assertEqual((PUBLIC_V4,), result.target.addresses)
        self.assertEqual(result.target, probe.contexts[0].target)
        event = self.connection.execute(
            """
            SELECT action, result, user_id, extra_json
            FROM audit_events
            WHERE action = 'job.probe'
            """
        ).fetchone()
        self.assertEqual(("job.probe", "success", "user-opaque-1"), event[:3])
        serialized = json.dumps(event)
        self.assertNotIn("example.org", serialized)
        self.assertNotIn("must-not-be-logged", serialized)

    def test_runner_rejects_bad_observation_provenance_and_audits_failure(self) -> None:
        with self.assertRaises(ProbeValidationError):
            self.runner.run(self.request, FakeProbe(bad_provenance=True))
        event = self.connection.execute(
            """
            SELECT result, extra_json FROM audit_events
            WHERE action = 'job.probe'
            """
        ).fetchone()
        self.assertEqual("failure", event[0])
        extra = json.loads(event[1])
        self.assertEqual("PROBEVALIDATIONERROR", extra["error_code"])
        self.assertNotIn("subject_uri", extra)

    def test_runner_cannot_change_subject_for_existing_attempt(self) -> None:
        request = ProbeRequest(
            analysis_id="analysis-1",
            attempt_id="attempt-1",
            subject_uri="http://127.0.0.1/",
            requested_at=NOW,
        )
        probe = FakeProbe()
        with self.assertRaises(IntegrityViolation):
            self.runner.run(request, probe)
        self.assertEqual([], probe.contexts)

    def test_runner_bounds_plugin_metadata_and_output_container(self) -> None:
        for probe, expected_probe_id in (
            (InvalidMetadataProbe(), "invalid-probe"),
            (ListReturningProbe(), "http-metadata"),
        ):
            with self.subTest(probe=type(probe).__name__), self.assertRaises(
                ProbeValidationError
            ):
                self.runner.run(self.request, probe)
            event = self.connection.execute(
                """
                SELECT extra_json FROM audit_events
                WHERE action = 'job.probe'
                ORDER BY timestamp DESC, event_id DESC
                LIMIT 1
                """
            ).fetchone()
            self.assertEqual(expected_probe_id, json.loads(event[0])["probe_id"])


if __name__ == "__main__":
    unittest.main()
