from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from archivability.evidence.models import Evidence, EvidenceSource, Observation
from archivability.lifecycle.models import AnalysisState, Attempt, AttemptState
from archivability.methodology.models import IndicatorResult, ResultState
from archivability.storage.audit import AuditContext
from archivability.storage.errors import IntegrityViolation, PersistenceError
from archivability.storage.postgresql_lifecycle import PostgreSqlLifecycleRepository


class PostgreSqlEvidenceRepository(PostgreSqlLifecycleRepository):
    """PostgreSQL evidence and probe persistence with verified provenance."""

    def save_observation(
        self, value: Observation, *, audit: AuditContext = AuditContext()
    ) -> None:
        self._execute_write(
            lambda: self._insert_observation(value),
            resource="observation",
            resource_id=value.observation_id,
            audit=audit,
            action="data.create",
            before=None,
            after={
                "analysis_id": value.analysis_id,
                "content_hash": value.content_hash,
            },
        )

    def save_evidence(
        self, value: Evidence, *, audit: AuditContext = AuditContext()
    ) -> None:
        self._execute_write(
            lambda: self._insert_evidence(value),
            resource="evidence",
            resource_id=value.evidence_id,
            audit=audit,
            action="data.create",
            before=None,
            after={
                "analysis_id": value.analysis_id,
                "indicator_id": value.indicator_id,
                "content_hash": value.content_hash,
            },
        )

    def save_indicator_result(
        self, value: IndicatorResult, *, audit: AuditContext = AuditContext()
    ) -> None:
        self._execute_write(
            lambda: self._insert_indicator_result(value),
            resource="indicator_result",
            resource_id=f"{value.analysis_id}:{value.indicator_id}",
            audit=audit,
            action="data.create",
            before=None,
            after={
                "analysis_id": value.analysis_id,
                "indicator_id": value.indicator_id,
                "state": value.state.value,
            },
        )

    def save_chain(
        self,
        *,
        observations: Sequence[Observation],
        evidence: Sequence[Evidence],
        indicator_results: Sequence[IndicatorResult],
        audit: AuditContext = AuditContext(),
    ) -> None:
        resource_id = self._chain_resource_id(observations, evidence, indicator_results)
        try:
            with self._transaction():
                for value in observations:
                    self._insert_observation(value)
                    self._audit_observation(value, audit)
                for value in evidence:
                    self._insert_evidence(value)
                    self._audit_evidence(value, audit)
                for value in indicator_results:
                    self._insert_indicator_result(value)
                    self._audit_result(value, audit)
        except psycopg.IntegrityError as exc:
            self._record_failure("analysis_chain", resource_id, audit, exc)
            self._raise_integrity(exc)
        except PersistenceError as exc:
            self._record_failure("analysis_chain", resource_id, audit, exc)
            raise

    def get_observation(self, observation_id: str) -> Observation | None:
        row = self._connection.execute(
            """
            SELECT document_json, content_hash, analysis_id
            FROM archivability.observations
            WHERE observation_id = %s
            """,
            (observation_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            document = row[0]
            value = Observation(
                observation_id=document["observation_id"],
                analysis_id=document["analysis_id"],
                attempt_id=document["attempt_id"],
                kind=document["kind"],
                subject_uri=document["subject_uri"],
                observed_at=datetime.fromisoformat(document["observed_at"]),
                probe_id=document["probe_id"],
                tool_name=document["tool_name"],
                tool_version=document["tool_version"],
                payload_schema_version=document["payload_schema_version"],
                payload=document["payload"],
                content_hash=document["content_hash"],
                error_code=document["error_code"],
                truncated=document["truncated"],
            )
            if value.content_hash != row[1]:
                raise IntegrityViolation(
                    "observation column hash does not match document"
                )
            if value.observation_id != observation_id or value.analysis_id != row[2]:
                raise IntegrityViolation(
                    "observation identity does not match stored columns"
                )
            return value
        except IntegrityViolation:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "stored observation failed integrity validation"
            ) from exc

    def get_evidence(self, evidence_id: str) -> Evidence | None:
        row = self._connection.execute(
            """
            SELECT document_json, content_hash, analysis_id, indicator_id
            FROM archivability.evidence
            WHERE evidence_id = %s
            """,
            (evidence_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            document = row[0]
            value = Evidence(
                evidence_id=document["evidence_id"],
                analysis_id=document["analysis_id"],
                indicator_id=document["indicator_id"],
                kind=document["kind"],
                subject_uri=document["subject_uri"],
                method_id=document["method_id"],
                method_version=document["method_version"],
                created_at=datetime.fromisoformat(document["created_at"]),
                confidence=document["confidence"],
                sources=tuple(
                    EvidenceSource(
                        observation_id=source["observation_id"],
                        content_hash=source["content_hash"],
                        probe_id=source["probe_id"],
                        tool_name=source["tool_name"],
                        tool_version=source["tool_version"],
                        observed_at=datetime.fromisoformat(source["observed_at"]),
                    )
                    for source in document["sources"]
                ),
                data=document["data"],
                summary=document["summary"],
                content_hash=document["content_hash"],
            )
            if value.content_hash != row[1]:
                raise IntegrityViolation("evidence column hash does not match document")
            if (
                value.evidence_id != evidence_id
                or value.analysis_id != row[2]
                or value.indicator_id != row[3]
            ):
                raise IntegrityViolation(
                    "evidence identity does not match stored columns"
                )
            stored_sources = self._connection.execute(
                """
                SELECT observation_id, observation_hash
                FROM archivability.evidence_sources
                WHERE evidence_id = %s
                ORDER BY source_order
                """,
                (evidence_id,),
            ).fetchall()
            expected = [
                (source.observation_id, source.content_hash) for source in value.sources
            ]
            if stored_sources != expected:
                raise IntegrityViolation(
                    "stored evidence sources do not match document"
                )
            return value
        except IntegrityViolation:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "stored evidence failed integrity validation"
            ) from exc

    def get_indicator_result(
        self, analysis_id: str, indicator_id: str
    ) -> IndicatorResult | None:
        row = self._connection.execute(
            """
            SELECT document_json
            FROM archivability.indicator_results
            WHERE analysis_id = %s AND indicator_id = %s
            """,
            (analysis_id, indicator_id),
        ).fetchone()
        if row is None:
            return None
        try:
            document = row[0]
            value = IndicatorResult(
                indicator_id=document["indicator_id"],
                state=ResultState(document["state"]),
                confidence=document["confidence"],
                score=document["score"],
                analysis_id=document["analysis_id"],
                evidence_ids=tuple(document["evidence_ids"]),
                evidence_hashes=tuple(document["evidence_hashes"]),
                measured_at=(
                    datetime.fromisoformat(document["measured_at"])
                    if document["measured_at"] is not None
                    else None
                ),
                probe_ids=tuple(document["probe_ids"]),
                tool_versions=tuple(document["tool_versions"]),
                measurement_method_version=document["measurement_method_version"],
            )
            if value.analysis_id != analysis_id or value.indicator_id != indicator_id:
                raise IntegrityViolation(
                    "result identity does not match stored columns"
                )
            stored = self._connection.execute(
                """
                SELECT evidence_id, evidence_hash
                FROM archivability.indicator_result_evidence
                WHERE analysis_id = %s AND indicator_id = %s
                ORDER BY evidence_order
                """,
                (analysis_id, indicator_id),
            ).fetchall()
            expected = list(
                zip(value.evidence_ids, value.evidence_hashes, strict=True)
            )
            if stored != expected:
                raise IntegrityViolation(
                    "stored result evidence does not match document"
                )
            return value
        except IntegrityViolation:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "stored indicator result failed validation"
            ) from exc

    def complete_probe_attempt(
        self,
        previous: Attempt,
        current: Attempt,
        observations: tuple[Observation, ...],
        *,
        audit: AuditContext = AuditContext(),
    ) -> None:
        try:
            self._validate_probe_completion(previous, current, observations)
            with self._transaction():
                for value in observations:
                    self._insert_observation(value)
                    self._audit_observation(value, audit)
                self._update_attempt(previous, current)
                self._write_audit(
                    resource="attempt",
                    resource_id=current.attempt_id,
                    result="success",
                    audit=audit,
                    action="job.analysis_attempt",
                    before=self._attempt_audit_state(previous),
                    after=self._attempt_audit_state(current),
                )
        except psycopg.IntegrityError as exc:
            self._record_probe_persistence_failure(current.attempt_id, audit, exc)
            self._raise_integrity(exc)
        except PersistenceError as exc:
            self._record_probe_persistence_failure(current.attempt_id, audit, exc)
            raise

    def record_probe_event(
        self,
        *,
        analysis_id: str,
        attempt_id: str,
        probe_id: str,
        result: str,
        error_code: str | None,
        audit: AuditContext = AuditContext(),
    ) -> None:
        relation = self._connection.execute(
            """
            SELECT 1 FROM archivability.attempts
            WHERE attempt_id = %s AND analysis_id = %s
            """,
            (attempt_id, analysis_id),
        ).fetchone()
        if relation is None:
            raise IntegrityViolation("probe event does not reference a valid attempt")
        with self._transaction():
            self._write_audit(
                resource="attempt",
                resource_id=attempt_id,
                result=result,
                audit=audit,
                action="job.probe",
                extra={
                    "analysis_id": analysis_id,
                    "probe_id": probe_id,
                    "error_code": error_code,
                },
            )

    def authorize_probe_request(
        self, *, analysis_id: str, attempt_id: str, subject_uri: str
    ) -> None:
        analysis = self.get_analysis(analysis_id)
        attempt = self.get_attempt(attempt_id)
        if analysis is None or attempt is None:
            raise IntegrityViolation("probe request references missing lifecycle state")
        if analysis.state is not AnalysisState.RUNNING:
            raise IntegrityViolation("probe request requires a running analysis")
        if (
            attempt.state is not AttemptState.RUNNING
            or attempt.analysis_id != analysis_id
            or attempt_id not in analysis.attempt_ids
        ):
            raise IntegrityViolation(
                "probe request requires its registered running attempt"
            )
        if subject_uri != analysis.subject_uri:
            raise IntegrityViolation(
                "probe request subject does not match the analysis"
            )

    def _insert_observation(self, value: Observation) -> None:
        self._connection.execute(
            """
            INSERT INTO archivability.observations
                (observation_id, analysis_id, content_hash, document_json, stored_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                value.observation_id,
                value.analysis_id,
                value.content_hash,
                Jsonb(value.to_dict()),
                self._now(),
            ),
        )

    def _insert_evidence(self, value: Evidence) -> None:
        self._connection.execute(
            """
            INSERT INTO archivability.evidence
                (evidence_id, analysis_id, indicator_id, content_hash,
                 document_json, stored_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                value.evidence_id,
                value.analysis_id,
                value.indicator_id,
                value.content_hash,
                Jsonb(value.to_dict()),
                self._now(),
            ),
        )
        with self._connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO archivability.evidence_sources
                    (evidence_id, source_order, analysis_id,
                     observation_id, observation_hash)
                VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (
                        value.evidence_id,
                        order,
                        value.analysis_id,
                        source.observation_id,
                        source.content_hash,
                    )
                    for order, source in enumerate(value.sources)
                ],
            )

    def _insert_indicator_result(self, value: IndicatorResult) -> None:
        if value.analysis_id is None:
            raise PersistenceError("persisted indicator results require analysis_id")
        if not value.evidence_ids:
            raise PersistenceError(
                "persisted indicator results require evidence provenance"
            )
        self._connection.execute(
            """
            INSERT INTO archivability.indicator_results
                (analysis_id, indicator_id, document_json, stored_at)
            VALUES (%s, %s, %s, %s)
            """,
            (
                value.analysis_id,
                value.indicator_id,
                Jsonb(value.to_dict()),
                self._now(),
            ),
        )
        with self._connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO archivability.indicator_result_evidence
                    (analysis_id, indicator_id, evidence_order,
                     evidence_id, evidence_hash)
                VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (
                        value.analysis_id,
                        value.indicator_id,
                        order,
                        evidence_id,
                        evidence_hash,
                    )
                    for order, (evidence_id, evidence_hash) in enumerate(
                        zip(value.evidence_ids, value.evidence_hashes, strict=True)
                    )
                ],
            )

    def _audit_observation(self, value: Observation, audit: AuditContext) -> None:
        self._write_audit(
            resource="observation",
            resource_id=value.observation_id,
            result="success",
            audit=audit,
            action="data.create",
            after={
                "analysis_id": value.analysis_id,
                "content_hash": value.content_hash,
            },
        )

    def _audit_evidence(self, value: Evidence, audit: AuditContext) -> None:
        self._write_audit(
            resource="evidence",
            resource_id=value.evidence_id,
            result="success",
            audit=audit,
            action="data.create",
            after={
                "analysis_id": value.analysis_id,
                "indicator_id": value.indicator_id,
                "content_hash": value.content_hash,
            },
        )

    def _audit_result(self, value: IndicatorResult, audit: AuditContext) -> None:
        self._write_audit(
            resource="indicator_result",
            resource_id=f"{value.analysis_id}:{value.indicator_id}",
            result="success",
            audit=audit,
            action="data.create",
            after={
                "analysis_id": value.analysis_id,
                "indicator_id": value.indicator_id,
                "state": value.state.value,
            },
        )

    def _record_probe_persistence_failure(
        self, attempt_id: str, audit: AuditContext, error: Exception
    ) -> None:
        self._record_failure(
            "attempt",
            attempt_id,
            audit,
            error,
            action="job.probe_persistence",
        )

    @staticmethod
    def _validate_probe_completion(
        previous: Attempt,
        current: Attempt,
        observations: tuple[Observation, ...],
    ) -> None:
        PostgreSqlEvidenceRepository._validate_attempt_identity(previous, current)
        if previous.state is not AttemptState.RUNNING:
            raise IntegrityViolation("probe completion requires a running attempt")
        if current.state is not AttemptState.SUCCEEDED:
            raise IntegrityViolation("probe completion requires a successful attempt")
        if not isinstance(observations, tuple) or not observations:
            raise IntegrityViolation("probe completion requires observations")
        if len({item.observation_id for item in observations}) != len(observations):
            raise IntegrityViolation("probe observations must have unique identities")
        if any(
            item.analysis_id != current.analysis_id
            or item.attempt_id != current.attempt_id
            for item in observations
        ):
            raise IntegrityViolation("probe observation does not match its attempt")

    @staticmethod
    def _chain_resource_id(
        observations: Sequence[Observation],
        evidence: Sequence[Evidence],
        results: Sequence[IndicatorResult],
    ) -> str:
        analysis_ids = {
            *(item.analysis_id for item in observations),
            *(item.analysis_id for item in evidence),
            *(item.analysis_id for item in results if item.analysis_id is not None),
        }
        if len(analysis_ids) != 1:
            raise PersistenceError("a persisted chain must belong to one analysis")
        return next(iter(analysis_ids))
