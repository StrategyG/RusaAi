#!/bin/sh
set -eu

: "${RUSA_RAG_DB_PASSWORD:?RUSA_RAG_DB_PASSWORD is required for the restricted RAG login}"

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --set=rag_password="$RUSA_RAG_DB_PASSWORD" \
  --file=/opt/rusa/rag-db-bootstrap.sql

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --file=/opt/rusa/migrations/007_rag_knowledge_pgvector.sql

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --file=/opt/rusa/migrations/008_rag_embedding_profiles.sql
