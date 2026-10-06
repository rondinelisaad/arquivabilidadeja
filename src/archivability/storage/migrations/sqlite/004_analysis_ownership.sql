PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TABLE analysis_ownership (
    analysis_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL CHECK (
        length(owner_user_id) BETWEEN 1 AND 256
    ),
    created_at TEXT NOT NULL,
    FOREIGN KEY (analysis_id) REFERENCES analyses (analysis_id)
) STRICT;

CREATE INDEX analysis_ownership_owner_idx
ON analysis_ownership (owner_user_id, analysis_id);

CREATE TRIGGER analysis_ownership_no_update
BEFORE UPDATE ON analysis_ownership BEGIN
    SELECT RAISE(ABORT, 'analysis ownership cannot be changed');
END;

CREATE TRIGGER analysis_ownership_no_delete
BEFORE DELETE ON analysis_ownership BEGIN
    SELECT RAISE(ABORT, 'analysis ownership cannot be deleted');
END;

INSERT INTO schema_migrations (version, applied_at)
VALUES ('004_analysis_ownership', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
