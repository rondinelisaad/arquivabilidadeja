"""Core domain package for Arquivabilidade JA."""

from archivability.evidence import (
    Evidence,
    EvidenceSource,
    EvidenceValidationError,
    Observation,
    derive_indicator_result,
)
from archivability.methodology.loader import load_methodology
from archivability.methodology.models import (
    AssessmentResult,
    IndicatorResult,
    LegacyClearPlusInput,
    MethodologyConfig,
    ResultState,
)
from archivability.methodology.scoring import ScoringEngine
from archivability.storage import (
    AuditContext,
    DuplicateRecordError,
    IntegrityViolation,
    PersistenceError,
    SqliteEvidenceRepository,
    apply_sqlite_migrations,
    configure_sqlite_connection,
)

__all__ = [
    "AssessmentResult",
    "AuditContext",
    "DuplicateRecordError",
    "Evidence",
    "EvidenceSource",
    "EvidenceValidationError",
    "IndicatorResult",
    "IntegrityViolation",
    "LegacyClearPlusInput",
    "MethodologyConfig",
    "Observation",
    "PersistenceError",
    "ResultState",
    "ScoringEngine",
    "SqliteEvidenceRepository",
    "apply_sqlite_migrations",
    "configure_sqlite_connection",
    "derive_indicator_result",
    "load_methodology",
]
