class PersistenceError(RuntimeError):
    """Base error for persistence operations."""


class DuplicateRecordError(PersistenceError):
    """Raised when an immutable identity has already been stored."""


class IntegrityViolation(PersistenceError):
    """Raised when stored content or provenance no longer verifies."""


class ConcurrencyConflict(PersistenceError):
    """Raised when a stale revision attempts to change lifecycle state."""
