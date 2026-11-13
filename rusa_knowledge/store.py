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
    chunk_snapshot_sha256: str,
) -> str:
    payload = json.dumps(
        {
            "source_uri": source_uri,
            "title": title,
            "content_hash": content_hash,
            "chunk_snapshot_sha256": chunk_snapshot_sha256,
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
        require_publication: bool = True,
    ) -> None:
        if not dsn:
            raise ValueError("knowledge database DSN is required")
        if dimensions not in {384, 1536}:
            raise ValueError("embedding dimensions must be 384 or 1536")
        if not 1 <= min_pool_size <= max_pool_size <= 50:
            raise ValueError("database pool limits are invalid")
        if db_role is not None and not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", db_role):
            raise ValueError("knowledge database role name is invalid")
        if not isinstance(require_publication, bool):
            raise ValueError("require_publication must be a boolean")
        self.db_role = db_role
        self.require_publication = require_publication
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise RuntimeError("install psycopg with the pool extra to use the knowledge store") from exc
        self.dimensions = dimensions
        # Both identifiers are fixed constants selected from the supported dimension allowlist.
        self.embedding_column = "embedding_384" if dimensions == 384 else "embedding"
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
                        SELECT format_type(a.atttypid, a.atttypmod) = %s
                        FROM pg_attribute a
                        WHERE a.attrelid = to_regclass('public.rusa_knowledge_chunks')
                          AND a.attname = %s AND NOT a.attisdropped
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
                    NOT has_table_privilege(
                        current_user, 'public.rusa_knowledge_documents', 'DELETE'
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
                        FROM pg_class
                        WHERE oid = to_regclass('public.rusa_knowledge_document_versions')
                    ), false),
                    COALESCE((
                        SELECT relrowsecurity AND relforcerowsecurity
                        FROM pg_class
                        WHERE oid = to_regclass('public.rusa_knowledge_version_chunks')
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
                    to_regclass('public.rusa_knowledge_versions_document_idx') IS NOT NULL,
                    has_table_privilege(
                        current_user, 'public.rusa_knowledge_document_versions', 'SELECT'
                    ),
                    NOT has_table_privilege(
                        current_user, 'public.rusa_knowledge_document_versions', 'UPDATE'
                    ),
                    NOT has_table_privilege(
                        current_user, 'public.rusa_knowledge_document_versions', 'DELETE'
                    ),
                    has_table_privilege(
                        current_user, 'public.rusa_knowledge_version_chunks', 'SELECT'
                    ),
                    NOT has_table_privilege(
                        current_user, 'public.rusa_knowledge_version_chunks', 'UPDATE'
                    ),
                    NOT has_table_privilege(
                        current_user, 'public.rusa_knowledge_version_chunks', 'DELETE'
                    ),
                    to_regclass('public.rusa_knowledge_publication_latest_idx') IS NOT NULL,
                    has_table_privilege(
                        current_user, 'public.rusa_knowledge_publication_events', 'SELECT'
                    ),
                    (
                        SELECT count(*) = 5 AND bool_and(relrowsecurity AND relforcerowsecurity)
                        FROM pg_class
                        WHERE oid IN (
                            to_regclass('public.rusa_knowledge_rights_attestations'),
                            to_regclass('public.rusa_knowledge_review_findings'),
                            to_regclass('public.rusa_knowledge_human_approvals'),
                            to_regclass('public.rusa_knowledge_publication_events'),
                            to_regclass('public.rusa_knowledge_audit_events')
                        )
                    ),
                    (
                        SELECT count(*) = 5
                        FROM pg_policies
                        WHERE schemaname='public'
                          AND policyname IN (
                              'rusa_knowledge_rights_tenant_isolation',
                              'rusa_knowledge_reviews_tenant_isolation',
                              'rusa_knowledge_approvals_tenant_isolation',
                              'rusa_knowledge_publications_tenant_isolation',
                              'rusa_knowledge_audit_tenant_isolation'
                          )
                    ),
                    (
                        SELECT count(*) = 3
                        FROM pg_roles
                        WHERE rolname IN (
                            'rusa_knowledge_reviewer',
                            'rusa_knowledge_approver',
                            'rusa_knowledge_publisher'
                        )
                    ),
                    to_regprocedure(
                        'public.rusa_publish_knowledge_version(text,uuid,uuid,text,text)'
                    ) IS NOT NULL,
                    to_regprocedure(
                        'public.rusa_withdraw_knowledge_version(text,uuid,uuid,text)'
                    ) IS NOT NULL,
                    to_regprocedure(
                        'public.rusa_knowledge_version_is_published(text,uuid,uuid)'
                    ) IS NOT NULL,
                    has_function_privilege(
                        current_user,
                        'public.rusa_knowledge_version_is_published(text,uuid,uuid)',
                        'EXECUTE'
                    ),
                    to_regprocedure(
                        'public.rusa_knowledge_version_attribution(text,uuid,uuid)'
                    ) IS NOT NULL,
                    has_function_privilege(
                        current_user,
                        'public.rusa_knowledge_version_attribution(text,uuid,uuid)',
                        'EXECUTE'
                    )
                """,
                (f"vector({self.dimensions})", self.embedding_column),
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

        current_metadata = json.loads(metadata_json)
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
        chunk_snapshot_sha256 = hashlib.sha256(
            json.dumps(
                [{"content": row[1], "embedding": row[2]} for row in rows],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        current_fingerprint = _document_fingerprint(
            source_uri, title, content_hash, current_metadata, chunk_snapshot_sha256
        )

        from psycopg import sql

        version_embedding_column = sql.Identifier(self.embedding_column)
        version_insert_sql = sql.SQL(
            """
            INSERT INTO rusa_knowledge_version_chunks
                (tenant_id, version_id, chunk_index, content, {embedding_column}, metadata)
            VALUES (%s, %s::uuid, %s, %s, %s::vector, %s::jsonb)
            """
        ).format(embedding_column=version_embedding_column)
        current_insert_sql = sql.SQL(
            """
            INSERT INTO rusa_knowledge_chunks
                (tenant_id, document_id, collection, chunk_index, content, {embedding_column}, metadata)
            VALUES (%s, %s::uuid, %s, %s, %s, %s::vector, %s::jsonb)
            """
        ).format(embedding_column=version_embedding_column)

        with self._pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            lock_key = json.dumps(
                [tenant_id, collection, source_uri],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (lock_key,),
            )
            existing = connection.execute(
                """
                SELECT document_id
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

            existing_version = connection.execute(
                """
                SELECT version_id, version_number
                FROM rusa_knowledge_document_versions
                WHERE tenant_id=%s AND document_id=%s::uuid AND version_fingerprint=%s
                """,
                (tenant_id, document_id, current_fingerprint),
            ).fetchone()
            if existing_version is None:
                version_number = connection.execute(
                    """
                    SELECT COALESCE(MAX(version_number), 0) + 1
                    FROM rusa_knowledge_document_versions
                    WHERE tenant_id=%s AND document_id=%s::uuid
                    """,
                    (tenant_id, document_id),
                ).fetchone()[0]
                version = connection.execute(
                    """
                    INSERT INTO rusa_knowledge_document_versions
                        (tenant_id, document_id, version_number, collection, source_uri,
                         title, content_hash, version_fingerprint, metadata)
                    VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    RETURNING version_id
                    """,
                    (
                        tenant_id, document_id, version_number, collection, source_uri,
                        title, content_hash, current_fingerprint, metadata_json,
                    ),
                ).fetchone()
                version_id = str(version[0])
                with connection.cursor() as cursor:
                    cursor.executemany(
                        version_insert_sql,
                        [
                            (
                                tenant_id, version_id, index, content, vector, chunk_metadata,
                            )
                            for index, content, vector, chunk_metadata in rows
                        ],
                    )
            else:
                latest_version_number = connection.execute(
                    """
                    SELECT COALESCE(MAX(version_number), 0)
                    FROM rusa_knowledge_document_versions
                    WHERE tenant_id=%s AND document_id=%s::uuid
                    """,
                    (tenant_id, document_id),
                ).fetchone()[0]
                if int(existing_version[1]) < int(latest_version_number):
                    # Re-ingesting a historical snapshot must not roll the
                    # document pointer or mutable index back to a superseded version.
                    return document_id
                version_id = str(existing_version[0])

            connection.execute(
                """
                UPDATE rusa_knowledge_documents
                SET title=%s, content_hash=%s, metadata=%s::jsonb,
                    current_version_id=%s::uuid, updated_at=now()
                WHERE tenant_id=%s AND document_id=%s::uuid
                """,
                (title, content_hash, metadata_json, version_id, tenant_id, document_id),
            )
            connection.execute(
                "DELETE FROM rusa_knowledge_chunks WHERE tenant_id=%s AND document_id=%s::uuid",
                (tenant_id, document_id),
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    current_insert_sql,
                    [
                        (
                            tenant_id, document_id, collection, index, content, vector,
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

        from psycopg import sql

        query_sql = sql.SQL(
            """
            WITH scoped_chunks AS NOT MATERIALIZED (
                SELECT c.chunk_id, c.document_id, c.tenant_id, c.collection,
                       c.chunk_index, c.content, c.{embedding_column}, c.search_vector
                FROM rusa_knowledge_chunks c
                JOIN rusa_knowledge_documents d
                  ON d.tenant_id=c.tenant_id AND d.document_id=c.document_id
                LEFT JOIN rusa_knowledge_document_versions v
                  ON v.tenant_id=d.tenant_id
                 AND v.document_id=d.document_id
                 AND v.version_id=d.current_version_id
                LEFT JOIN rusa_knowledge_version_chunks vc
                  ON vc.tenant_id=c.tenant_id
                 AND vc.version_id=d.current_version_id
                 AND vc.chunk_index=c.chunk_index
                WHERE c.tenant_id=%s AND c.collection=%s
                  AND (
                      (
                          d.current_version_id IS NOT NULL
                          AND v.version_id IS NOT NULL
                          AND vc.version_id IS NOT NULL
                          AND c.collection=v.collection
                          AND c.content=vc.content
                          AND c.metadata=vc.metadata
                          AND (
                              vc.{embedding_column} IS NULL
                              OR c.{embedding_column} IS NOT DISTINCT FROM vc.{embedding_column}
                          )
                      )
                      OR (d.current_version_id IS NULL AND NOT %s::boolean)
                  )
                  AND (
                      COALESCE(COALESCE(v.metadata, d.metadata)->>'knowledge_scope', 'shared') = 'shared'
                      OR (
                          COALESCE(COALESCE(v.metadata, d.metadata)->>'knowledge_scope', 'shared') = 'agent_specific'
                          AND %s::text IS NOT NULL
                          AND COALESCE(COALESCE(v.metadata, d.metadata)->'agent_ids', '[]'::jsonb) ? %s
                          AND (
                              COALESCE(v.metadata, d.metadata)->>'project_id' IS NULL
                              OR (
                                  %s::text IS NOT NULL
                                  AND COALESCE(v.metadata, d.metadata)->>'project_id' = %s
                              )
                          )
                      )
                  )
                  AND (
                      NOT %s::boolean
                      OR public.rusa_knowledge_version_is_published(
                          d.tenant_id, d.document_id, d.current_version_id
                      )
                  )
            ),
            vector_candidates AS (
                SELECT chunk_id,
                       ROW_NUMBER() OVER (ORDER BY {embedding_column} <=> %s::vector) AS vector_rank
                FROM scoped_chunks
                WHERE {embedding_column} IS NOT NULL
                ORDER BY {embedding_column} <=> %s::vector
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
                   COALESCE(v.title, d.title), COALESCE(v.source_uri, d.source_uri),
                    COALESCE(v.metadata, d.metadata), combined.rrf_score,
                   COALESCE(1 - (c.{embedding_column} <=> %s::vector), 0.0) AS vector_similarity,
                   attribution.attribution_required, attribution.attribution_text
            FROM combined
            JOIN rusa_knowledge_chunks c
              ON c.chunk_id = combined.chunk_id AND c.tenant_id=%s
            JOIN rusa_knowledge_documents d
              ON d.document_id=c.document_id AND d.tenant_id=c.tenant_id
            LEFT JOIN rusa_knowledge_document_versions v
              ON v.tenant_id=d.tenant_id
             AND v.document_id=d.document_id
             AND v.version_id=d.current_version_id
            LEFT JOIN LATERAL public.rusa_knowledge_version_attribution(
                d.tenant_id, d.document_id, d.current_version_id
            ) attribution ON TRUE
            ORDER BY combined.rrf_score DESC
            LIMIT %s
            """
        ).format(embedding_column=sql.Identifier(self.embedding_column))
        params = (
            tenant_id, collection, self.require_publication,
            agent_id, agent_id, project_id, project_id, self.require_publication,
            vector, vector, candidate_limit,
            query, query, query, candidate_limit,
            vector, tenant_id, limit,
        )
        with self._pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            rows = connection.execute(query_sql, params).fetchall()
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
                "attribution_required": bool(row[9]) if row[9] is not None else False,
                "attribution_text": row[10],
            }
            for row in rows
        ]

    def fetch_chunks_for_reembedding(
        self,
        *,
        tenant_id: str,
        collection: str | None = None,
        after_chunk_id: str | None = None,
        limit: int = 64,
    ) -> list[dict[str, str]]:
        """Read legacy 1536d chunks missing their 384d replacement, in bounded batches."""
        if self.dimensions != 384:
            raise ValueError("re-embedding requires a 384-dimensional knowledge store")
        if not isinstance(tenant_id, str) or not TENANT_PATTERN.fullmatch(tenant_id):
            raise ValueError("tenant_id is invalid")
        if collection is not None and (
            not isinstance(collection, str) or not COLLECTION_PATTERN.fullmatch(collection)
        ):
            raise ValueError("collection is invalid")
        if not 1 <= limit <= 64:
            raise ValueError("re-embedding batch size must be between 1 and 64")
        if after_chunk_id is not None:
            try:
                UUID(after_chunk_id)
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError("after_chunk_id must be a UUID") from exc
        with self._pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            rows = connection.execute(
                """
                SELECT chunk_id::text, content
                FROM rusa_knowledge_chunks
                WHERE tenant_id=%s
                  AND embedding_384 IS NULL
                  AND embedding IS NOT NULL
                  AND (%s::text IS NULL OR collection=%s)
                  AND (%s::uuid IS NULL OR chunk_id > %s::uuid)
                ORDER BY chunk_id
                LIMIT %s
                """,
                (tenant_id, collection, collection, after_chunk_id, after_chunk_id, limit),
            ).fetchall()
        return [{"chunk_id": str(row[0]), "content": row[1]} for row in rows]

    def update_reembedded_embeddings(
        self,
        *,
        tenant_id: str,
        vectors: list[tuple[str, list[float]]],
    ) -> int:
        """Set 384d vectors only where the legacy vector still exists and no replacement exists."""
        if self.dimensions != 384:
            raise ValueError("re-embedding requires a 384-dimensional knowledge store")
        if not isinstance(tenant_id, str) or not TENANT_PATTERN.fullmatch(tenant_id):
            raise ValueError("tenant_id is invalid")
        if not vectors or len(vectors) > 64:
            raise ValueError("re-embedding update batch must contain between 1 and 64 vectors")
        rows = []
        for chunk_id, vector in vectors:
            try:
                UUID(chunk_id)
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError("chunk_id must be a UUID") from exc
            rows.append((_vector_literal(vector, 384), tenant_id, chunk_id))
        updated = 0
        with self._pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            with connection.cursor() as cursor:
                for vector, scoped_tenant, chunk_id in rows:
                    cursor.execute(
                        """
                        UPDATE rusa_knowledge_chunks
                        SET embedding_384=%s::vector
                        WHERE tenant_id=%s AND chunk_id=%s::uuid
                          AND embedding_384 IS NULL AND embedding IS NOT NULL
                        """,
                        (vector, scoped_tenant, chunk_id),
                    )
                    updated += cursor.rowcount
        return updated

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
                SELECT d.document_id, COALESCE(v.title, d.title),
                       COALESCE(v.source_uri, d.source_uri),
                       COALESCE(v.metadata, d.metadata),
                       COALESCE(v.content_hash, d.content_hash),
                       d.created_at, d.updated_at, d.current_version_id,
                       attribution.attribution_required, attribution.attribution_text
                FROM rusa_knowledge_documents d
                LEFT JOIN rusa_knowledge_document_versions v
                  ON v.tenant_id=d.tenant_id
                 AND v.document_id=d.document_id
                 AND v.version_id=d.current_version_id
                LEFT JOIN LATERAL public.rusa_knowledge_version_attribution(
                    d.tenant_id, d.document_id, d.current_version_id
                ) attribution ON TRUE
                WHERE d.tenant_id=%s
                  AND COALESCE(v.collection, d.collection)=%s
                  AND d.document_id=%s::uuid
                  AND (d.current_version_id IS NULL OR v.version_id IS NOT NULL)
                  AND (
                      COALESCE(COALESCE(v.metadata, d.metadata)->>'knowledge_scope', 'shared') = 'shared'
                      OR (
                          COALESCE(COALESCE(v.metadata, d.metadata)->>'knowledge_scope', 'shared') = 'agent_specific'
                          AND %s::text IS NOT NULL
                          AND COALESCE(COALESCE(v.metadata, d.metadata)->'agent_ids', '[]'::jsonb) ? %s
                          AND (
                              COALESCE(v.metadata, d.metadata)->>'project_id' IS NULL
                              OR (
                                  %s::text IS NOT NULL
                                  AND COALESCE(v.metadata, d.metadata)->>'project_id' = %s
                              )
                          )
                      )
                  )
                  AND (
                      NOT %s::boolean
                      OR public.rusa_knowledge_version_is_published(
                          d.tenant_id, d.document_id, d.current_version_id
                      )
                  )
                """,
                (
                    tenant_id, collection, document_id,
                    agent_id, agent_id, project_id, project_id, self.require_publication,
                ),
            ).fetchone()
            if document is None:
                return None
            if document[7] is None and not self.require_publication:
                chunks = connection.execute(
                    """
                    SELECT content FROM rusa_knowledge_chunks
                    WHERE tenant_id=%s AND collection=%s AND document_id=%s::uuid
                    ORDER BY chunk_index
                    """,
                    (tenant_id, collection, document_id),
                ).fetchall()
            else:
                chunks = connection.execute(
                    """
                    SELECT content FROM rusa_knowledge_version_chunks
                    WHERE tenant_id=%s AND version_id=%s::uuid
                    ORDER BY chunk_index
                    """,
                    (tenant_id, document[7]),
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
            "attribution_required": bool(document[8]) if document[8] is not None else False,
            "attribution_text": document[9],
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
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("version limit must be between 1 and 100")
        with self._pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            rows = connection.execute(
                """
                SELECT v.version_id, v.version_number, v.title, v.source_uri,
                       v.content_hash, v.metadata, v.created_at,
                       (v.version_id = d.current_version_id) AS is_current
                FROM rusa_knowledge_document_versions v
                JOIN rusa_knowledge_documents d
                  ON d.tenant_id=v.tenant_id AND d.document_id=v.document_id
                WHERE v.tenant_id=%s AND v.collection=%s AND v.document_id=%s::uuid
                  AND (
                      COALESCE(d.metadata->>'knowledge_scope', 'shared') = 'shared'
                      OR (
                          COALESCE(d.metadata->>'knowledge_scope', 'shared') = 'agent_specific'
                          AND %s::text IS NOT NULL
                          AND COALESCE(d.metadata->'agent_ids', '[]'::jsonb) ? %s
                          AND (
                              d.metadata->>'project_id' IS NULL
                              OR (%s::text IS NOT NULL AND d.metadata->>'project_id' = %s)
                          )
                      )
                  )
                  AND (
                      COALESCE(v.metadata->>'knowledge_scope', 'shared') = 'shared'
                      OR (
                          COALESCE(v.metadata->>'knowledge_scope', 'shared') = 'agent_specific'
                          AND %s::text IS NOT NULL
                          AND COALESCE(v.metadata->'agent_ids', '[]'::jsonb) ? %s
                          AND (
                              v.metadata->>'project_id' IS NULL
                              OR (%s::text IS NOT NULL AND v.metadata->>'project_id' = %s)
                          )
                      )
                  )
                  AND (
                      NOT %s::boolean
                      OR public.rusa_knowledge_version_is_published(
                          v.tenant_id, v.document_id, v.version_id
                      )
                  )
                ORDER BY v.version_number DESC
                LIMIT %s
                """,
                (
                    tenant_id, collection, document_id,
                    agent_id, agent_id, project_id, project_id,
                    agent_id, agent_id, project_id, project_id,
                    self.require_publication, limit,
                ),
            ).fetchall()
        return [
            {
                "version_id": str(row[0]),
                "version_number": int(row[1]),
                "title": row[2],
                "source_uri": row[3],
                "content_hash": str(row[4]).strip(),
                "metadata": row[5],
                "created_at": row[6].isoformat() if row[6] else None,
                "is_current": bool(row[7]),
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
        if type(max_chars) is not int or not 1 <= max_chars <= 50_000:
            raise ValueError("max_chars must be between 1 and 50000")
        with self._pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            version = connection.execute(
                """
                SELECT v.version_id, v.version_number, v.document_id, v.title,
                       v.source_uri, v.content_hash, v.metadata, v.created_at,
                       (v.version_id = d.current_version_id) AS is_current,
                       attribution.attribution_required, attribution.attribution_text
                FROM rusa_knowledge_document_versions v
                JOIN rusa_knowledge_documents d
                  ON d.tenant_id=v.tenant_id AND d.document_id=v.document_id
                LEFT JOIN LATERAL public.rusa_knowledge_version_attribution(
                    v.tenant_id, v.document_id, v.version_id
                ) attribution ON TRUE
                WHERE v.tenant_id=%s AND v.collection=%s
                  AND v.document_id=%s::uuid AND v.version_id=%s::uuid
                  AND (
                      COALESCE(d.metadata->>'knowledge_scope', 'shared') = 'shared'
                      OR (
                          COALESCE(d.metadata->>'knowledge_scope', 'shared') = 'agent_specific'
                          AND %s::text IS NOT NULL
                          AND COALESCE(d.metadata->'agent_ids', '[]'::jsonb) ? %s
                          AND (
                              d.metadata->>'project_id' IS NULL
                              OR (%s::text IS NOT NULL AND d.metadata->>'project_id' = %s)
                          )
                      )
                  )
                  AND (
                      COALESCE(v.metadata->>'knowledge_scope', 'shared') = 'shared'
                      OR (
                          COALESCE(v.metadata->>'knowledge_scope', 'shared') = 'agent_specific'
                          AND %s::text IS NOT NULL
                          AND COALESCE(v.metadata->'agent_ids', '[]'::jsonb) ? %s
                          AND (
                              v.metadata->>'project_id' IS NULL
                              OR (%s::text IS NOT NULL AND v.metadata->>'project_id' = %s)
                          )
                      )
                  )
                  AND (
                      NOT %s::boolean
                      OR public.rusa_knowledge_version_is_published(
                          v.tenant_id, v.document_id, v.version_id
                      )
                  )
                """,
                (
                    tenant_id, collection, document_id, version_id,
                    agent_id, agent_id, project_id, project_id,
                    agent_id, agent_id, project_id, project_id,
                    self.require_publication,
                ),
            ).fetchone()
            if version is None:
                return None
            chunks = connection.execute(
                """
                SELECT content FROM rusa_knowledge_version_chunks
                WHERE tenant_id=%s AND version_id=%s::uuid
                ORDER BY chunk_index
                """,
                (tenant_id, version_id),
            ).fetchall()
        content = "\n\n".join(row[0] for row in chunks)[:max_chars]
        return {
            "version_id": str(version[0]),
            "version_number": int(version[1]),
            "document_id": str(version[2]),
            "title": version[3],
            "source_uri": version[4],
            "content_hash": str(version[5]).strip(),
            "metadata": version[6],
            "created_at": version[7].isoformat() if version[7] else None,
            "is_current": bool(version[8]),
            "content": content,
            "truncated": sum(len(row[0]) for row in chunks) > max_chars,
            "attribution_required": bool(version[9]) if version[9] is not None else False,
            "attribution_text": version[10],
        }

    def get_review_candidate_version(
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
        if self.db_role != "rusa_knowledge_reviewer":
            raise PermissionError("unpublished candidate reads require the reviewer database role")
        self._validate_scope(tenant_id, collection)
        self._validate_agent_id(agent_id)
        self._validate_project_id(project_id)
        self._validate_document_version_ids(document_id, version_id)
        if type(max_chars) is not int or not 1 <= max_chars <= 50_000:
            raise ValueError("max_chars must be between 1 and 50000")
        with self._pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            version = connection.execute(
                """
                SELECT v.version_id, v.version_number, v.document_id, v.title,
                       v.source_uri, v.content_hash, v.metadata, v.created_at,
                       (v.version_id = d.current_version_id) AS is_current
                FROM rusa_knowledge_document_versions v
                JOIN rusa_knowledge_documents d
                  ON d.tenant_id=v.tenant_id AND d.document_id=v.document_id
                WHERE v.tenant_id=%s AND v.collection=%s
                  AND v.document_id=%s::uuid AND v.version_id=%s::uuid
                  AND (
                      COALESCE(d.metadata->>'knowledge_scope', 'shared') = 'shared'
                      OR (
                          COALESCE(d.metadata->>'knowledge_scope', 'shared') = 'agent_specific'
                          AND %s::text IS NOT NULL
                          AND COALESCE(d.metadata->'agent_ids', '[]'::jsonb) ? %s
                          AND (
                              d.metadata->>'project_id' IS NULL
                              OR (%s::text IS NOT NULL AND d.metadata->>'project_id' = %s)
                          )
                      )
                  )
                  AND (
                      COALESCE(v.metadata->>'knowledge_scope', 'shared') = 'shared'
                      OR (
                          COALESCE(v.metadata->>'knowledge_scope', 'shared') = 'agent_specific'
                          AND %s::text IS NOT NULL
                          AND COALESCE(v.metadata->'agent_ids', '[]'::jsonb) ? %s
                          AND (
                              v.metadata->>'project_id' IS NULL
                              OR (%s::text IS NOT NULL AND v.metadata->>'project_id' = %s)
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
                SELECT content FROM rusa_knowledge_version_chunks
                WHERE tenant_id=%s AND version_id=%s::uuid
                ORDER BY chunk_index
                """,
                (tenant_id, version_id),
            ).fetchall()
        content = "\n\n".join(row[0] for row in chunks)[:max_chars]
        return {
            "version_id": str(version[0]),
            "version_number": int(version[1]),
            "document_id": str(version[2]),
            "title": version[3],
            "source_uri": version[4],
            "content_hash": str(version[5]).strip(),
            "metadata": version[6],
            "created_at": version[7].isoformat() if version[7] else None,
            "is_current": bool(version[8]),
            "content": content,
            "truncated": sum(len(row[0]) for row in chunks) > max_chars,
            "publication_status": "candidate_only",
        }

    @staticmethod
    def _validate_document_version_ids(document_id: str, version_id: str) -> None:
        try:
            UUID(document_id)
            UUID(version_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("document_id and version_id must be UUIDs") from exc

    def record_rights_attestation(
        self,
        *,
        tenant_id: str,
        document_id: str,
        version_id: str,
        rights_status: str,
        source_owner: str,
        license_id: str,
        internal_storage_allowed: bool,
        extraction_allowed: bool,
        derivative_allowed: bool,
        embedding_allowed: bool,
        retrieval_allowed: bool,
        retention_policy: str,
        evidence_uri: str,
        attribution_required: bool = False,
        attribution_text: str | None = None,
    ) -> int:
        if not isinstance(tenant_id, str) or not TENANT_PATTERN.fullmatch(tenant_id):
            raise ValueError("tenant_id is invalid")
        self._validate_document_version_ids(document_id, version_id)
        if rights_status not in {"unknown", "cleared", "restricted", "denied"}:
            raise ValueError("rights_status is invalid")
        flags = (
            internal_storage_allowed,
            extraction_allowed,
            derivative_allowed,
            embedding_allowed,
            retrieval_allowed,
            attribution_required,
        )
        if any(type(flag) is not bool for flag in flags):
            raise ValueError("rights permission flags must be booleans")
        bounded = (
            (source_owner, 512, "source_owner"),
            (license_id, 256, "license_id"),
            (retention_policy, 2048, "retention_policy"),
            (evidence_uri, 2048, "evidence_uri"),
        )
        for value, maximum, label in bounded:
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError(f"{label} must contain 1 to {maximum} characters")
        if attribution_text is not None and (
            not isinstance(attribution_text, str) or len(attribution_text) > 2048
        ):
            raise ValueError("attribution_text exceeds 2048 characters")
        if attribution_required and not (attribution_text or "").strip():
            raise ValueError("attribution_text is required when attribution is required")
        if rights_status == "cleared" and not all(
            (internal_storage_allowed, extraction_allowed, derivative_allowed,
             embedding_allowed, retrieval_allowed)
        ):
            raise ValueError("cleared rights require all storage, extraction, derivative, embedding and retrieval permissions")
        with self._pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                """
                INSERT INTO rusa_knowledge_rights_attestations (
                    tenant_id, document_id, version_id, rights_status, source_owner,
                    license_id, internal_storage_allowed, extraction_allowed,
                    derivative_allowed, embedding_allowed, retrieval_allowed,
                    attribution_required, attribution_text, retention_policy, evidence_uri
                )
                VALUES (
                    %s, %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING rights_id
                """,
                (
                    tenant_id, document_id, version_id, rights_status, source_owner,
                    license_id, internal_storage_allowed, extraction_allowed,
                    derivative_allowed, embedding_allowed, retrieval_allowed,
                    attribution_required, attribution_text, retention_policy, evidence_uri,
                ),
            ).fetchone()
        return int(row[0])

    def record_review_finding(
        self,
        *,
        tenant_id: str,
        document_id: str,
        version_id: str,
        review_type: str,
        decision: str,
        finding_text: str,
        evidence_uri: str | None = None,
    ) -> int:
        if not isinstance(tenant_id, str) or not TENANT_PATTERN.fullmatch(tenant_id):
            raise ValueError("tenant_id is invalid")
        self._validate_document_version_ids(document_id, version_id)
        if review_type not in {"technical", "source_fidelity", "security", "qa"}:
            raise ValueError("review_type is invalid")
        if decision not in {"passed", "needs_changes", "failed"}:
            raise ValueError("review decision is invalid")
        if not isinstance(finding_text, str) or not finding_text.strip() or len(finding_text) > 10_000:
            raise ValueError("finding_text must contain 1 to 10000 characters")
        if evidence_uri is not None and (
            not isinstance(evidence_uri, str) or len(evidence_uri) > 2048
        ):
            raise ValueError("evidence_uri exceeds 2048 characters")
        with self._pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                """
                INSERT INTO rusa_knowledge_review_findings (
                    tenant_id, document_id, version_id, review_type, decision,
                    finding_text, evidence_uri
                )
                VALUES (%s, %s::uuid, %s::uuid, %s, %s, %s, %s)
                RETURNING review_id
                """,
                (
                    tenant_id, document_id, version_id, review_type,
                    decision, finding_text, evidence_uri,
                ),
            ).fetchone()
        return int(row[0])

    def record_human_approval(
        self,
        *,
        tenant_id: str,
        document_id: str,
        version_id: str,
        decision: str,
        approval_reference: str,
    ) -> int:
        if not isinstance(tenant_id, str) or not TENANT_PATTERN.fullmatch(tenant_id):
            raise ValueError("tenant_id is invalid")
        self._validate_document_version_ids(document_id, version_id)
        if decision not in {"approved", "rejected", "revoked"}:
            raise ValueError("approval decision is invalid")
        if (
            not isinstance(approval_reference, str)
            or not approval_reference.strip()
            or len(approval_reference) > 512
        ):
            raise ValueError("approval_reference must contain 1 to 512 characters")
        with self._pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                """
                INSERT INTO rusa_knowledge_human_approvals (
                    tenant_id, document_id, version_id, decision, approval_reference
                )
                VALUES (%s, %s::uuid, %s::uuid, %s, %s)
                RETURNING approval_id
                """,
                (tenant_id, document_id, version_id, decision, approval_reference),
            ).fetchone()
        return int(row[0])

    def publish_version(
        self,
        *,
        tenant_id: str,
        document_id: str,
        version_id: str,
        manifest_uri: str,
        manifest_sha256: str,
    ) -> int:
        if not isinstance(tenant_id, str) or not TENANT_PATTERN.fullmatch(tenant_id):
            raise ValueError("tenant_id is invalid")
        self._validate_document_version_ids(document_id, version_id)
        if (
            not isinstance(manifest_uri, str)
            or not manifest_uri.strip()
            or len(manifest_uri) > 2048
            or not isinstance(manifest_sha256, str)
            or len(manifest_sha256) != 64
            or re.search(r"[^0-9a-f]", manifest_sha256)
        ):
            raise ValueError("publication manifest URI or SHA-256 is invalid")
        with self._pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                """
                SELECT public.rusa_publish_knowledge_version(
                    %s, %s::uuid, %s::uuid, %s, %s
                )
                """,
                (tenant_id, document_id, version_id, manifest_uri, manifest_sha256),
            ).fetchone()
        return int(row[0])

    def withdraw_version(
        self,
        *,
        tenant_id: str,
        document_id: str,
        version_id: str,
        reason: str,
    ) -> int:
        if not isinstance(tenant_id, str) or not TENANT_PATTERN.fullmatch(tenant_id):
            raise ValueError("tenant_id is invalid")
        self._validate_document_version_ids(document_id, version_id)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2048:
            raise ValueError("withdrawal reason must contain 1 to 2048 characters")
        with self._pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                """
                SELECT public.rusa_withdraw_knowledge_version(
                    %s, %s::uuid, %s::uuid, %s
                )
                """,
                (tenant_id, document_id, version_id, reason),
            ).fetchone()
        return int(row[0])

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
                SELECT COALESCE(v.collection, d.collection) AS collection,
                       COUNT(*) AS document_count
                FROM rusa_knowledge_documents d
                LEFT JOIN rusa_knowledge_document_versions v
                  ON v.tenant_id=d.tenant_id
                 AND v.document_id=d.document_id
                 AND v.version_id=d.current_version_id
                WHERE d.tenant_id=%s
                  AND (d.current_version_id IS NULL OR v.version_id IS NOT NULL)
                  AND (
                      COALESCE(COALESCE(v.metadata, d.metadata)->>'knowledge_scope', 'shared') = 'shared'
                      OR (
                          COALESCE(COALESCE(v.metadata, d.metadata)->>'knowledge_scope', 'shared') = 'agent_specific'
                          AND %s::text IS NOT NULL
                          AND COALESCE(COALESCE(v.metadata, d.metadata)->'agent_ids', '[]'::jsonb) ? %s
                          AND (
                              COALESCE(v.metadata, d.metadata)->>'project_id' IS NULL
                              OR (
                                  %s::text IS NOT NULL
                                  AND COALESCE(v.metadata, d.metadata)->>'project_id' = %s
                              )
                          )
                      )
                  )
                  AND (
                      NOT %s::boolean
                      OR public.rusa_knowledge_version_is_published(
                          d.tenant_id, d.document_id, d.current_version_id
                      )
                  )
                GROUP BY COALESCE(v.collection, d.collection)
                ORDER BY COALESCE(v.collection, d.collection)
                LIMIT 100
                """,
                (tenant_id, agent_id, agent_id, project_id, project_id, self.require_publication),
            ).fetchall()
        return [{"collection": row[0], "document_count": int(row[1])} for row in rows]

    def delete_tenant(self, *, tenant_id: str) -> int:
        if not isinstance(tenant_id, str) or not TENANT_PATTERN.fullmatch(tenant_id):
            raise ValueError("tenant_id is invalid")
        raise PermissionError(
            "tenant erasure requires the privileged retention procedure; "
            "the knowledge worker role cannot delete documents or immutable versions"
        )

    def close(self) -> None:
        self._pool.close()
