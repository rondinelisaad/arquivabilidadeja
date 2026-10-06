from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from archivability.lifecycle.models import (
    Analysis,
    AnalysisState,
    Attempt,
    AttemptState,
)
from archivability.storage.audit import AuditContext
from archivability.storage.errors import (
    ConcurrencyConflict,
    DuplicateRecordError,
    IntegrityViolation,
    PersistenceError,
)


class PostgreSqlLifecycleRepository:
    """PostgreSQL lifecycle adapter with explicit transactions and audit events."""

    def __init__(
        self,
        connection: psycopg.Connection[Any],
        *,
        clock: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not connection.autocommit:
            raise PersistenceError(
                "PostgreSQL runtime connections require autocommit to be enabled"
            )
        self._connection = connection
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._event_id_factory = event_id_factory or (lambda: str(uuid4()))

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        try:
            with self._connection.transaction():
                yield
        except psycopg.IntegrityError:
            raise
        except psycopg.Error as exc:
            raise PersistenceError("PostgreSQL transaction failed") from exc

    def add_analysis(
        self,
        value: Analysis,
        *,
        owner_user_id: str | None = None,
        audit: AuditContext = AuditContext(),
    ) -> None:
        self._validate_owner(owner_user_id, audit)
        try:
            with self._transaction():
                self._connection.execute(
                    """
                    INSERT INTO archivability.analyses
                        (analysis_id, state, revision, document_json,
                         stored_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        value.analysis_id,
                        value.state.value,
                        value.revision,
                        Jsonb(value.to_dict()),
                        self._now(),
                        self._now(),
                    ),
                )
                if owner_user_id is not None:
                    self._connection.execute(
                        """
                        INSERT INTO archivability.analysis_ownership
                            (analysis_id, owner_user_id, created_at)
                        VALUES (%s, %s, %s)
                        """,
                        (value.analysis_id, owner_user_id, self._now()),
                    )
                self._write_audit(
                    resource="analysis",
                    resource_id=value.analysis_id,
                    result="success",
                    audit=audit,
                    action="data.create",
                    after={
                        "state": value.state.value,
                        "revision": value.revision,
                        "owner_bound": owner_user_id is not None,
                    },
                )
                if owner_user_id is not None:
                    self._write_audit(
                        resource="analysis_ownership",
                        resource_id=value.analysis_id,
                        result="success",
                        audit=audit,
                        action="permission.analysis_owner_assign",
                        after={"owner_bound": True},
                    )
        except psycopg.IntegrityError as exc:
            self._record_failure("analysis", value.analysis_id, audit, exc)
            self._raise_integrity(exc)
        except PersistenceError as exc:
            self._record_failure("analysis", value.analysis_id, audit, exc)
            raise

    def get_analysis_owner(self, analysis_id: str) -> str | None:
        row = self._connection.execute(
            """
            SELECT owner_user_id
            FROM archivability.analysis_ownership
            WHERE analysis_id = %s
            """,
            (analysis_id,),
        ).fetchone()
        return row[0] if row is not None else None

    def get_analysis(self, analysis_id: str) -> Analysis | None:
        row = self._connection.execute(
            """
            SELECT document_json, state, revision
            FROM archivability.analyses
            WHERE analysis_id = %s
            """,
            (analysis_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            value = self._analysis_from_document(row[0])
            if (
                value.analysis_id != analysis_id
                or value.state.value != row[1]
                or value.revision != row[2]
            ):
                raise IntegrityViolation(
                    "analysis document does not match stored columns"
                )
            attempt_ids = tuple(
                item[0]
                for item in self._connection.execute(
                    """
                    SELECT attempt_id
                    FROM archivability.attempts
                    WHERE analysis_id = %s
                    ORDER BY sequence
                    """,
                    (analysis_id,),
                ).fetchall()
            )
            if attempt_ids != value.attempt_ids:
                raise IntegrityViolation(
                    "analysis attempts do not match stored relations"
                )
            return value
        except IntegrityViolation:
            raise
        except (KeyError, TypeError, ValueError) as exc:
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
        try:
            with self._transaction():
                self._update_analysis(previous_analysis, analysis)
                self._connection.execute(
                    """
                    INSERT INTO archivability.attempts
                        (attempt_id, analysis_id, sequence, state, revision,
                         document_json, stored_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        attempt.attempt_id,
                        attempt.analysis_id,
                        attempt.sequence,
                        attempt.state.value,
                        attempt.revision,
                        Jsonb(attempt.to_dict()),
                        self._now(),
                        self._now(),
                    ),
                )
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
        except psycopg.IntegrityError as exc:
            self._record_failure(
                "attempt",
                attempt.attempt_id,
                audit,
                exc,
                action="job.analysis_attempt_start",
            )
            self._raise_integrity(exc)
        except PersistenceError as exc:
            self._record_failure(
                "attempt",
                attempt.attempt_id,
                audit,
                exc,
                action="job.analysis_attempt_start",
            )
            raise

    def get_attempt(self, attempt_id: str) -> Attempt | None:
        row = self._connection.execute(
            """
            SELECT document_json, state, revision, analysis_id, sequence
            FROM archivability.attempts
            WHERE attempt_id = %s
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            value = self._attempt_from_document(row[0])
            if (
                value.attempt_id != attempt_id
                or value.state.value != row[1]
                or value.revision != row[2]
                or value.analysis_id != row[3]
                or value.sequence != row[4]
            ):
                raise IntegrityViolation(
                    "attempt document does not match stored columns"
                )
            return value
        except IntegrityViolation:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrityViolation("stored attempt failed validation") from exc

    def list_attempts(self, analysis_id: str) -> tuple[Attempt, ...]:
        rows = self._connection.execute(
            """
            SELECT document_json, state, revision, attempt_id, sequence
            FROM archivability.attempts
            WHERE analysis_id = %s
            ORDER BY sequence
            """,
            (analysis_id,),
        ).fetchall()
        values: list[Attempt] = []
        for row in rows:
            try:
                value = self._attempt_from_document(row[0])
            except (KeyError, TypeError, ValueError) as exc:
                raise IntegrityViolation("stored attempt failed validation") from exc
            if (
                value.analysis_id != analysis_id
                or value.state.value != row[1]
                or value.revision != row[2]
                or value.attempt_id != row[3]
                or value.sequence != row[4]
            ):
                raise IntegrityViolation(
                    "attempt document does not match stored columns"
                )
            values.append(value)
        return tuple(values)

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

    def _execute_write(
        self,
        operation: Callable[[], None],
        *,
        resource: str,
        resource_id: str,
        audit: AuditContext,
        action: str,
        before: dict[str, Any] | None,
        after: dict[str, Any],
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
        except psycopg.IntegrityError as exc:
            self._record_failure(resource, resource_id, audit, exc, action=action)
            self._raise_integrity(exc)
        except PersistenceError as exc:
            self._record_failure(resource, resource_id, audit, exc, action=action)
            raise

    def _update_analysis(self, previous: Analysis, current: Analysis) -> None:
        if current.revision != previous.revision + 1:
            raise ConcurrencyConflict("analysis revision must increase by one")
        cursor = self._connection.execute(
            """
            UPDATE archivability.analyses
            SET state = %s, revision = %s, document_json = %s, updated_at = %s
            WHERE analysis_id = %s AND revision = %s
            """,
            (
                current.state.value,
                current.revision,
                Jsonb(current.to_dict()),
                self._now(),
                previous.analysis_id,
                previous.revision,
            ),
        )
        if cursor.rowcount != 1:
            raise ConcurrencyConflict("analysis was changed by another operation")

    def _update_attempt(self, previous: Attempt, current: Attempt) -> None:
        if current.revision != previous.revision + 1:
            raise ConcurrencyConflict("attempt revision must increase by one")
        cursor = self._connection.execute(
            """
            UPDATE archivability.attempts
            SET state = %s, revision = %s, document_json = %s, updated_at = %s
            WHERE attempt_id = %s AND revision = %s
            """,
            (
                current.state.value,
                current.revision,
                Jsonb(current.to_dict()),
                self._now(),
                previous.attempt_id,
                previous.revision,
            ),
        )
        if cursor.rowcount != 1:
            raise ConcurrencyConflict("attempt was changed by another operation")

    def _write_audit(
        self,
        *,
        resource: str,
        resource_id: str,
        result: str,
        audit: AuditContext,
        action: str,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO archivability.audit_events
                (event_id, timestamp, user_id, ip_address, session_id, action,
                 resource, resource_id, result, before_json, after_json, extra_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                self._event_id_factory(),
                self._now(),
                audit.user_id,
                audit.ip_address,
                audit.session_id,
                action,
                resource,
                resource_id,
                result,
                Jsonb(before) if before is not None else None,
                Jsonb(after) if after is not None else None,
                Jsonb(extra) if extra is not None else None,
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
        except (psycopg.Error, PersistenceError):
            pass

    @staticmethod
    def _raise_integrity(error: psycopg.IntegrityError) -> None:
        if isinstance(error, psycopg.errors.UniqueViolation):
            raise DuplicateRecordError("immutable record already exists") from error
        raise IntegrityViolation(
            "database rejected inconsistent lifecycle data"
        ) from error

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise PersistenceError(
                "repository clock must return a timezone-aware datetime"
            )
        return value.astimezone(timezone.utc)

    @staticmethod
    def _validate_owner(owner_user_id: str | None, audit: AuditContext) -> None:
        if owner_user_id is not None and (
            not isinstance(owner_user_id, str)
            or not 1 <= len(owner_user_id) <= 256
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in owner_user_id
            )
        ):
            raise PersistenceError("analysis owner identifier is invalid")
        if owner_user_id is not None and owner_user_id != audit.user_id:
            raise PersistenceError("analysis owner must match the audited actor")

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
            created_at=datetime.fromisoformat(document["created_at"]),
            updated_at=datetime.fromisoformat(document["updated_at"]),
            started_at=(
                datetime.fromisoformat(document["started_at"])
                if document["started_at"] is not None
                else None
            ),
            finished_at=(
                datetime.fromisoformat(document["finished_at"])
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
            created_at=datetime.fromisoformat(document["created_at"]),
            updated_at=datetime.fromisoformat(document["updated_at"]),
            started_at=datetime.fromisoformat(document["started_at"]),
            finished_at=(
                datetime.fromisoformat(document["finished_at"])
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
