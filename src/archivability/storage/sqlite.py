from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator
from uuid import uuid4

from archivability.evidence.models import Evidence, EvidenceSource, Observation
from archivability.lifecycle.models import Analysis, AnalysisState, Attempt, AttemptState
from archivability.methodology.models import IndicatorResult, ResultState
from archivability.storage.audit import AuditContext
from archivability.storage.errors import (
    ConcurrencyConflict,
    DuplicateRecordError,
    IntegrityViolation,
    PersistenceError,
)

if TYPE_CHECKING:
    from archivability.evidence.assessment import DerivationPersistenceResult


_MIGRATIONS = Path(__file__).parent / "migrations" / "sqlite"


def configure_sqlite_connection(connection: sqlite3.Connection) -> None:
    """Apply safe local-development connection settings without running DDL."""
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")


def apply_sqlite_migrations(connection: sqlite3.Connection) -> None:
    """Provision the development/test schema; never call from application runtime."""
    if connection.in_transaction:
        raise PersistenceError("migrations require an idle connection")
    has_registry = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    applied = (
        {
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
        }
        if has_registry
        else set()
    )
    for migration in sorted(_MIGRATIONS.glob("*.sql")):
        if migration.stem not in applied:
            connection.executescript(migration.read_text(encoding="utf-8"))


