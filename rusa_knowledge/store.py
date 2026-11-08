"""PostgreSQL/pgvector knowledge store with transaction-local tenant isolation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any
from uuid import UUID

from .safety import validate_document_metadata

TENANT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
COLLECTION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
AGENT_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
PROJECT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


def _document_fingerprint(
    source_uri: str,
    title: str,
    content_hash: str,
    metadata: dict[str, Any],
) -> str:
    payload = json.dumps(
        {
            "source_uri": source_uri,
            "title": title,
            "content_hash": content_hash,
            "metadata": metadata,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _vector_literal(vector: list[float], dimensions: int) -> str:
    if len(vector) != dimensions:
        raise ValueError("embedding dimension does not match the pgvector column")
    values = [float(value) for value in vector]
    if not all(math.isfinite(value) and abs(value) <= 1_000_000 for value in values):
        raise ValueError("embedding vector contains an invalid or out-of-range value")
    norm_squared = sum(value * value for value in values)
    if not math.isfinite(norm_squared) or norm_squared <= 0:
        raise ValueError("embedding vector must have a finite, non-zero norm")
    return "[" + ",".join(format(value, ".9g") for value in values) + "]"


class PostgresKnowledgeStore:
    """Shared store. Apply the versioned SQL migration before enabling this service."""

    def __init__(
        self,
        dsn: str,
        *,
        dimensions: int = 1536,
        min_pool_size: int = 1,
        max_pool_size: int = 10,
        db_role: str | None = None,
    ) -> None:
        if not dsn:
            raise ValueError("knowledge database DSN is required")
        if dimensions != 1536:
            raise ValueError("the current migration fixes embedding dimensions at 1536")
        if not 1 <= min_pool_size <= max_pool_size <= 50:
            raise ValueError("database pool limits are invalid")
        if db_role is not None and not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", db_role):
            raise ValueError("knowledge database role name is invalid")
        self.db_role = db_role
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise RuntimeError("install psycopg with the pool extra to use the knowledge store") from exc
        self.dimensions = dimensions
        self._pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_pool_size,
            max_size=max_pool_size,
            timeout=5.0,
            open=True,
        )

    @staticmethod
    def _validate_scope(tenant_id: str, collection: str) -> None:
        if not isinstance(tenant_id, str) or not TENANT_PATTERN.fullmatch(tenant_id):
            raise ValueError("tenant_id is invalid")
        if not isinstance(collection, str) or not COLLECTION_PATTERN.fullmatch(collection):
            raise ValueError("collection is invalid")

    @staticmethod
    def _validate_agent_id(agent_id: str | None) -> None:
        if agent_id is not None and (
            not isinstance(agent_id, str) or not AGENT_PATTERN.fullmatch(agent_id)
        ):
            raise ValueError("agent_id is invalid")

    @staticmethod
    def _validate_project_id(project_id: str | None) -> None:
        if project_id is not None and (
            not isinstance(project_id, str) or not PROJECT_PATTERN.fullmatch(project_id)
        ):
            raise ValueError("project_id is invalid")

    def _set_db_role(self, connection: Any) -> None:
        if self.db_role:
            connection.execute("SELECT set_config('role', %s, true)", (self.db_role,))

    def _set_tenant(self, connection: Any, tenant_id: str) -> None:
        self._set_db_role(connection)
        connection.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))

    def check_ready(self) -> bool:
        with self._pool.connection() as connection:
            self._set_db_role(connection)
            row = connection.execute(
                """
                SELECT
                    EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector'),
                    COALESCE((
                        SELECT relrowsecurity AND relforcerowsecurity
                        FROM pg_class WHERE oid = to_regclass('public.rusa_knowledge_documents')
                    ), false),
                    COALESCE((
                        SELECT relrowsecurity AND relforcerowsecurity
                        FROM pg_class WHERE oid = to_regclass('public.rusa_knowledge_chunks')
                    ), false),
                    COALESCE((
                        SELECT format_type(a.atttypid, a.atttypmod) = 'vector(1536)'
                        FROM pg_attribute a
                        WHERE a.attrelid = to_regclass('public.rusa_knowledge_chunks')
                          AND a.attname = 'embedding' AND NOT a.attisdropped
                    ), false),
                    EXISTS (
                        SELECT 1 FROM pg_policies
                        WHERE schemaname='public'
                          AND tablename='rusa_knowledge_documents'
                          AND policyname='rusa_knowledge_documents_tenant_isolation'
                    ),
                    EXISTS (
                        SELECT 1 FROM pg_policies
                        WHERE schemaname='public'
                          AND tablename='rusa_knowledge_chunks'
                          AND policyname='rusa_knowledge_chunks_tenant_isolation'
                    ),
                    has_table_privilege(
                        current_user, 'public.rusa_knowledge_documents', 'SELECT'
                    ),
                    has_table_privilege(
                        current_user, 'public.rusa_knowledge_chunks', 'SELECT'
                    ),
                    EXISTS (
                        SELECT 1 FROM pg_roles
                        WHERE rolname=current_user AND NOT rolsuper AND NOT rolbypassrls
                    ),
                    to_regclass('public.rusa_knowledge_documents_scope_idx') IS NOT NULL,
                    to_regclass('public.rusa_knowledge_documents_agent_ids_gin') IS NOT NULL,
                    to_regclass('public.rusa_knowledge_documents_project_scope_idx') IS NOT NULL,
                    COALESCE((
                        SELECT relrowsecurity AND relforcerowsecurity
                        FROM pg_class WHERE oid = to_regclass('public.rusa_knowledge_document_versions')
                    ), false),
                    COALESCE((
                        SELECT relrowsecurity AND relforcerowsecurity
                        FROM pg_class WHERE oid = to_regclass('public.rusa_knowledge_version_chunks')
                    ), false),
                    EXISTS (
                        SELECT 1 FROM pg_policies
                        WHERE schemaname='public'
                          AND tablename='rusa_knowledge_document_versions'
                          AND policyname='rusa_knowledge_document_versions_tenant_isolation'
                    ),
                    EXISTS (
                        SELECT 1 FROM pg_policies
                        WHERE schemaname='public'
                          AND tablename='rusa_knowledge_version_chunks'
                          AND policyname='rusa_knowledge_version_chunks_tenant_isolation'
                    ),
                    has_table_privilege(
                        current_user, 'public.rusa_knowledge_document_versions', 'SELECT'
                    ),
                    has_table_privilege(
                        current_user, 'public.rusa_knowledge_version_chunks', 'SELECT'
                    ),
                    to_regclass('public.rusa_knowledge_versions_document_idx') IS NOT NULL
                """
            ).fetchone()
        return bool(row and all(row))

    def ingest_document(
        self,
        *,
        tenant_id: str,
        collection: str,
        source_uri: str,
        title: str,
        content_hash: str,
        metadata: dict[str, Any],
        chunks: list[str],
        embeddings: list[list[float]],
    ) -> str:
        self._validate_scope(tenant_id, collection)
        if (
            not isinstance(source_uri, str)
            or not source_uri.strip()
            or len(source_uri) > 2048
            or len(source_uri.encode("utf-8")) > 2048
            or any(ord(character) < 32 for character in source_uri)
        ):
            raise ValueError("source_uri must be a bounded string without control characters")
        if not isinstance(title, str) or not title.strip() or len(title) > 256:
            raise ValueError("title must contain 1 to 256 characters")
        if not isinstance(metadata, dict):
            raise ValueError("document metadata must be a JSON object")
        if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            raise ValueError("content_hash must be a SHA-256 hex digest")
        if not chunks or len(chunks) != len(embeddings) or len(chunks) > 10_000:
            raise ValueError("document chunks and embeddings are invalid")
        metadata_json = validate_document_metadata(source_uri, title, metadata)
        if len(metadata_json.encode("utf-8")) > 16_384:
            raise ValueError("document metadata exceeds 16 KiB")

        rows = []
        for index, (content, embedding) in enumerate(zip(chunks, embeddings, strict=True)):
            if not content.strip() or len(content) > 20_000:
                raise ValueError("chunk content is empty or exceeds the configured limit")
            rows.append(
                (
                    index,
                    content,
                    _vector_literal(embedding, self.dimensions),
                    json.dumps({"chunk_index": index}, ensure_ascii=False),
                )
            )

        current_metadata = json.loads(metadata_json)
        current_fingerprint = _document_fingerprint(
            source_uri, title, content_hash, current_metadata
        )
        with self._pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            lock_key = f"{tenant_id}\\x1f{collection}\\x1f{source_uri}"
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (lock_key,),
            )
            existing = connection.execute(
                """
                SELECT document_id, title, source_uri, content_hash, metadata
                FROM rusa_knowledge_documents
                WHERE tenant_id=%s AND collection=%s AND source_uri=%s
                FOR UPDATE
                """,
                (tenant_id, collection, source_uri),
            ).fetchone()
            if existing is None:
                document = connection.execute(
                    """
                    INSERT INTO rusa_knowledge_documents
                        (tenant_id, collection, source_uri, title, content_hash, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    RETURNING document_id
                    """,
                    (tenant_id, collection, source_uri, title, content_hash, metadata_json),
                ).fetchone()
                document_id = str(document[0])
            else:
                document_id = str(existing[0])
                previous_metadata = existing[4]
                if isinstance(previous_metadata, str):
                    previous_metadata = json.loads(previous_metadata)
                previous_fingerprint = _document_fingerprint(
                    existing[2],
                    existing[1],
                    str(existing[3]).strip(),
                    previous_metadata,
                )
                if previous_fingerprint != current_fingerprint:
                    previous_metadata_json = json.dumps(
                        previous_metadata,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    version = connection.execute(
                        """
                        INSERT INTO rusa_knowledge_document_versions
                            (tenant_id, document_id, collection, source_uri, title,
                             content_hash, version_fingerprint, metadata)
                        VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (tenant_id, document_id, version_fingerprint)
                        DO NOTHING
                        RETURNING version_id
                        """,
                        (
                            tenant_id, document_id, collection, existing[2], existing[1],
                            str(existing[3]).strip(), previous_fingerprint, previous_metadata_json,
                        ),
                    ).fetchone()
                    if version is not None:
                        connection.execute(
                            """
                            INSERT INTO rusa_knowledge_version_chunks
                                (tenant_id, version_id, chunk_index, content, embedding, metadata)
                            SELECT tenant_id, %s::uuid, chunk_index, content, embedding, metadata
                            FROM rusa_knowledge_chunks
                            WHERE tenant_id=%s AND document_id=%s::uuid
                            ORDER BY chunk_index
                            """,
                            (str(version[0]), tenant_id, document_id),
                        )
                connection.execute(
                    """
                    UPDATE rusa_knowledge_documents
                    SET title=%s, content_hash=%s, metadata=%s::jsonb, updated_at=now()
                    WHERE tenant_id=%s AND document_id=%s::uuid
                    """,
                    (title, content_hash, metadata_json, tenant_id, document_id),
                )
            connection.execute(
                "DELETE FROM rusa_knowledge_chunks WHERE tenant_id=%s AND document_id=%s::uuid",
                (tenant_id, document_id),
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO rusa_knowledge_chunks
                        (tenant_id, document_id, collection, chunk_index, content, embedding, metadata)
                    VALUES (%s, %s::uuid, %s, %s, %s, %s::vector, %s::jsonb)
                    """,
                    [
                        (
                            tenant_id,
                            document_id,
                            collection,
                            index,
                            content,
                            vector,
                            chunk_metadata,
                        )
                        for index, content, vector, chunk_metadata in rows
                    ],
                )
        return document_id

    def search(
        self,
        *,
        tenant_id: str,
        collection: str,
        query: str,
        query_embedding: list[float],
        limit: int = 5,
        agent_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self._validate_scope(tenant_id, collection)
        self._validate_agent_id(agent_id)
        self._validate_project_id(project_id)
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("query must contain 1 to 2000 characters")
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        vector = _vector_literal(query_embedding, self.dimensions)
        candidate_limit = min(max(limit * 4, 20), 100)

        sql = """
            WITH scoped_chunks AS NOT MATERIALIZED (
                SELECT c.chunk_id, c.embedding, c.search_vector
                FROM rusa_knowledge_chunks c
                JOIN rusa_knowledge_documents d
                  ON d.tenant_id=c.tenant_id AND d.document_id=c.document_id
                WHERE c.tenant_id=%s AND c.collection=%s
                  AND (
                      COALESCE(d.metadata->>'knowledge_scope', 'shared') = 'shared'
                      OR (
                          %s::text IS NOT NULL
                          AND COALESCE(d.metadata->'agent_ids', '[]'::jsonb) ? %s
                          AND (
                              d.metadata->>'project_id' IS NULL
                              OR (
                                  %s::text IS NOT NULL
                                  AND d.metadata->>'project_id' = %s
                              )
                          )
                      )
                  )
            ),
            vector_candidates AS (
                SELECT chunk_id,
                       ROW_NUMBER() OVER (ORDER BY embedding <=> %s::vector) AS vector_rank
                FROM scoped_chunks
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            ),
            lexical_candidates AS (
                SELECT chunk_id,
                       ROW_NUMBER() OVER (
                           ORDER BY ts_rank(search_vector, plainto_tsquery('simple', %s)) DESC
                       ) AS lexical_rank
                FROM scoped_chunks
                WHERE search_vector @@ plainto_tsquery('simple', %s)
                ORDER BY ts_rank(search_vector, plainto_tsquery('simple', %s)) DESC
                LIMIT %s
            ),
            combined AS (
                SELECT COALESCE(v.chunk_id, l.chunk_id) AS chunk_id,
                       0.7 * COALESCE(1.0 / (60 + v.vector_rank), 0.0)
                     + 0.3 * COALESCE(1.0 / (60 + l.lexical_rank), 0.0) AS rrf_score
                FROM vector_candidates v
                FULL OUTER JOIN lexical_candidates l ON v.chunk_id = l.chunk_id
            )
            SELECT c.chunk_id, c.document_id, c.chunk_index, c.content,
                   d.title, d.source_uri, d.metadata, combined.rrf_score,
                   1 - (c.embedding <=> %s::vector) AS vector_similarity
            FROM combined
            JOIN rusa_knowledge_chunks c
              ON c.chunk_id = combined.chunk_id AND c.tenant_id=%s
            JOIN rusa_knowledge_documents d
              ON d.document_id=c.document_id AND d.tenant_id=c.tenant_id
            ORDER BY combined.rrf_score DESC
            LIMIT %s
        """
        params = (
            tenant_id, collection, agent_id, agent_id, project_id, project_id,
            vector, vector, candidate_limit,
            query, query, query, candidate_limit,
            vector, tenant_id, limit,
        )
        with self._pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            rows = connection.execute(sql, params).fetchall()
        return [
            {
                "chunk_id": str(row[0]),
                "document_id": str(row[1]),
                "chunk_index": int(row[2]),
                "content": row[3],
                "title": row[4],
                "source_uri": row[5],
                "metadata": row[6],
                "score": float(row[7]),
                "vector_similarity": float(row[8]),
            }
            for row in rows
        ]

    def get_document(
        self,
        *,
        tenant_id: str,
        collection: str,
        document_id: str,
        max_chars: int = 20_000,
        agent_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        self._validate_scope(tenant_id, collection)
        self._validate_agent_id(agent_id)
        self._validate_project_id(project_id)
        try:
            UUID(document_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("document_id must be a UUID") from exc
        if not 1 <= max_chars <= 50_000:
            raise ValueError("max_chars must be between 1 and 50000")
        with self._pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            document = connection.execute(
                """
                SELECT document_id, title, source_uri, metadata, content_hash, created_at, updated_at
                FROM rusa_knowledge_documents
                WHERE tenant_id=%s AND collection=%s AND document_id=%s::uuid
                  AND (
                      COALESCE(metadata->>'knowledge_scope', 'shared') = 'shared'
                      OR (
                          %s::text IS NOT NULL
                          AND COALESCE(metadata->'agent_ids', '[]'::jsonb) ? %s
                          AND (
                              metadata->>'project_id' IS NULL
                              OR (
                                  %s::text IS NOT NULL
                                  AND metadata->>'project_id' = %s
                              )
                          )
                      )
                  )
                """,
                (tenant_id, collection, document_id, agent_id, agent_id, project_id, project_id),
            ).fetchone()
            if document is None:
                return None
            chunks = connection.execute(
                """
                SELECT content FROM rusa_knowledge_chunks
                WHERE tenant_id=%s AND collection=%s AND document_id=%s::uuid
                ORDER BY chunk_index
                """,
                (tenant_id, collection, document_id),
            ).fetchall()
        content = "\n\n".join(row[0] for row in chunks)[:max_chars]
        return {
            "document_id": str(document[0]),
            "title": document[1],
            "source_uri": document[2],
            "metadata": document[3],
            "content_hash": document[4],
            "created_at": document[5].isoformat() if document[5] else None,
            "updated_at": document[6].isoformat() if document[6] else None,
            "content": content,
            "truncated": sum(len(row[0]) for row in chunks) > max_chars,
        }

    def list_document_versions(
        self,
        *,
        tenant_id: str,
        collection: str,
        document_id: str,
        agent_id: str | None = None,
        project_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self._validate_scope(tenant_id, collection)
        self._validate_agent_id(agent_id)
        self._validate_project_id(project_id)
        try:
            UUID(document_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("document_id must be a UUID") from exc
        if not 1 <= limit <= 100:
            raise ValueError("version limit must be between 1 and 100")
        with self._pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            rows = connection.execute(
                """
                SELECT v.version_id, v.title, v.source_uri, v.content_hash,
                       v.metadata, v.created_at
                FROM rusa_knowledge_document_versions v
                JOIN rusa_knowledge_documents d
                  ON d.tenant_id=v.tenant_id AND d.document_id=v.document_id
                WHERE v.tenant_id=%s AND v.collection=%s AND v.document_id=%s::uuid
                  AND (
                      COALESCE(d.metadata->>'knowledge_scope', 'shared') = 'shared'
                      OR (
                          %s::text IS NOT NULL
                          AND COALESCE(d.metadata->'agent_ids', '[]'::jsonb) ? %s
                          AND (
                              d.metadata->>'project_id' IS NULL
                              OR (
                                  %s::text IS NOT NULL
                                  AND d.metadata->>'project_id' = %s
                              )
                          )
                      )
                  )
                  AND (
                      COALESCE(v.metadata->>'knowledge_scope', 'shared') = 'shared'
                      OR (
                          %s::text IS NOT NULL
                          AND COALESCE(v.metadata->'agent_ids', '[]'::jsonb) ? %s
                          AND (
                              v.metadata->>'project_id' IS NULL
                              OR (
                                  %s::text IS NOT NULL
                                  AND v.metadata->>'project_id' = %s
                              )
                          )
                      )
                  )
                ORDER BY v.created_at DESC
                LIMIT %s
                """,
                (
                    tenant_id, collection, document_id,
                    agent_id, agent_id, project_id, project_id,
                    agent_id, agent_id, project_id, project_id, limit,
                ),
            ).fetchall()
        return [
            {
                "version_id": str(row[0]),
                "title": row[1],
                "source_uri": row[2],
                "content_hash": str(row[3]).strip(),
                "metadata": row[4],
                "created_at": row[5].isoformat() if row[5] else None,
            }
            for row in rows
        ]

    def get_document_version(
        self,
        *,
        tenant_id: str,
        collection: str,
        document_id: str,
        version_id: str,
        agent_id: str | None = None,
        project_id: str | None = None,
        max_chars: int = 20_000,
    ) -> dict[str, Any] | None:
        self._validate_scope(tenant_id, collection)
        self._validate_agent_id(agent_id)
        self._validate_project_id(project_id)
        try:
            UUID(document_id)
            UUID(version_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("document_id and version_id must be UUIDs") from exc
        if not 1 <= max_chars <= 50_000:
            raise ValueError("max_chars must be between 1 and 50000")
        with self._pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            version = connection.execute(
                """
                SELECT v.version_id, v.document_id, v.title, v.source_uri,
                       v.content_hash, v.metadata, v.created_at
                FROM rusa_knowledge_document_versions v
                JOIN rusa_knowledge_documents d
                  ON d.tenant_id=v.tenant_id AND d.document_id=v.document_id
                WHERE v.tenant_id=%s AND v.collection=%s
                  AND v.document_id=%s::uuid AND v.version_id=%s::uuid
                  AND (
                      COALESCE(d.metadata->>'knowledge_scope', 'shared') = 'shared'
                      OR (
                          %s::text IS NOT NULL
                          AND COALESCE(d.metadata->'agent_ids', '[]'::jsonb) ? %s
                          AND (
                              d.metadata->>'project_id' IS NULL
                              OR (
                                  %s::text IS NOT NULL
                                  AND d.metadata->>'project_id' = %s
                              )
                          )
                      )
                  )
                  AND (
                      COALESCE(v.metadata->>'knowledge_scope', 'shared') = 'shared'
                      OR (
                          %s::text IS NOT NULL
                          AND COALESCE(v.metadata->'agent_ids', '[]'::jsonb) ? %s
                          AND (
                              v.metadata->>'project_id' IS NULL
                              OR (
                                  %s::text IS NOT NULL
                                  AND v.metadata->>'project_id' = %s
                              )
                          )
                      )
                  )
                """,
                (
                    tenant_id, collection, document_id, version_id,
                    agent_id, agent_id, project_id, project_id,
                    agent_id, agent_id, project_id, project_id,
                ),
            ).fetchone()
            if version is None:
                return None
            chunks = connection.execute(
                """
                SELECT content
                FROM rusa_knowledge_version_chunks
                WHERE tenant_id=%s AND version_id=%s::uuid
                ORDER BY chunk_index
                """,
                (tenant_id, version_id),
            ).fetchall()
        content = "\\n\\n".join(row[0] for row in chunks)[:max_chars]
        return {
            "version_id": str(version[0]),
            "document_id": str(version[1]),
            "title": version[2],
            "source_uri": version[3],
            "content_hash": str(version[4]).strip(),
            "metadata": version[5],
            "created_at": version[6].isoformat() if version[6] else None,
            "content": content,
            "truncated": sum(len(row[0]) for row in chunks) > max_chars,
        }

    def list_collections(
        self,
        *,
        tenant_id: str,
        agent_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(tenant_id, str) or not TENANT_PATTERN.fullmatch(tenant_id):
            raise ValueError("tenant_id is invalid")
        self._validate_agent_id(agent_id)
        self._validate_project_id(project_id)
        with self._pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            rows = connection.execute(
                """
                SELECT collection, COUNT(*) AS document_count
                FROM rusa_knowledge_documents
                WHERE tenant_id=%s
                  AND (
                      COALESCE(metadata->>'knowledge_scope', 'shared') = 'shared'
                      OR (
                          %s::text IS NOT NULL
                          AND COALESCE(metadata->'agent_ids', '[]'::jsonb) ? %s
                          AND (
                              metadata->>'project_id' IS NULL
                              OR (
                                  %s::text IS NOT NULL
                                  AND metadata->>'project_id' = %s
                              )
                          )
                      )
                  )
                GROUP BY collection
                ORDER BY collection
                LIMIT 100
                """,
                (tenant_id, agent_id, agent_id, project_id, project_id),
            ).fetchall()
        return [{"collection": row[0], "document_count": int(row[1])} for row in rows]

    def delete_tenant(self, *, tenant_id: str) -> int:
        if not isinstance(tenant_id, str) or not TENANT_PATTERN.fullmatch(tenant_id):
            raise ValueError("tenant_id is invalid")
        with self._pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            cursor = connection.execute(
                "DELETE FROM rusa_knowledge_documents WHERE tenant_id=%s",
                (tenant_id,),
            )
            return cursor.rowcount

    def close(self) -> None:
        self._pool.close()
