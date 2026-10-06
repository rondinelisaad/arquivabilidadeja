"""Core domain package for Arquivabilidade JA."""

from archivability.evidence import (
    Evidence,
    EvidenceSource,
    EvidenceValidationError,
    Observation,
    derive_indicator_result,
)
from archivability.lifecycle import (
    Analysis,
    AnalysisOrchestrator,
    AnalysisState,
    Attempt,
    AttemptState,
    LifecycleError,
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
    ConcurrencyConflict,
    DuplicateRecordError,
    IntegrityViolation,
    PersistenceError,
    SqliteEvidenceRepository,
    SqliteLifecycleRepository,
    apply_sqlite_migrations,
    configure_sqlite_connection,
)

__all__ = [
    "AssessmentResult",
    "Analysis",
    "AnalysisOrchestrator",
    "AnalysisState",
    "AuditContext",
    "Attempt",
    "AttemptState",
    "ConcurrencyConflict",
    "DuplicateRecordError",
    "Evidence",
    "EvidenceSource",
    "EvidenceValidationError",
    "IndicatorResult",
    "IntegrityViolation",
    "LegacyClearPlusInput",
    "LifecycleError",
    "MethodologyConfig",
    "Observation",
    "PersistenceError",
    "ResultState",
    "ScoringEngine",
    "SqliteEvidenceRepository",
    "SqliteLifecycleRepository",
    "apply_sqlite_migrations",
    "configure_sqlite_connection",
    "derive_indicator_result",
    "load_methodology",
]
