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
    load_methodology_catalog,
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
        self.methodology = load_methodology(METHODOLOGY_PATH)
        self.methodology_catalog = load_methodology_catalog(METHODOLOGY_PATH)
        analysis_ids = iter(f"analysis-api-{index}" for index in range(1, 100))
        attempt_ids = iter(f"attempt-api-{index}" for index in range(1, 100))
        orchestrator = AnalysisOrchestrator(
            self.repository,
            clock=lambda: NOW,
            analysis_id_factory=lambda: next(analysis_ids),
            attempt_id_factory=lambda: next(attempt_ids),
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
            methodology=self.methodology,
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
            reports=AnalysisReportService(self.repository, self.methodology),
            probe=SuccessfulHttpProbe(),
            authorization=self.authorization,
            rate_limiter=self.rate_limiter,
            audit_recorder=self.repository,
            methodology_catalog=self.methodology_catalog,
            batch_id_factory=lambda: "batch-api-1",
        )
        self.principal = ApiPrincipal(
            user_id="user-opaque-1",
            session_id="session-opaque-1",
            permissions=frozenset({"analysis:read", "analysis:cancel"}),
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
        exported = self.api.export_analysis(
            created.body["analysis_id"],
            context=self.context("request-export-1"),
        )
        history = self.api.list_analyses(context=self.context("request-history-1"))
        site_history = self.api.list_site_history(
            "EXAMPLE.ORG.", context=self.context("request-site-history-1")
        )

        self.assertEqual(202, created.status_code)
        self.assertEqual("collection_queued", created.body["status"])
        self.assertEqual(
            "/v1/analyses/analysis-api-1/export.json", created.body["export_path"]
        )
        self.assertEqual(200, report.status_code)
        self.assertEqual("https://example.org/", report.body["target_origin"])
        self.assertEqual("no-store", report.headers["Cache-Control"])
        self.assertEqual(report.body, exported.body)
        self.assertEqual(200, history.status_code)
        self.assertEqual(1, len(history.body["items"]))
        self.assertEqual(200, site_history.status_code)
        self.assertEqual(1, len(site_history.body["items"]))
        self.assertEqual(1, site_history.body["strata"][0]["item_count"])
        self.assertEqual(
            "analysis-api-1", history.body["items"][0]["analysis_id"]
        )
        self.assertEqual(
            'attachment; filename="analysis-analysis-api-1.json"',
            exported.headers["Content-Disposition"],
        )
        event = self.connection.execute(
            """
            SELECT action, extra_json FROM audit_events
            WHERE resource_id = 'request-export-1'
            """
        ).fetchone()
        self.assertEqual("access.analysis_api_export", event[0])
        self.assertEqual("export", json.loads(event[1])["operation"])
        serialized = json.dumps(
            [
                created.to_dict(),
                report.to_dict(),
                exported.to_dict(),
                history.to_dict(),
                site_history.to_dict(),
            ]
        )
        self.assertNotIn("private", serialized)
        self.assertNotIn("must-not-be-logged", serialized)

    def test_site_history_rejects_invalid_domain_without_leaking_it(self) -> None:
        response = self.api.list_site_history(
            "invalid/domain",
            context=self.context("request-site-history-invalid"),
        )

        self.assertEqual(400, response.status_code)
        serialized = json.dumps(response.to_dict())
        self.assertNotIn("invalid/domain", serialized)

    def test_batch_submits_independent_durable_analyses(self) -> None:
        response = self.api.create_batch(
            {
                "subjects": [
                    {"subject_uri": "https://one.example/secret"},
                    {"subject_uri": "https://two.example/private"},
                ]
            },
            context=self.context("request-batch-1"),
        )

        self.assertEqual(202, response.status_code)
        self.assertEqual("batch-api-1", response.body["batch_id"])
        self.assertEqual(2, response.body["accepted_count"])
        self.assertEqual(0, response.body["failed_count"])
        self.assertEqual(
            2, self.connection.execute("SELECT count(*) FROM analyses").fetchone()[0]
        )
        self.assertEqual(
            2,
            self.connection.execute("SELECT count(*) FROM collection_jobs").fetchone()[
                0
            ],
        )
        serialized = json.dumps(response.to_dict())
        self.assertNotIn("one.example", serialized)
        self.assertNotIn("two.example", serialized)
        event = self.connection.execute(
            "SELECT action, extra_json FROM audit_events WHERE resource_id = ?",
            ("request-batch-1",),
        ).fetchone()
        assert event is not None
        self.assertEqual("access.analysis_api_batch_create", event[0])
        self.assertNotIn("subject", event[1])

    def test_creation_idempotency_replays_and_rejects_changed_input(self) -> None:
        first = self.api.create_analysis(
            {"subject_uri": SUBJECT},
            context=self.context("request-idempotency-1"),
            idempotency_key="opaque-request-key-1",
        )
        replay = self.api.create_analysis(
            {"subject_uri": SUBJECT},
            context=self.context("request-idempotency-2"),
            idempotency_key="opaque-request-key-1",
        )
        conflict = self.api.create_analysis(
            {"subject_uri": "https://different.example/"},
            context=self.context("request-idempotency-3"),
            idempotency_key="opaque-request-key-1",
        )

        self.assertEqual(202, first.status_code)
        self.assertFalse(first.body["replayed"])
        self.assertEqual(first.body["analysis_id"], replay.body["analysis_id"])
        self.assertTrue(replay.body["replayed"])
        self.assertEqual(409, conflict.status_code)
        self.assertEqual("IDEMPOTENCY_CONFLICT", conflict.body["error"]["code"])
        self.assertEqual(
            1, self.connection.execute("SELECT count(*) FROM analyses").fetchone()[0]
        )
        stored = self.connection.execute(
            "SELECT key_hash, request_hash FROM analysis_idempotency_keys"
        ).fetchone()
        self.assertEqual(64, len(stored[0]))
        self.assertEqual(64, len(stored[1]))
        self.assertNotIn("opaque-request-key-1", json.dumps(stored))

    def test_cancel_is_owner_authorized_idempotent_and_stops_queue_claim(self) -> None:
        created = self.api.create_analysis(
            {"subject_uri": SUBJECT}, context=self.context("request-cancel-create")
        )

        first = self.api.cancel_analysis(
            created.body["analysis_id"], context=self.context("request-cancel-1")
        )
        second = self.api.cancel_analysis(
            created.body["analysis_id"], context=self.context("request-cancel-2")
        )
        claimed = self.repository.claim_next_collection_job(
            worker_id="worker-after-cancel",
            occurred_at=NOW,
            lease_expires_at=datetime(2026, 10, 7, 5, 1, tzinfo=timezone.utc),
            audit=self.context("request-cancel-claim").audit_context(),
        )

        self.assertEqual(200, first.status_code)
        self.assertEqual("cancelled", first.body["state"])
        self.assertEqual(first.body, second.body)
        self.assertIsNone(claimed)
        actions = self.connection.execute(
            """
            SELECT action FROM audit_events
            WHERE resource_id IN (?, ?) ORDER BY resource_id
            """,
            ("request-cancel-1", "request-cancel-2"),
        ).fetchall()
        self.assertEqual(
            [("access.analysis_api_cancel",), ("access.analysis_api_cancel",)],
            actions,
        )

    def test_cancel_requires_explicit_permission(self) -> None:
        created = self.api.create_analysis(
            {"subject_uri": SUBJECT}, context=self.context("request-cancel-create-2")
        )
        principal = ApiPrincipal(
            user_id=self.principal.user_id,
            session_id=self.principal.session_id,
            permissions=frozenset({"analysis:read"}),
        )

        response = self.api.cancel_analysis(
            created.body["analysis_id"],
            context=ApiRequestContext(
                request_id="request-cancel-denied",
                ip_address="192.0.2.10",
                principal=principal,
            ),
        )

        self.assertEqual(403, response.status_code)

    def test_invalid_batch_is_rejected_before_any_write(self) -> None:
        response = self.api.create_batch(
            {
                "subjects": [
                    {"subject_uri": "https://valid.example/"},
                    {"subject_uri": "file:///etc/passwd"},
                ]
            },
            context=self.context("request-batch-invalid"),
        )

        self.assertEqual(400, response.status_code)
        self.assertEqual(
            0, self.connection.execute("SELECT count(*) FROM analyses").fetchone()[0]
        )

    def test_methodology_and_indicator_catalog_are_versioned_and_filterable(self) -> None:
        openapi = self.api.get_openapi(context=self.context("request-openapi-1"))
        methodology = self.api.get_methodology(
            context=self.context("request-methodology-1")
        )
        indicators = self.api.list_indicators(
            {"dimension": "discoverability", "version": "0.1.0"},
            context=self.context("request-indicators-1"),
        )
        indicator = self.api.get_indicator(
            "latency_breakdown",
            context=self.context("request-indicator-1"),
        )

        self.assertEqual(200, openapi.status_code)
        self.assertEqual("3.1.0", openapi.body["openapi"])
        self.assertIn("/v1/analyses", openapi.body["paths"])
        self.assertEqual(200, methodology.status_code)
        self.assertEqual("candidate", methodology.body["status"])
        self.assertEqual(45, methodology.body["indicator_count"])
        self.assertEqual(200, indicators.status_code)
        self.assertTrue(indicators.body["items"])
        self.assertTrue(
            all(
                item["dimension"] == "discoverability"
                for item in indicators.body["items"]
            )
        )
        self.assertEqual(200, indicator.status_code)
        self.assertEqual("N01", indicator.body["indicator"]["id"])
        self.assertEqual(
            [
                "methodology.read",
                "methodology.read",
                "methodology.read",
                "methodology.read",
            ],
            self.rate_limiter.operations[-4:],
        )
        actions = {
            row[0]
            for row in self.connection.execute(
                """
                SELECT action FROM audit_events
                WHERE resource_id IN (?, ?, ?, ?)
                """,
                (
                    "request-openapi-1",
                    "request-methodology-1",
                    "request-indicators-1",
                    "request-indicator-1",
                ),
            ).fetchall()
        }
        self.assertEqual(
            {
                "access.analysis_api_openapi_read",
                "access.analysis_api_methodology_read",
                "access.analysis_api_indicator_list",
                "access.analysis_api_indicator_read",
            },
            actions,
        )

    def test_indicator_filters_and_missing_indicator_fail_closed(self) -> None:
        invalid = self.api.list_indicators(
            {"classification": "invented"},
            context=self.context("request-indicators-invalid"),
        )
        missing = self.api.get_indicator(
            "missing-indicator",
            context=self.context("request-indicator-missing"),
        )

        self.assertEqual(400, invalid.status_code)
        self.assertEqual(404, missing.status_code)

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
            reports=AnalysisReportService(self.repository, self.methodology),
            probe=SuccessfulHttpProbe(),
            authorization=OwnershipAuthorizationPolicy(self.repository),
            rate_limiter=self.rate_limiter,
            audit_recorder=self.repository,
            methodology_catalog=self.methodology_catalog,
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
