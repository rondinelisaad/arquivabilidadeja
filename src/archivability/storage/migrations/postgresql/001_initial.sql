SET LOCAL search_path = pg_catalog, archivability;

CREATE TABLE analyses (
    analysis_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (
        state IN ('requested', 'running', 'completed', 'partially_completed', 'failed', 'cancelled')
    ),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    document_json JSONB NOT NULL,
    stored_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX analyses_state_idx ON analyses (state, updated_at);

CREATE TABLE attempts (
    attempt_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES analyses (analysis_id),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    state TEXT NOT NULL CHECK (state IN ('running', 'succeeded', 'failed', 'cancelled')),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    document_json JSONB NOT NULL,
    stored_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (analysis_id, sequence)
);

CREATE INDEX attempts_analysis_idx ON attempts (analysis_id, sequence);

CREATE TABLE observations (
    observation_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    document_json JSONB NOT NULL,
    stored_at TIMESTAMPTZ NOT NULL,
    UNIQUE (observation_id, content_hash),
    UNIQUE (observation_id, analysis_id),
    UNIQUE (observation_id, content_hash, analysis_id)
);

CREATE INDEX observations_analysis_idx ON observations (analysis_id, observation_id);

CREATE TABLE evidence (
    evidence_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL,
    indicator_id TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    document_json JSONB NOT NULL,
    stored_at TIMESTAMPTZ NOT NULL,
    UNIQUE (evidence_id, content_hash),
    UNIQUE (evidence_id, analysis_id),
    UNIQUE (evidence_id, content_hash, analysis_id, indicator_id)
);

CREATE INDEX evidence_analysis_indicator_idx
ON evidence (analysis_id, indicator_id, evidence_id);

CREATE TABLE evidence_sources (
    evidence_id TEXT NOT NULL,
    source_order INTEGER NOT NULL CHECK (source_order >= 0),
    analysis_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    observation_hash TEXT NOT NULL CHECK (length(observation_hash) = 64),
    PRIMARY KEY (evidence_id, source_order),
    UNIQUE (evidence_id, observation_id),
    FOREIGN KEY (evidence_id, analysis_id)
        REFERENCES evidence (evidence_id, analysis_id),
    FOREIGN KEY (observation_id, observation_hash, analysis_id)
        REFERENCES observations (observation_id, content_hash, analysis_id)
);

CREATE TABLE indicator_results (
    analysis_id TEXT NOT NULL,
    indicator_id TEXT NOT NULL,
    document_json JSONB NOT NULL,
    stored_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (analysis_id, indicator_id)
);

CREATE TABLE indicator_result_evidence (
    analysis_id TEXT NOT NULL,
    indicator_id TEXT NOT NULL,
    evidence_order INTEGER NOT NULL CHECK (evidence_order >= 0),
    evidence_id TEXT NOT NULL,
    evidence_hash TEXT NOT NULL CHECK (length(evidence_hash) = 64),
    PRIMARY KEY (analysis_id, indicator_id, evidence_order),
    UNIQUE (analysis_id, indicator_id, evidence_id),
    FOREIGN KEY (analysis_id, indicator_id)
        REFERENCES indicator_results (analysis_id, indicator_id),
    FOREIGN KEY (evidence_id, evidence_hash, analysis_id, indicator_id)
        REFERENCES evidence (evidence_id, content_hash, analysis_id, indicator_id)
);

CREATE TABLE assessment_jobs (
    job_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES analyses (analysis_id),
    observation_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'succeeded', 'failed')),
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 10),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    available_at TIMESTAMPTZ NOT NULL,
    claimed_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_code TEXT,
    document_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    stored_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
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
    FOREIGN KEY (observation_id, analysis_id)
        REFERENCES observations (observation_id, analysis_id)
);

CREATE INDEX assessment_jobs_claim_idx
ON assessment_jobs (state, available_at, lease_expires_at, created_at);

