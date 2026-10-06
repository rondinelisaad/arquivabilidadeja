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

from archivability import (  # noqa: E402
    AnalysisApi,
    AnalysisOrchestrator,
    AnalysisReportService,
    ApiPrincipal,
    ApiRequestContext,
    HttpAssessmentQueueService,
    HttpAssessmentWorkflow,
    OwnershipAuthorizationPolicy,
    ProbeExecutionService,
    ProbeRunner,
    SqliteAssessmentJobRepository,
    SsrfPolicy,
    apply_sqlite_migrations,
    load_methodology,
)
from tests.application.test_http_workflow import (  # noqa: E402
    StaticResolver,
    SuccessfulHttpProbe,
)


NOW = datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc)
SUBJECT = "https://example.org/private?token=must-not-be-logged"
METHODOLOGY_PATH = ROOT / "methodology" / "v0.1.0"


class Authorization:
    def __init__(self, *, create: bool = True, read: bool = True) -> None:
        self.create = create
        self.read = read
        self.raise_error = False

    def can_create_analysis(self, principal: ApiPrincipal) -> bool:
        del principal
        if self.raise_error:
            raise RuntimeError("authorization backend details")
        return self.create

    def can_read_analysis(self, principal: ApiPrincipal, analysis_id: str) -> bool:
        del principal, analysis_id
        if self.raise_error:
            raise RuntimeError("authorization backend details")
        return self.read


class RateLimiter:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.operations: list[str] = []

    def allow(self, principal: ApiPrincipal, operation: str) -> bool:
        del principal
        self.operations.append(operation)
        return self.allowed


class AnalysisApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        apply_sqlite_migrations(self.connection)
        event_ids = iter(f"event-{index:03d}" for index in range(600))
        self.repository = SqliteAssessmentJobRepository(
            self.connection,
            clock=lambda: NOW,
            event_id_factory=lambda: next(event_ids),
        )
        methodology = load_methodology(METHODOLOGY_PATH)
        orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: NOW,
            analysis_id_factory=lambda: "analysis-api-1",
            attempt_id_factory=lambda: "attempt-api-1",
        )
        runner = ProbeRunner(
            policy=SsrfPolicy(),
            resolver=StaticResolver(),
            event_recorder=self.repository,
            authorizer=self.repository,
        )
        execution = ProbeExecutionService(
            runner=runner,
            repository=self.repository,
            clock=lambda: NOW,
        )
        queue = HttpAssessmentQueueService(
            repository=self.repository,
            clock=lambda: NOW,
            job_id_factory=lambda: "job-api-1",
        )
        self.workflow = HttpAssessmentWorkflow(
            methodology=methodology,
            repository=self.repository,
            orchestrator=orchestrator,
            probe_execution=execution,
            queue=queue,
            clock=lambda: NOW,
        )
        self.authorization = Authorization()
        self.rate_limiter = RateLimiter()
        self.api = AnalysisApi(
            workflow=self.workflow,
            reports=AnalysisReportService(self.repository),
            probe=SuccessfulHttpProbe(),
            authorization=self.authorization,
            rate_limiter=self.rate_limiter,
            audit_recorder=self.repository,
        )
        self.principal = ApiPrincipal(
            user_id="user-opaque-1",
            session_id="session-opaque-1",
        )

    def tearDown(self) -> None:
        self.connection.close()

    def context(
        self, request_id: str, *, authenticated: bool = True
    ) -> ApiRequestContext:
        return ApiRequestContext(
            request_id=request_id,
            ip_address="192.0.2.10",
            principal=self.principal if authenticated else None,
        )

    def test_unauthenticated_request_is_rejected_before_workflow(self) -> None:
        response = self.api.create_analysis(
            {"subject_uri": SUBJECT},
            context=self.context("request-1", authenticated=False),
        )

        self.assertEqual(401, response.status_code)
        self.assertEqual("AUTHENTICATION_REQUIRED", response.body["error"]["code"])
        self.assertEqual(
            0,
            self.connection.execute("SELECT count(*) FROM analyses").fetchone()[0],
        )
        event = self.connection.execute(
            "SELECT action, result, user_id FROM audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(("access.denied", "unauthorized", None), event)

    def test_authorization_denial_does_not_reveal_analysis_existence(self) -> None:
        created = self.api.create_analysis(
            {"subject_uri": SUBJECT},
            context=self.context("request-create-before-denial"),
        )
        self.authorization.read = False
        existing = self.api.get_analysis(
            created.body["analysis_id"],
            context=self.context("request-2"),
        )
        missing = self.api.get_analysis(
            "missing-analysis",
            context=self.context("request-3"),
        )

        self.assertEqual(403, existing.status_code)
        self.assertEqual(existing.body["error"], missing.body["error"])
        report_reads = self.connection.execute(
            "SELECT count(*) FROM audit_events WHERE action = 'access.analysis_report'"
        ).fetchone()[0]
        self.assertEqual(0, report_reads)

    def test_rate_limit_runs_before_input_and_workflow(self) -> None:
        self.rate_limiter.allowed = False
        response = self.api.create_analysis(
            {"unexpected": SUBJECT},
            context=self.context("request-4"),
        )

        self.assertEqual(429, response.status_code)
        self.assertEqual("RATE_LIMITED", response.body["error"]["code"])
        self.assertEqual(["analysis.create"], self.rate_limiter.operations)
        self.assertEqual(
            0,
            self.connection.execute("SELECT count(*) FROM analyses").fetchone()[0],
        )

    def test_authorization_backend_failure_is_closed_and_sanitized(self) -> None:
        self.authorization.raise_error = True

        response = self.api.create_analysis(
            {"subject_uri": SUBJECT},
            context=self.context("request-auth-failure"),
        )

        self.assertEqual(500, response.status_code)
        self.assertEqual("INTERNAL_ERROR", response.body["error"]["code"])
        self.assertNotIn("backend", json.dumps(response.to_dict()))
        self.assertEqual(
            0,
            self.connection.execute("SELECT count(*) FROM analyses").fetchone()[0],
        )

    def test_create_and_read_return_sanitized_no_store_responses(self) -> None:
        created = self.api.create_analysis(
            {"subject_uri": SUBJECT},
            context=self.context("request-5"),
        )
        report = self.api.get_analysis(
            created.body["analysis_id"],
            context=self.context("request-6"),
        )

        self.assertEqual(202, created.status_code)
        self.assertEqual("assessment_queued", created.body["status"])
        self.assertEqual(200, report.status_code)
        self.assertEqual("https://example.org/", report.body["target_origin"])
        self.assertEqual("no-store", report.headers["Cache-Control"])
        serialized = json.dumps([created.to_dict(), report.to_dict()])
        self.assertNotIn("private", serialized)
        self.assertNotIn("must-not-be-logged", serialized)

    def test_creation_persists_immutable_owner_and_audits_without_duplication(
        self,
    ) -> None:
        created = self.api.create_analysis(
            {"subject_uri": SUBJECT},
            context=self.context("request-owner-create"),
        )

        analysis_id = created.body["analysis_id"]
        owner = self.connection.execute(
            "SELECT owner_user_id FROM analysis_ownership WHERE analysis_id = ?",
            (analysis_id,),
        ).fetchone()
        event = self.connection.execute(
            """
            SELECT action, user_id, after_json
            FROM audit_events
            WHERE resource = 'analysis_ownership' AND resource_id = ?
            """,
            (analysis_id,),
        ).fetchone()
        self.assertEqual((self.principal.user_id,), owner)
        self.assertEqual("permission.analysis_owner_assign", event[0])
        self.assertEqual(self.principal.user_id, event[1])
        self.assertEqual({"owner_bound": True}, json.loads(event[2]))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE analysis_ownership SET owner_user_id = 'other' WHERE analysis_id = ?",
                (analysis_id,),
            )
        self.connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "DELETE FROM analysis_ownership WHERE analysis_id = ?",
                (analysis_id,),
            )
        self.connection.rollback()

    def test_owner_policy_blocks_idor_and_allows_explicit_global_reader(self) -> None:
        created = self.api.create_analysis(
            {"subject_uri": SUBJECT},
            context=self.context("request-owner-policy-create"),
        )
        secure_api = AnalysisApi(
            workflow=self.workflow,
            reports=AnalysisReportService(self.repository),
            probe=SuccessfulHttpProbe(),
            authorization=OwnershipAuthorizationPolicy(self.repository),
            rate_limiter=self.rate_limiter,
            audit_recorder=self.repository,
        )
        analysis_id = created.body["analysis_id"]
        owner_context = ApiRequestContext(
            request_id="request-owner-read",
            ip_address="192.0.2.10",
            principal=ApiPrincipal(
                user_id=self.principal.user_id,
                session_id="session-owner-read",
                permissions=frozenset({"analysis:read"}),
            ),
        )
        other_context = ApiRequestContext(
            request_id="request-other-read",
            ip_address="192.0.2.11",
            principal=ApiPrincipal(
                user_id="user-opaque-2",
                session_id="session-other-read",
                permissions=frozenset({"analysis:read"}),
            ),
        )
        auditor_context = ApiRequestContext(
            request_id="request-auditor-read",
            ip_address="192.0.2.12",
            principal=ApiPrincipal(
                user_id="auditor-opaque-1",
                session_id="session-auditor-read",
                permissions=frozenset({"analysis:read:any"}),
            ),
        )

        owned = secure_api.get_analysis(analysis_id, context=owner_context)
        forbidden = secure_api.get_analysis(analysis_id, context=other_context)
        hidden_missing = secure_api.get_analysis(
            "missing-analysis", context=other_context
        )
        audited = secure_api.get_analysis(analysis_id, context=auditor_context)

        self.assertEqual(200, owned.status_code)
        self.assertEqual(403, forbidden.status_code)
        self.assertEqual(forbidden.body["error"], hidden_missing.body["error"])
        self.assertEqual(200, audited.status_code)

    def test_owner_insert_failure_rolls_back_analysis(self) -> None:
        self.connection.executescript(
            """
            CREATE TRIGGER reject_test_owner
            BEFORE INSERT ON analysis_ownership BEGIN
                SELECT RAISE(ABORT, 'test owner rejection');
            END;
            """
        )

        response = self.api.create_analysis(
            {"subject_uri": SUBJECT},
            context=self.context("request-owner-rollback"),
        )

        self.assertEqual(500, response.status_code)
        self.assertEqual(
            0, self.connection.execute("SELECT count(*) FROM analyses").fetchone()[0]
        )
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM analysis_ownership"
            ).fetchone()[0],
        )

    def test_invalid_and_missing_requests_use_stable_errors(self) -> None:
        invalid = self.api.create_analysis(
            {"subject_uri": "not-a-url", "extra": True},
            context=self.context("request-7"),
        )
        missing = self.api.get_analysis(
            "missing-analysis",
            context=self.context("request-8"),
        )

        self.assertEqual(400, invalid.status_code)
        self.assertEqual("INVALID_REQUEST", invalid.body["error"]["code"])
        self.assertEqual(404, missing.status_code)
        self.assertEqual("NOT_FOUND", missing.body["error"]["code"])
        events = self.connection.execute(
            """
            SELECT action, result, resource_id, extra_json
            FROM audit_events
            WHERE resource = 'analysis_api'
            ORDER BY event_id
            """
        ).fetchall()
        self.assertEqual(2, len(events))
        serialized = json.dumps(events)
        self.assertNotIn("not-a-url", serialized)
        self.assertNotIn("must-not-be-logged", serialized)


if __name__ == "__main__":
    unittest.main()
