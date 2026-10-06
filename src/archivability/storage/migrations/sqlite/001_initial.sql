PRAGMA foreign_keys = ON;

CREATE TABLE schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
) STRICT;

CREATE TABLE observations (
    observation_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    document_json TEXT NOT NULL CHECK (json_valid(document_json)),
    stored_at TEXT NOT NULL,
    UNIQUE (observation_id, content_hash),
    UNIQUE (observation_id, content_hash, analysis_id),
    CHECK (length(content_hash) = 64)
) STRICT;

CREATE INDEX observations_analysis_idx ON observations (analysis_id, observation_id);

CREATE TABLE evidence (
    evidence_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL,
    indicator_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    document_json TEXT NOT NULL CHECK (json_valid(document_json)),
    stored_at TEXT NOT NULL,
    UNIQUE (evidence_id, content_hash),
    UNIQUE (evidence_id, analysis_id),
    UNIQUE (evidence_id, content_hash, analysis_id, indicator_id),
    CHECK (length(content_hash) = 64)
) STRICT;

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
) STRICT;

CREATE TABLE indicator_results (
    analysis_id TEXT NOT NULL,
    indicator_id TEXT NOT NULL,
    document_json TEXT NOT NULL CHECK (json_valid(document_json)),
    stored_at TEXT NOT NULL,
    PRIMARY KEY (analysis_id, indicator_id)
) STRICT;

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
) STRICT;

CREATE TABLE audit_events (
    event_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    user_id TEXT,
    ip_address TEXT,
    session_id TEXT,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('success', 'failure', 'unauthorized')),
    before_json TEXT CHECK (before_json IS NULL OR json_valid(before_json)),
    after_json TEXT CHECK (after_json IS NULL OR json_valid(after_json)),
    extra_json TEXT CHECK (extra_json IS NULL OR json_valid(extra_json))
) STRICT;

CREATE INDEX audit_events_resource_idx
    ON audit_events (resource, resource_id, timestamp);

CREATE TRIGGER observations_no_update
BEFORE UPDATE ON observations BEGIN
    SELECT RAISE(ABORT, 'observations are append-only');
END;
CREATE TRIGGER observations_no_delete
BEFORE DELETE ON observations BEGIN
    SELECT RAISE(ABORT, 'observations are append-only');
END;
CREATE TRIGGER evidence_no_update
BEFORE UPDATE ON evidence BEGIN
    SELECT RAISE(ABORT, 'evidence is append-only');
END;
CREATE TRIGGER evidence_no_delete
BEFORE DELETE ON evidence BEGIN
    SELECT RAISE(ABORT, 'evidence is append-only');
END;
CREATE TRIGGER evidence_sources_no_update
BEFORE UPDATE ON evidence_sources BEGIN
    SELECT RAISE(ABORT, 'evidence sources are append-only');
END;
CREATE TRIGGER evidence_sources_no_delete
BEFORE DELETE ON evidence_sources BEGIN
    SELECT RAISE(ABORT, 'evidence sources are append-only');
END;
CREATE TRIGGER indicator_results_no_update
BEFORE UPDATE ON indicator_results BEGIN
    SELECT RAISE(ABORT, 'indicator results are append-only');
END;
CREATE TRIGGER indicator_results_no_delete
BEFORE DELETE ON indicator_results BEGIN
    SELECT RAISE(ABORT, 'indicator results are append-only');
END;
CREATE TRIGGER indicator_result_evidence_no_update
BEFORE UPDATE ON indicator_result_evidence BEGIN
    SELECT RAISE(ABORT, 'indicator result evidence is append-only');
END;
CREATE TRIGGER indicator_result_evidence_no_delete
BEFORE DELETE ON indicator_result_evidence BEGIN
    SELECT RAISE(ABORT, 'indicator result evidence is append-only');
END;
CREATE TRIGGER audit_events_no_update
BEFORE UPDATE ON audit_events BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;
CREATE TRIGGER audit_events_no_delete
BEFORE DELETE ON audit_events BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;

INSERT INTO schema_migrations (version, applied_at)
VALUES ('001_initial', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
