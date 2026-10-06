PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TABLE analyses (
    analysis_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (
        state IN ('requested', 'running', 'completed', 'partially_completed', 'failed', 'cancelled')
    ),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    document_json TEXT NOT NULL CHECK (json_valid(document_json)),
    stored_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE INDEX analyses_state_idx ON analyses (state, updated_at);

CREATE TABLE attempts (
    attempt_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    state TEXT NOT NULL CHECK (state IN ('running', 'succeeded', 'failed', 'cancelled')),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    document_json TEXT NOT NULL CHECK (json_valid(document_json)),
    stored_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (analysis_id, sequence),
    FOREIGN KEY (analysis_id) REFERENCES analyses (analysis_id)
) STRICT;

CREATE INDEX attempts_analysis_idx ON attempts (analysis_id, sequence);

CREATE TRIGGER analyses_guard_update
BEFORE UPDATE ON analyses BEGIN
    SELECT CASE
        WHEN NEW.analysis_id != OLD.analysis_id
        THEN RAISE(ABORT, 'analysis identity is immutable')
        WHEN NEW.revision != OLD.revision + 1
        THEN RAISE(ABORT, 'analysis revision must increase by one')
    END;
END;

CREATE TRIGGER analyses_no_delete
BEFORE DELETE ON analyses BEGIN
    SELECT RAISE(ABORT, 'analyses cannot be deleted');
END;

CREATE TRIGGER attempts_guard_update
BEFORE UPDATE ON attempts BEGIN
    SELECT CASE
        WHEN NEW.attempt_id != OLD.attempt_id
          OR NEW.analysis_id != OLD.analysis_id
          OR NEW.sequence != OLD.sequence
        THEN RAISE(ABORT, 'attempt identity is immutable')
        WHEN NEW.revision != OLD.revision + 1
        THEN RAISE(ABORT, 'attempt revision must increase by one')
    END;
END;

CREATE TRIGGER attempts_no_delete
BEFORE DELETE ON attempts BEGIN
    SELECT RAISE(ABORT, 'attempts cannot be deleted');
END;

INSERT INTO schema_migrations (version, applied_at)
VALUES ('002_analysis_lifecycle', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
