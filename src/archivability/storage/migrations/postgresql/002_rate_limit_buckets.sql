SET LOCAL search_path = archivability, pg_catalog;

CREATE TABLE rate_limit_buckets (
    user_id TEXT NOT NULL CHECK (length(user_id) BETWEEN 1 AND 256),
    operation TEXT NOT NULL CHECK (
        operation ~ '^[a-z][a-z0-9_.:-]{0,127}$'
    ),
    tokens DOUBLE PRECISION NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity BETWEEN 1 AND 10000),
    refill_seconds INTEGER NOT NULL CHECK (refill_seconds BETWEEN 1 AND 86400),
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, operation),
    CHECK (tokens >= 0 AND tokens <= capacity)
);

CREATE INDEX rate_limit_buckets_updated_idx
ON rate_limit_buckets (updated_at);
