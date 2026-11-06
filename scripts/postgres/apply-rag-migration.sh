#!/bin/sh
set -eu

: "${RUSA_AGENT_DB_PASSWORD:?RUSA_AGENT_DB_PASSWORD is required}"

DB_HOST="${RUSA_AGENT_DB_HOST:-rusa-agent-db}"
DB_PORT="${RUSA_AGENT_DB_PORT:-5432}"
DB_NAME="${RUSA_AGENT_DB_NAME:-rusa_agent}"
DB_USER="${RUSA_AGENT_DB_USER:-rusa_agent_owner}"
MIGRATION_FILE="${RUSA_RAG_MIGRATION_FILE:-/migrations/005_rag_knowledge_pgvector.sql}"

if [ ! -r "$MIGRATION_FILE" ]; then
  echo "RAG migration file is not readable" >&2
  exit 1
fi

export PGPASSWORD="$RUSA_AGENT_DB_PASSWORD"
exec psql \
  --no-psqlrc \
  --set ON_ERROR_STOP=1 \
  --host "$DB_HOST" \
  --port "$DB_PORT" \
  --username "$DB_USER" \
  --dbname "$DB_NAME" \
  --file "$MIGRATION_FILE"
