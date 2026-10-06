PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE UNIQUE INDEX observations_identity_analysis_idx
    ON observations (observation_id, analysis_id);

CREATE TABLE assessment_jobs (
    job_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL,
    observation_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'succeeded', 'failed')),
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 10),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    available_at TEXT NOT NULL,
    claimed_by TEXT,
    lease_expires_at TEXT,
    completed_at TEXT,
    error_code TEXT,
    document_json TEXT NOT NULL CHECK (json_valid(document_json)),
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
    ),
    FOREIGN KEY (analysis_id) REFERENCES analyses (analysis_id),
    FOREIGN KEY (observation_id, analysis_id)
        REFERENCES observations (observation_id, analysis_id)
) STRICT;

CREATE INDEX assessment_jobs_claim_idx
    ON assessment_jobs (state, available_at, lease_expires_at, created_at);

CREATE TRIGGER assessment_jobs_guard_update
BEFORE UPDATE ON assessment_jobs BEGIN
    SELECT CASE
        WHEN NEW.job_id != OLD.job_id
          OR NEW.analysis_id != OLD.analysis_id
          OR NEW.observation_id != OLD.observation_id
          OR NEW.max_attempts != OLD.max_attempts
          OR NEW.created_at != OLD.created_at
          OR NEW.stored_at != OLD.stored_at
        THEN RAISE(ABORT, 'assessment job identity is immutable')
        WHEN NEW.revision != OLD.revision + 1
        THEN RAISE(ABORT, 'assessment job revision must increase by one')
    END;
END;

CREATE TRIGGER assessment_jobs_no_delete
BEFORE DELETE ON assessment_jobs BEGIN
    SELECT RAISE(ABORT, 'assessment jobs cannot be deleted');
END;

INSERT INTO schema_migrations (version, applied_at)
VALUES ('003_assessment_jobs', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
