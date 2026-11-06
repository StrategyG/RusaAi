#!/bin/sh
set -eu

: "${RUSA_AGENT_WORKER_DB_PASSWORD:?RUSA_AGENT_WORKER_DB_PASSWORD is required}"
: "${RUSA_AGENT_API_DB_PASSWORD:?RUSA_AGENT_API_DB_PASSWORD is required}"

case "$RUSA_AGENT_WORKER_DB_PASSWORD" in
  *[!A-Za-z0-9_-]*|'') echo "Worker DB password must be URL-safe alphanumeric, underscore, or dash" >&2; exit 1 ;;
esac
case "$RUSA_AGENT_API_DB_PASSWORD" in
  *[!A-Za-z0-9_-]*|'') echo "API DB password must be URL-safe alphanumeric, underscore, or dash" >&2; exit 1 ;;
esac

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE rusa_agent_worker
    LOGIN PASSWORD '$RUSA_AGENT_WORKER_DB_PASSWORD'
    NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
CREATE ROLE rusa_agent_api
    LOGIN PASSWORD '$RUSA_AGENT_API_DB_PASSWORD'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM rusa_agent_api;
GRANT CONNECT ON DATABASE $POSTGRES_DB TO rusa_agent_worker, rusa_agent_api;
GRANT USAGE, CREATE ON SCHEMA public TO rusa_agent_worker;
GRANT USAGE ON SCHEMA public TO rusa_agent_api;

-- Runtime migrations grant the API role only the minimum task-queue privileges.
SQL
