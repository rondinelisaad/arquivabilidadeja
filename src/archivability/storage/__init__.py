"""Persistence ports and local development adapters."""

from typing import TYPE_CHECKING, Any

from archivability.storage.audit import AuditContext
from archivability.storage.errors import (
    ConcurrencyConflict,
    DuplicateRecordError,
    IntegrityViolation,
    PersistenceError,
)
from archivability.storage.postgresql import apply_postgresql_migrations
from archivability.storage.sqlite import (
    SqliteEvidenceRepository,
    SqliteLifecycleRepository,
    apply_sqlite_migrations,
    configure_sqlite_connection,
)

if TYPE_CHECKING:
    from archivability.storage.sqlite_jobs import SqliteAssessmentJobRepository

__all__ = [
    "AuditContext",
    "ConcurrencyConflict",
    "DuplicateRecordError",
    "IntegrityViolation",
    "PersistenceError",
    "SqliteEvidenceRepository",
    "SqliteAssessmentJobRepository",
    "SqliteLifecycleRepository",
    "apply_postgresql_migrations",
    "apply_sqlite_migrations",
    "configure_sqlite_connection",
]


def __getattr__(name: str) -> Any:
    if name == "SqliteAssessmentJobRepository":
        from archivability.storage.sqlite_jobs import SqliteAssessmentJobRepository

        return SqliteAssessmentJobRepository
    raise AttributeError(name)
