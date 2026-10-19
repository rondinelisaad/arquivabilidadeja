\getenv migration_password ARCHIVABILITY_MIGRATION_DB_PASSWORD
\getenv runtime_password ARCHIVABILITY_RUNTIME_DB_PASSWORD
\getenv database_name POSTGRES_DB

CREATE ROLE archivability_migration
    LOGIN
    PASSWORD :'migration_password'
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS;

CREATE ROLE archivability_runtime
    LOGIN
    PASSWORD :'runtime_password'
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS;

GRANT CONNECT, CREATE ON DATABASE :"database_name" TO archivability_migration;
