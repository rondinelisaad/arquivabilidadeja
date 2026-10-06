-- Execute with psql as the schema owner, for example:
-- psql --set=runtime_role=archivability_runtime \
--      --set=migration_role=archivability_migration \
--      --set=database_name=archivability -f runtime_grants.sql
\if :{?runtime_role}
\else
\echo 'runtime_role is required'
\quit
\endif
\if :{?migration_role}
\else
\echo 'migration_role is required'
\quit
\endif
\if :{?database_name}
\else
\echo 'database_name is required'
\quit
\endif

REVOKE ALL ON DATABASE :"database_name" FROM PUBLIC;
REVOKE ALL ON DATABASE :"database_name" FROM :"runtime_role";
GRANT CONNECT ON DATABASE :"database_name" TO :"runtime_role";

REVOKE ALL ON SCHEMA archivability FROM PUBLIC;
REVOKE ALL ON SCHEMA archivability FROM :"runtime_role";
GRANT USAGE ON SCHEMA archivability TO :"runtime_role";

REVOKE ALL ON ALL TABLES IN SCHEMA archivability FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA archivability FROM :"runtime_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA archivability FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA archivability FROM :"runtime_role";

GRANT SELECT, INSERT ON TABLE
    archivability.observations,
    archivability.evidence,
    archivability.evidence_sources,
    archivability.indicator_results,
    archivability.indicator_result_evidence,
    archivability.analysis_ownership,
    archivability.audit_events
TO :"runtime_role";

GRANT SELECT, INSERT, UPDATE ON TABLE
    archivability.analyses,
    archivability.attempts,
    archivability.assessment_jobs
TO :"runtime_role";

-- DELETE is intentionally limited to expired distributed rate-limit buckets.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    archivability.rate_limit_buckets
TO :"runtime_role";

ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA archivability
    REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA archivability
    REVOKE ALL ON TABLES FROM :"runtime_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA archivability
    REVOKE ALL ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA archivability
    REVOKE ALL ON FUNCTIONS FROM :"runtime_role";

-- Future tables receive no runtime access implicitly. Review each migration and
-- grant only the DML operations the new table requires.
