PRAGMA foreign_keys = ON;

CREATE TABLE collection_jobs (
    job_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL UNIQUE REFERENCES analyses (analysis_id),
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'succeeded', 'failed')),
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 10),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    available_at TEXT NOT NULL,
    claimed_by TEXT,
    lease_expires_at TEXT,
    completed_at TEXT,
    error_code TEXT,
    document_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    stored_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (attempt_count <= max_attempts),
    CHECK (
        (state = 'pending' AND claimed_by IS NULL AND lease_expires_at IS NULL
            AND completed_at IS NULL)
        OR (state = 'running' AND claimed_by IS NOT NULL AND lease_expires_at IS NOT NULL
            AND completed_at IS NULL AND error_code IS NULL)
        OR (state = 'succeeded' AND claimed_by IS NULL AND lease_expires_at IS NULL
            AND completed_at IS NOT NULL AND error_code IS NULL)
        OR (state = 'failed' AND claimed_by IS NULL AND lease_expires_at IS NULL
            AND completed_at IS NOT NULL AND error_code IS NOT NULL
            AND attempt_count = max_attempts)
    )
);

CREATE INDEX collection_jobs_claim_idx
ON collection_jobs (state, available_at, lease_expires_at, created_at);

CREATE TRIGGER collection_jobs_no_delete
BEFORE DELETE ON collection_jobs
BEGIN
    SELECT RAISE(ABORT, 'collection jobs are append-only');
END;

INSERT INTO schema_migrations (version, applied_at)
VALUES ('005_collection_jobs', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
