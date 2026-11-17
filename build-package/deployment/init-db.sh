#!/bin/sh
# Local bootstrap only. Existing volumes require deliberate role/secret migration.
# app_runtime: API + task workers (always inside a workspace context, subject to RLS).
# app_system:  dispatcher/scheduler/reconciler; sees only scheduling columns granted by migrations.
# app_resolver: NOLOGIN owner of SECURITY DEFINER lookup functions (see contracts/core_schema.sql).
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v runtime_password="$APP_DB_PASSWORD" -v system_password="$APP_SYSTEM_DB_PASSWORD" \
  -v ddl_role="$POSTGRES_USER" <<'SQL'
CREATE ROLE app_runtime LOGIN PASSWORD :'runtime_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE app_system LOGIN PASSWORD :'system_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE app_resolver NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
GRANT app_resolver TO :"ddl_role";
REVOKE ALL ON DATABASE codehover FROM PUBLIC;
GRANT CONNECT ON DATABASE codehover TO app_runtime, app_system;
GRANT USAGE ON SCHEMA public TO app_runtime, app_system, app_resolver;
-- Broad defaults for the runtime role only; migrations revoke UPDATE/DELETE on append-only tables
-- and grant app_system/app_resolver explicit column-level privileges.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO app_runtime;
SQL
