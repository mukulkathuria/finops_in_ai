#!/usr/bin/env bash
# First-start bootstrap (runs once on an empty data dir, as the superuser).
# Creates the four least-privilege roles and the application database.
# Passwords come only from the environment; nothing secret lives in this image.
set -euo pipefail

: "${FINOPS_DB:=finops}"
: "${FINOPS_MIGRATOR_PASSWORD:?FINOPS_MIGRATOR_PASSWORD is required}"
: "${FINOPS_APP_PASSWORD:?FINOPS_APP_PASSWORD is required}"
: "${FINOPS_WORKER_PASSWORD:?FINOPS_WORKER_PASSWORD is required}"
: "${FINOPS_READONLY_PASSWORD:?FINOPS_READONLY_PASSWORD is required}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  --set=migrator_pw="$FINOPS_MIGRATOR_PASSWORD" \
  --set=app_pw="$FINOPS_APP_PASSWORD" \
  --set=worker_pw="$FINOPS_WORKER_PASSWORD" \
  --set=readonly_pw="$FINOPS_READONLY_PASSWORD" \
  -f /opt/finops/sql/roles.sql

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  --set=db="$FINOPS_DB" -f /opt/finops/sql/create-database.sql

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$FINOPS_DB" \
  -f /opt/finops/sql/database-setup.sql
