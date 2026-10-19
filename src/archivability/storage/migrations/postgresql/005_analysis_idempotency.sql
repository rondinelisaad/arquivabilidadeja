SET LOCAL search_path = archivability, pg_catalog;

CREATE TABLE analysis_idempotency_keys (
    owner_user_id TEXT NOT NULL CHECK (length(owner_user_id) BETWEEN 1 AND 256),
    key_hash TEXT NOT NULL CHECK (length(key_hash) = 64),
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    analysis_id TEXT NOT NULL UNIQUE REFERENCES analyses (analysis_id),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (owner_user_id, key_hash)
);

CREATE TRIGGER analysis_idempotency_keys_no_update
BEFORE UPDATE OR DELETE ON analysis_idempotency_keys
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