def _dump(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IntegrityViolation("stored timestamp has no timezone")
    return parsed


class SqliteEvidenceRepository:
    """Append-only SQLite adapter intended exclusively for local development/tests."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._connection = connection
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._event_id_factory = event_id_factory or (lambda: str(uuid4()))
        configure_sqlite_connection(connection)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self._connection.in_transaction:
            raise PersistenceError("nested or externally managed transactions are unsupported")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def save_observation(
        self, value: Observation, *, audit: AuditContext = AuditContext()
    ) -> None:
        self._execute_write(
            lambda: self._insert_observation(value),
            resource="observation",
            resource_id=value.observation_id,
            audit=audit,
            after={"analysis_id": value.analysis_id, "content_hash": value.content_hash},
        )

    def save_evidence(
        self, value: Evidence, *, audit: AuditContext = AuditContext()
    ) -> None:
        self._execute_write(
            lambda: self._insert_evidence(value),
            resource="evidence",
            resource_id=value.evidence_id,
            audit=audit,
            after={
                "analysis_id": value.analysis_id,
                "indicator_id": value.indicator_id,
                "content_hash": value.content_hash,
            },
        )

    def save_indicator_result(
        self, value: IndicatorResult, *, audit: AuditContext = AuditContext()
    ) -> None:
        resource_id = f"{value.analysis_id}:{value.indicator_id}"
        self._execute_write(
            lambda: self._insert_indicator_result(value),
            resource="indicator_result",
            resource_id=resource_id,
            audit=audit,
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
                    self._write_audit(
                        resource="observation",
                        resource_id=value.observation_id,
                        result="success",
                        audit=audit,
                        after={"analysis_id": value.analysis_id, "content_hash": value.content_hash},
                    )
                for value in evidence:
                    self._insert_evidence(value)
                    self._write_audit(
                        resource="evidence",
                        resource_id=value.evidence_id,
                        result="success",
                        audit=audit,
                        after={
                            "analysis_id": value.analysis_id,
                            "indicator_id": value.indicator_id,
                            "content_hash": value.content_hash,
                        },
                    )
                for value in indicator_results:
                    self._insert_indicator_result(value)
                    self._write_audit(
                        resource="indicator_result",
                        resource_id=f"{value.analysis_id}:{value.indicator_id}",
                        result="success",
                        audit=audit,
                        after={
                            "analysis_id": value.analysis_id,
                            "indicator_id": value.indicator_id,
                            "state": value.state.value,
                        },
                    )
        except sqlite3.IntegrityError as exc:
            self._record_failure("analysis_chain", resource_id, audit, exc)
            self._raise_integrity(exc)
        except sqlite3.DatabaseError as exc:
            self._record_failure("analysis_chain", resource_id, audit, exc)
            raise PersistenceError("database write failed") from exc

    def get_observation(self, observation_id: str) -> Observation | None:
        row = self._connection.execute(
            """
            SELECT document_json, content_hash, analysis_id
            FROM observations
            WHERE observation_id = ?
            """,
            (observation_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            document = json.loads(row[0])
            value = Observation(
                observation_id=document["observation_id"],
                analysis_id=document["analysis_id"],
                attempt_id=document["attempt_id"],
                kind=document["kind"],
                subject_uri=document["subject_uri"],
                observed_at=_parse_datetime(document["observed_at"]),
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
                raise IntegrityViolation("observation column hash does not match document")
            if value.observation_id != observation_id or value.analysis_id != row[2]:
                raise IntegrityViolation("observation identity does not match stored columns")
            return value
        except IntegrityViolation:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IntegrityViolation("stored observation failed integrity validation") from exc

    def get_evidence(self, evidence_id: str) -> Evidence | None:
        row = self._connection.execute(
            """
            SELECT document_json, content_hash, analysis_id, indicator_id
            FROM evidence
            WHERE evidence_id = ?
            """,
            (evidence_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            document = json.loads(row[0])
            value = Evidence(
                evidence_id=document["evidence_id"],
                analysis_id=document["analysis_id"],
                indicator_id=document["indicator_id"],
                kind=document["kind"],
                subject_uri=document["subject_uri"],
                method_id=document["method_id"],
                method_version=document["method_version"],
                created_at=_parse_datetime(document["created_at"]),
                confidence=document["confidence"],
                sources=tuple(
                    EvidenceSource(
                        observation_id=source["observation_id"],
                        content_hash=source["content_hash"],
                        probe_id=source["probe_id"],
                        tool_name=source["tool_name"],
                        tool_version=source["tool_version"],
                        observed_at=_parse_datetime(source["observed_at"]),
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
                raise IntegrityViolation("evidence identity does not match stored columns")
            stored_sources = self._connection.execute(
                """
                SELECT observation_id, observation_hash
                FROM evidence_sources
                WHERE evidence_id = ?
                ORDER BY source_order
                """,
                (evidence_id,),
            ).fetchall()
            expected_sources = [
                (source.observation_id, source.content_hash) for source in value.sources
            ]
            if stored_sources != expected_sources:
                raise IntegrityViolation("stored evidence sources do not match document")
            return value
        except IntegrityViolation:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IntegrityViolation("stored evidence failed integrity validation") from exc

    def get_indicator_result(
        self, analysis_id: str, indicator_id: str
    ) -> IndicatorResult | None:
        row = self._connection.execute(
            "SELECT document_json FROM indicator_results WHERE analysis_id = ? AND indicator_id = ?",
            (analysis_id, indicator_id),
        ).fetchone()
        if row is None:
            return None
        try:
            document = json.loads(row[0])
            value = IndicatorResult(
                indicator_id=document["indicator_id"],
                state=ResultState(document["state"]),
                confidence=document["confidence"],
                score=document["score"],
                analysis_id=document["analysis_id"],
                evidence_ids=tuple(document["evidence_ids"]),
                evidence_hashes=tuple(document["evidence_hashes"]),
                measured_at=(
                    _parse_datetime(document["measured_at"])
                    if document["measured_at"] is not None
                    else None
                ),
                probe_ids=tuple(document["probe_ids"]),
                tool_versions=tuple(document["tool_versions"]),
                measurement_method_version=document["measurement_method_version"],
            )
            if value.analysis_id != analysis_id or value.indicator_id != indicator_id:
                raise IntegrityViolation("result identity does not match stored columns")
            stored_evidence = self._connection.execute(
                """
                SELECT evidence_id, evidence_hash
                FROM indicator_result_evidence
                WHERE analysis_id = ? AND indicator_id = ?
                ORDER BY evidence_order
                """,
                (analysis_id, indicator_id),
            ).fetchall()
            if stored_evidence != list(zip(value.evidence_ids, value.evidence_hashes, strict=True)):
                raise IntegrityViolation("stored result evidence does not match document")
            return value
        except IntegrityViolation:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IntegrityViolation("stored indicator result failed validation") from exc

    def _execute_write(
        self,
        operation: Callable[[], None],
        *,
        resource: str,
        resource_id: str,
        audit: AuditContext,
        after: dict[str, Any],
        action: str = "data.create",
        before: dict[str, Any] | None = None,
        audit_result: str = "success",
    ) -> None:
        try:
            with self._transaction():
                operation()
                self._write_audit(
                    resource=resource,
                    resource_id=resource_id,
                    result=audit_result,
                    audit=audit,
                    action=action,
                    before=before,
                    after=after,
                )
        except sqlite3.IntegrityError as exc:
            self._record_failure(resource, resource_id, audit, exc, action=action)
            self._raise_integrity(exc)
        except sqlite3.DatabaseError as exc:
            self._record_failure(resource, resource_id, audit, exc, action=action)
            raise PersistenceError("database write failed") from exc
        except PersistenceError as exc:
            self._record_failure(resource, resource_id, audit, exc, action=action)
            raise

    def _insert_observation(self, value: Observation) -> None:
        self._connection.execute(
            """
            INSERT INTO observations
                (observation_id, analysis_id, content_hash, document_json, stored_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                value.observation_id,
                value.analysis_id,
                value.content_hash,
                _dump(value.to_dict()),
                self._timestamp(),
            ),
        )

    def _insert_evidence(self, value: Evidence) -> None:
        self._connection.execute(
            """
            INSERT INTO evidence
                (evidence_id, analysis_id, indicator_id, content_hash, document_json, stored_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                value.evidence_id,
                value.analysis_id,
                value.indicator_id,
                value.content_hash,
                _dump(value.to_dict()),
                self._timestamp(),
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO evidence_sources
                (evidence_id, source_order, analysis_id, observation_id, observation_hash)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                (
                    value.evidence_id,
                    order,
                    value.analysis_id,
                    source.observation_id,
                    source.content_hash,
                )
                for order, source in enumerate(value.sources)
            ),
        )

    def _insert_indicator_result(self, value: IndicatorResult) -> None:
        if value.analysis_id is None:
            raise PersistenceError("persisted indicator results require analysis_id")
        if not value.evidence_ids:
            raise PersistenceError("persisted indicator results require evidence provenance")
        self._connection.execute(
            """
            INSERT INTO indicator_results
                (analysis_id, indicator_id, document_json, stored_at)
            VALUES (?, ?, ?, ?)
            """,
            (value.analysis_id, value.indicator_id, _dump(value.to_dict()), self._timestamp()),
        )
        self._connection.executemany(
            """
            INSERT INTO indicator_result_evidence
                (analysis_id, indicator_id, evidence_order, evidence_id, evidence_hash)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                (value.analysis_id, value.indicator_id, order, evidence_id, evidence_hash)
                for order, (evidence_id, evidence_hash) in enumerate(
                    zip(value.evidence_ids, value.evidence_hashes, strict=True)
                )
            ),
        )

    def _write_audit(
        self,
        *,
        resource: str,
        resource_id: str,
        result: str,
        audit: AuditContext,
        action: str = "data.create",
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO audit_events
                (event_id, timestamp, user_id, ip_address, session_id, action,
                 resource, resource_id, result, before_json, after_json, extra_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._event_id_factory(),
                self._timestamp(),
                audit.user_id,
                audit.ip_address,
                audit.session_id,
                action,
                resource,
                resource_id,
                result,
                _dump(before) if before is not None else None,
                _dump(after) if after is not None else None,
                _dump(extra) if extra is not None else None,
            ),
        )

    def _record_failure(
        self,
        resource: str,
        resource_id: str,
        audit: AuditContext,
        error: Exception,
        *,
        action: str = "data.create",
    ) -> None:
        try:
            with self._transaction():
                self._write_audit(
                    resource=resource,
                    resource_id=resource_id,
                    result="failure",
                    audit=audit,
                    action=action,
                    extra={"error_type": type(error).__name__},
                )
        except (PersistenceError, sqlite3.DatabaseError):
            # Audit storage may be the failing component. Preserve the original error.
            pass

    @staticmethod
    def _raise_integrity(error: sqlite3.IntegrityError) -> None:
        message = str(error).lower()
        if "unique" in message or "primary key" in message:
            raise DuplicateRecordError("immutable record already exists") from error
        raise IntegrityViolation("database rejected inconsistent provenance") from error

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise PersistenceError("repository clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

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


class SqliteLifecycleRepository(SqliteEvidenceRepository):
    """SQLite lifecycle adapter with optimistic concurrency and audited transitions."""

    def add_analysis(
        self, value: Analysis, *, audit: AuditContext = AuditContext()
    ) -> None:
        self._execute_write(
            lambda: self._insert_analysis(value),
            resource="analysis",
            resource_id=value.analysis_id,
            audit=audit,
            after={"state": value.state.value, "revision": value.revision},
        )

    def get_analysis(self, analysis_id: str) -> Analysis | None:
        row = self._connection.execute(
            "SELECT document_json, state, revision FROM analyses WHERE analysis_id = ?",
            (analysis_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            value = self._analysis_from_document(json.loads(row[0]))
            if (
                value.analysis_id != analysis_id
                or value.state.value != row[1]
                or value.revision != row[2]
            ):
                raise IntegrityViolation("analysis document does not match stored columns")
            stored_attempt_ids = tuple(
                item[0]
                for item in self._connection.execute(
                    "SELECT attempt_id FROM attempts WHERE analysis_id = ? ORDER BY sequence",
                    (analysis_id,),
                ).fetchall()
            )
            if stored_attempt_ids != value.attempt_ids:
                raise IntegrityViolation("analysis attempts do not match stored relations")
            return value
        except IntegrityViolation:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IntegrityViolation("stored analysis failed validation") from exc

    def add_attempt(
        self,
        previous_analysis: Analysis,
        analysis: Analysis,
        attempt: Attempt,
        *,
        audit: AuditContext = AuditContext(),
    ) -> None:
        if attempt.analysis_id != analysis.analysis_id:
            raise IntegrityViolation("attempt and analysis identities do not match")
        resource_id = attempt.attempt_id
        try:
            with self._transaction():
                self._update_analysis(previous_analysis, analysis)
                self._insert_attempt(attempt)
                self._write_audit(
                    resource="analysis",
                    resource_id=analysis.analysis_id,
                    result="success",
                    audit=audit,
                    action="data.update",
                    before=self._analysis_audit_state(previous_analysis),
                    after=self._analysis_audit_state(analysis),
                )
                self._write_audit(
                    resource="attempt",
                    resource_id=attempt.attempt_id,
                    result="success",
                    audit=audit,
                    action="job.analysis_attempt_start",
                    after=self._attempt_audit_state(attempt),
                )
        except sqlite3.IntegrityError as exc:
            self._record_failure(
                "attempt", resource_id, audit, exc, action="job.analysis_attempt_start"
            )
            self._raise_integrity(exc)
        except (sqlite3.DatabaseError, PersistenceError) as exc:
            self._record_failure(
                "attempt", resource_id, audit, exc, action="job.analysis_attempt_start"
            )
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError("database write failed") from exc

    def get_attempt(self, attempt_id: str) -> Attempt | None:
        row = self._connection.execute(
            """
            SELECT document_json, state, revision, analysis_id, sequence
            FROM attempts
            WHERE attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            value = self._attempt_from_document(json.loads(row[0]))
            if (
                value.attempt_id != attempt_id
                or value.state.value != row[1]
                or value.revision != row[2]
                or value.analysis_id != row[3]
                or value.sequence != row[4]
            ):
                raise IntegrityViolation("attempt document does not match stored columns")
            return value
        except IntegrityViolation:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IntegrityViolation("stored attempt failed validation") from exc

    def list_attempts(self, analysis_id: str) -> tuple[Attempt, ...]:
        rows = self._connection.execute(
            "SELECT attempt_id FROM attempts WHERE analysis_id = ? ORDER BY sequence",
            (analysis_id,),
        ).fetchall()
        values = tuple(self.get_attempt(row[0]) for row in rows)
        if any(value is None for value in values):
            raise IntegrityViolation("attempt disappeared during lifecycle read")
        return tuple(value for value in values if value is not None)

    def update_attempt(
        self,
        previous: Attempt,
        current: Attempt,
        *,
        audit: AuditContext = AuditContext(),
    ) -> None:
        self._validate_attempt_identity(previous, current)
        result = "failure" if current.state is AttemptState.FAILED else "success"
        self._execute_write(
            lambda: self._update_attempt(previous, current),
            resource="attempt",
            resource_id=current.attempt_id,
            audit=audit,
            action="job.analysis_attempt",
            before=self._attempt_audit_state(previous),
            after=self._attempt_audit_state(current),
            audit_result=result,
        )

    def complete_probe_attempt(
        self,
        previous: Attempt,
        current: Attempt,
        observations: tuple[Observation, ...],
        *,
        audit: AuditContext = AuditContext(),
    ) -> None:
        resource_id = current.attempt_id
        try:
            self._validate_probe_completion(previous, current, observations)
            with self._transaction():
                for value in observations:
                    self._insert_observation(value)
                    self._write_audit(
                        resource="observation",
                        resource_id=value.observation_id,
                        result="success",
                        audit=audit,
                        after={
                            "analysis_id": value.analysis_id,
                            "content_hash": value.content_hash,
                        },
                    )
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
        except sqlite3.IntegrityError as exc:
            self._record_failure(
                "attempt",
                resource_id,
                audit,
                exc,
                action="job.probe_persistence",
            )
            self._raise_integrity(exc)
        except (sqlite3.DatabaseError, PersistenceError) as exc:
            self._record_failure(
                "attempt",
                resource_id,
                audit,
                exc,
                action="job.probe_persistence",
            )
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError("database write failed") from exc

    def complete_http_assessment(
        self,
        previous_analysis: Analysis,
        current_analysis: Analysis | None,
        evidence: tuple[Evidence, ...],
        indicator_results: tuple[IndicatorResult, ...],
        *,
        audit: AuditContext = AuditContext(),
    ) -> DerivationPersistenceResult:
        """Persist one deterministic derivation and its lifecycle transition."""
        resource_id = previous_analysis.analysis_id
        try:
            self._validate_http_assessment(
                previous_analysis,
                current_analysis,
                evidence,
                indicator_results,
            )
            with self._transaction():
                outcome = self._derivation_outcome(evidence, indicator_results)
                stored_analysis = self.get_analysis(previous_analysis.analysis_id)
                if stored_analysis != previous_analysis:
                    raise ConcurrencyConflict("analysis was changed by another operation")

                if outcome == "created":
                    if current_analysis is None:
                        raise IntegrityViolation(
                            "new derivation requires an analysis transition"
                        )
                    for value in evidence:
                        self._insert_evidence(value)
                        self._write_audit(
                            resource="evidence",
                            resource_id=value.evidence_id,
                            result="success",
                            audit=audit,
                            after={
                                "analysis_id": value.analysis_id,
                                "indicator_id": value.indicator_id,
                                "content_hash": value.content_hash,
                            },
                        )
                    for value in indicator_results:
                        self._insert_indicator_result(value)
                        self._write_audit(
                            resource="indicator_result",
                            resource_id=f"{value.analysis_id}:{value.indicator_id}",
                            result="success",
                            audit=audit,
                            after={
                                "analysis_id": value.analysis_id,
                                "indicator_id": value.indicator_id,
                                "state": value.state.value,
                            },
                        )
                    self._update_analysis(previous_analysis, current_analysis)
                    self._write_audit(
                        resource="analysis",
                        resource_id=current_analysis.analysis_id,
                        result="success",
                        audit=audit,
                        action="data.update",
                        before=self._analysis_audit_state(previous_analysis),
                        after=self._analysis_audit_state(current_analysis),
                    )
                elif current_analysis is not None:
                    raise IntegrityViolation(
                        "stored derivation cannot repeat a lifecycle transition"
                    )

                self._write_audit(
                    resource="analysis",
                    resource_id=resource_id,
                    result="success",
                    audit=audit,
                    action="job.http_metadata_assessment",
                    extra={
                        "outcome": outcome,
                        "evidence_count": len(evidence),
                        "indicator_result_count": len(indicator_results),
                    },
                )
            from archivability.evidence.assessment import DerivationPersistenceResult

            return DerivationPersistenceResult(
                outcome=outcome,
                evidence_count=len(evidence),
                indicator_result_count=len(indicator_results),
            )
        except sqlite3.IntegrityError as exc:
            self._record_failure(
                "analysis",
                resource_id,
                audit,
                exc,
                action="job.http_metadata_assessment",
            )
            self._raise_integrity(exc)
        except (sqlite3.DatabaseError, PersistenceError) as exc:
            self._record_failure(
                "analysis",
                resource_id,
                audit,
                exc,
                action="job.http_metadata_assessment",
            )
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError("database write failed") from exc

    def update_analysis(
        self,
        previous: Analysis,
        current: Analysis,
        *,
        audit: AuditContext = AuditContext(),
    ) -> None:
        if previous.analysis_id != current.analysis_id:
            raise IntegrityViolation("analysis identity cannot change")
        self._execute_write(
            lambda: self._update_analysis(previous, current),
            resource="analysis",
            resource_id=current.analysis_id,
            audit=audit,
            action="data.update",
            before=self._analysis_audit_state(previous),
            after=self._analysis_audit_state(current),
        )

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
            "SELECT 1 FROM attempts WHERE attempt_id = ? AND analysis_id = ?",
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
            raise IntegrityViolation("probe request requires its registered running attempt")
        if subject_uri != analysis.subject_uri:
            raise IntegrityViolation("probe request subject does not match the analysis")

    def _validate_http_assessment(
        self,
        previous: Analysis,
        current: Analysis | None,
        evidence: tuple[Evidence, ...],
        results: tuple[IndicatorResult, ...],
    ) -> None:
        if not evidence or not results:
            raise IntegrityViolation("HTTP assessment derivation must not be empty")
        if len({item.evidence_id for item in evidence}) != len(evidence):
            raise IntegrityViolation("derived evidence identities must be unique")
        if len({item.indicator_id for item in results}) != len(results):
            raise IntegrityViolation("derived indicator identities must be unique")
        if any(
            item.analysis_id != previous.analysis_id
            or item.subject_uri != previous.subject_uri
            for item in evidence
        ) or any(item.analysis_id != previous.analysis_id for item in results):
            raise IntegrityViolation("derivation does not match its analysis")
        evidence_by_id = {item.evidence_id: item for item in evidence}
        referenced_evidence = {
            evidence_id for item in results for evidence_id in item.evidence_ids
        }
        if any(
            not item.evidence_ids
            or any(
                evidence_id not in evidence_by_id
                or evidence_by_id[evidence_id].indicator_id != item.indicator_id
                or evidence_by_id[evidence_id].content_hash != evidence_hash
                for evidence_id, evidence_hash in zip(
                    item.evidence_ids, item.evidence_hashes, strict=True
                )
            )
            for item in results
        ) or referenced_evidence != set(evidence_by_id):
            raise IntegrityViolation("indicator result provenance is inconsistent")
        for item in evidence:
            for source in item.sources:
                observation = self.get_observation(source.observation_id)
                if (
                    observation is None
                    or observation.analysis_id != previous.analysis_id
                    or observation.content_hash != source.content_hash
                    or observation.probe_id != source.probe_id
                    or observation.tool_name != source.tool_name
                    or observation.tool_version != source.tool_version
                    or observation.observed_at != source.observed_at
                ):
                    raise IntegrityViolation("evidence source does not match its observation")

        if current is None:
            if previous.state not in {
                AnalysisState.COMPLETED,
                AnalysisState.PARTIALLY_COMPLETED,
                AnalysisState.CANCELLED,
            }:
                raise IntegrityViolation("derivation replay requires a terminal analysis")
            return
        immutable_fields_match = (
            previous.analysis_id == current.analysis_id
            and previous.subject_uri == current.subject_uri
            and previous.methodology_id == current.methodology_id
            and previous.methodology_version == current.methodology_version
            and previous.max_attempts == current.max_attempts
            and previous.attempt_ids == current.attempt_ids
            and previous.created_at == current.created_at
            and previous.started_at == current.started_at
        )
        if (
            previous.state is not AnalysisState.RUNNING
            or current.state
            not in {
                AnalysisState.COMPLETED,
                AnalysisState.PARTIALLY_COMPLETED,
                AnalysisState.CANCELLED,
            }
            or not immutable_fields_match
        ):
            raise IntegrityViolation("analysis completion is inconsistent")

    def _derivation_outcome(
        self,
        evidence: tuple[Evidence, ...],
        results: tuple[IndicatorResult, ...],
    ) -> str:
        stored_evidence = tuple(self.get_evidence(item.evidence_id) for item in evidence)
        stored_results = tuple(
            self.get_indicator_result(item.analysis_id or "", item.indicator_id)
            for item in results
        )
        present = tuple(item is not None for item in (*stored_evidence, *stored_results))
        if not any(present):
            return "created"
        if not all(present):
            raise IntegrityViolation("stored derivation is incomplete")
        if any(
            stored is None or stored.to_dict() != expected.to_dict()
            for stored, expected in zip(stored_evidence, evidence, strict=True)
        ) or any(
            stored is None or stored.to_dict() != expected.to_dict()
            for stored, expected in zip(stored_results, results, strict=True)
        ):
            raise IntegrityViolation("stored derivation conflicts with deterministic output")
        return "replayed"

    @staticmethod
    def _validate_probe_completion(
        previous: Attempt,
        current: Attempt,
        observations: tuple[Observation, ...],
    ) -> None:
        SqliteLifecycleRepository._validate_attempt_identity(previous, current)
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
    def _validate_attempt_identity(previous: Attempt, current: Attempt) -> None:
        if (
            previous.attempt_id != current.attempt_id
            or previous.analysis_id != current.analysis_id
            or previous.sequence != current.sequence
            or previous.created_at != current.created_at
            or previous.started_at != current.started_at
        ):
            raise IntegrityViolation("attempt identity cannot change")

    def _insert_analysis(self, value: Analysis) -> None:
        timestamp = self._timestamp()
        self._connection.execute(
            """
            INSERT INTO analyses
                (analysis_id, state, revision, document_json, stored_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                value.analysis_id,
                value.state.value,
                value.revision,
                _dump(value.to_dict()),
                timestamp,
                timestamp,
            ),
        )

    def _update_analysis(self, previous: Analysis, current: Analysis) -> None:
        if current.revision != previous.revision + 1:
            raise ConcurrencyConflict("analysis revision must increase by one")
        cursor = self._connection.execute(
            """
            UPDATE analyses
            SET state = ?, revision = ?, document_json = ?, updated_at = ?
            WHERE analysis_id = ? AND revision = ?
            """,
            (
                current.state.value,
                current.revision,
                _dump(current.to_dict()),
                self._timestamp(),
                previous.analysis_id,
                previous.revision,
            ),
        )
        if cursor.rowcount != 1:
            raise ConcurrencyConflict("analysis was changed by another operation")

    def _insert_attempt(self, value: Attempt) -> None:
        timestamp = self._timestamp()
        self._connection.execute(
            """
            INSERT INTO attempts
                (attempt_id, analysis_id, sequence, state, revision,
                 document_json, stored_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                value.attempt_id,
                value.analysis_id,
                value.sequence,
                value.state.value,
                value.revision,
                _dump(value.to_dict()),
                timestamp,
                timestamp,
            ),
        )

    def _update_attempt(self, previous: Attempt, current: Attempt) -> None:
        if current.revision != previous.revision + 1:
            raise ConcurrencyConflict("attempt revision must increase by one")
        cursor = self._connection.execute(
            """
            UPDATE attempts
            SET state = ?, revision = ?, document_json = ?, updated_at = ?
            WHERE attempt_id = ? AND revision = ?
            """,
            (
                current.state.value,
                current.revision,
                _dump(current.to_dict()),
                self._timestamp(),
                previous.attempt_id,
                previous.revision,
            ),
        )
        if cursor.rowcount != 1:
            raise ConcurrencyConflict("attempt was changed by another operation")

    @staticmethod
    def _analysis_from_document(document: dict[str, Any]) -> Analysis:
        return Analysis(
            analysis_id=document["analysis_id"],
            subject_uri=document["subject_uri"],
            methodology_id=document["methodology_id"],
            methodology_version=document["methodology_version"],
            state=AnalysisState(document["state"]),
            max_attempts=document["max_attempts"],
            attempt_ids=tuple(document["attempt_ids"]),
            created_at=_parse_datetime(document["created_at"]),
            updated_at=_parse_datetime(document["updated_at"]),
            started_at=(
                _parse_datetime(document["started_at"])
                if document["started_at"] is not None
                else None
            ),
            finished_at=(
                _parse_datetime(document["finished_at"])
                if document["finished_at"] is not None
                else None
            ),
            revision=document["revision"],
        )

    @staticmethod
    def _attempt_from_document(document: dict[str, Any]) -> Attempt:
        return Attempt(
            attempt_id=document["attempt_id"],
            analysis_id=document["analysis_id"],
            sequence=document["sequence"],
            state=AttemptState(document["state"]),
            created_at=_parse_datetime(document["created_at"]),
            updated_at=_parse_datetime(document["updated_at"]),
            started_at=_parse_datetime(document["started_at"]),
            finished_at=(
                _parse_datetime(document["finished_at"])
                if document["finished_at"] is not None
                else None
            ),
            failure_code=document["failure_code"],
            revision=document["revision"],
        )

    @staticmethod
    def _analysis_audit_state(value: Analysis) -> dict[str, Any]:
        return {"state": value.state.value, "revision": value.revision}

    @staticmethod
    def _attempt_audit_state(value: Attempt) -> dict[str, Any]:
        return {
            "analysis_id": value.analysis_id,
            "sequence": value.sequence,
            "state": value.state.value,
            "revision": value.revision,
            "failure_code": value.failure_code,
        }
