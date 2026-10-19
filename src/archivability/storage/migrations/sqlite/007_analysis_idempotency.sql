PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TABLE analysis_idempotency_keys (
    owner_user_id TEXT NOT NULL CHECK (length(owner_user_id) BETWEEN 1 AND 256),
    key_hash TEXT NOT NULL CHECK (length(key_hash) = 64),
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    analysis_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (owner_user_id, key_hash),
    FOREIGN KEY (analysis_id) REFERENCES analyses (analysis_id),
    UNIQUE (analysis_id)
) STRICT;

CREATE TRIGGER analysis_idempotency_keys_no_update
BEFORE UPDATE ON analysis_idempotency_keys BEGIN
    SELECT RAISE(ABORT, 'analysis idempotency keys cannot be changed');
END;

CREATE TRIGGER analysis_idempotency_keys_no_delete
BEFORE DELETE ON analysis_idempotency_keys BEGIN
    SELECT RAISE(ABORT, 'analysis idempotency keys cannot be deleted');
END;

INSERT INTO schema_migrations (version, applied_at)
VALUES ('007_analysis_idempotency', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