CREATE TABLE analysis_ownership (
    analysis_id TEXT PRIMARY KEY REFERENCES analyses (analysis_id),
    owner_user_id TEXT NOT NULL CHECK (length(owner_user_id) BETWEEN 1 AND 256),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX analysis_ownership_owner_idx
ON analysis_ownership (owner_user_id, analysis_id);

CREATE TABLE audit_events (
    event_id TEXT PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    user_id TEXT,
    ip_address INET,
    session_id TEXT,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('success', 'failure', 'unauthorized')),
    before_json JSONB,
    after_json JSONB,
    extra_json JSONB
);

CREATE INDEX audit_events_resource_idx
ON audit_events (resource, resource_id, timestamp);

CREATE FUNCTION reject_append_only_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
        USING ERRCODE = 'integrity_constraint_violation';
END;
$function$;

CREATE FUNCTION guard_analysis_update()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.analysis_id IS DISTINCT FROM OLD.analysis_id THEN
        RAISE EXCEPTION 'analysis identity is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.revision != OLD.revision + 1 THEN
        RAISE EXCEPTION 'analysis revision must increase by one'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$;

CREATE FUNCTION guard_attempt_update()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.attempt_id IS DISTINCT FROM OLD.attempt_id
        OR NEW.analysis_id IS DISTINCT FROM OLD.analysis_id
        OR NEW.sequence IS DISTINCT FROM OLD.sequence THEN
        RAISE EXCEPTION 'attempt identity is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.revision != OLD.revision + 1 THEN
        RAISE EXCEPTION 'attempt revision must increase by one'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$;

CREATE FUNCTION guard_assessment_job_update()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.job_id IS DISTINCT FROM OLD.job_id
        OR NEW.analysis_id IS DISTINCT FROM OLD.analysis_id
        OR NEW.observation_id IS DISTINCT FROM OLD.observation_id
        OR NEW.max_attempts IS DISTINCT FROM OLD.max_attempts
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR NEW.stored_at IS DISTINCT FROM OLD.stored_at THEN
        RAISE EXCEPTION 'assessment job identity is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.revision != OLD.revision + 1 THEN
        RAISE EXCEPTION 'assessment job revision must increase by one'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$;

CREATE TRIGGER analyses_guard_update
BEFORE UPDATE ON analyses
FOR EACH ROW EXECUTE FUNCTION guard_analysis_update();
CREATE TRIGGER analyses_no_delete
BEFORE DELETE ON analyses
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

CREATE TRIGGER attempts_guard_update
BEFORE UPDATE ON attempts
FOR EACH ROW EXECUTE FUNCTION guard_attempt_update();
CREATE TRIGGER attempts_no_delete
BEFORE DELETE ON attempts
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

CREATE TRIGGER assessment_jobs_guard_update
BEFORE UPDATE ON assessment_jobs
FOR EACH ROW EXECUTE FUNCTION guard_assessment_job_update();
CREATE TRIGGER assessment_jobs_no_delete
BEFORE DELETE ON assessment_jobs
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

CREATE TRIGGER observations_no_update_or_delete
BEFORE UPDATE OR DELETE ON observations
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
CREATE TRIGGER evidence_no_update_or_delete
BEFORE UPDATE OR DELETE ON evidence
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
CREATE TRIGGER evidence_sources_no_update_or_delete
BEFORE UPDATE OR DELETE ON evidence_sources
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
CREATE TRIGGER indicator_results_no_update_or_delete
BEFORE UPDATE OR DELETE ON indicator_results
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
CREATE TRIGGER indicator_result_evidence_no_update_or_delete
BEFORE UPDATE OR DELETE ON indicator_result_evidence
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
CREATE TRIGGER analysis_ownership_no_update_or_delete
BEFORE UPDATE OR DELETE ON analysis_ownership
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
CREATE TRIGGER audit_events_no_update_or_delete
BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
