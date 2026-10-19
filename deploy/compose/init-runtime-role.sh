#!/bin/sh
set -eu

: "${ARCHIVABILITY_RUNTIME_DB_PASSWORD:?runtime database password is required}"

psql \
  --set=ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --file /opt/arquivabilidade/init-runtime-role.sql
