"""Application service joining parsing, chunking, embeddings, and retrieval."""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Literal

from .chunking import chunk_text, normalize_text
from .embeddings import OpenAICompatibleEmbeddingProvider
from .safety import reject_credential_material, validate_document_metadata
from .store import PostgresKnowledgeStore

AGENT_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
PROJECT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


class KnowledgeService:
    def __init__(
        self,
        store: PostgresKnowledgeStore,
        embeddings: OpenAICompatibleEmbeddingProvider,
    ) -> None:
        self.store = store
        self.embeddings = embeddings

    @classmethod
    def from_env(
        cls, *, check_ready: bool = True, database_url: str | None = None
    ) -> KnowledgeService:
        dsn = database_url or os.getenv("RUSA_KNOWLEDGE_DATABASE_URL") or os.getenv("RUSA_AGENT_DATABASE_URL")
        if not dsn:
            raise RuntimeError("RUSA_KNOWLEDGE_DATABASE_URL is required")
        try:
            dimensions = int(os.getenv("RUSA_EMBEDDING_DIMENSIONS", "1536"))
        except ValueError as exc:
            raise ValueError("RUSA_EMBEDDING_DIMENSIONS must be an integer") from exc
        if dimensions not in {384, 1536}:
            raise ValueError("RUSA_EMBEDDING_DIMENSIONS must be 384 or 1536")

        raw_input_type = os.getenv("RUSA_EMBEDDING_INPUT_TYPE_ENABLED")
        if raw_input_type is None:
            input_type_enabled = dimensions == 384
        elif raw_input_type.strip().lower() in {"1", "true", "yes", "on"}:
            input_type_enabled = True
        elif raw_input_type.strip().lower() in {"0", "false", "no", "off"}:
            input_type_enabled = False
        else:
            raise ValueError("RUSA_EMBEDDING_INPUT_TYPE_ENABLED must be a boolean value")
        if dimensions == 384 and not input_type_enabled:
            raise ValueError("384-dimensional RusaModels E5 embeddings require query/passage input types")

        default_base_url = (
            os.getenv("RUSA_MODEL_BASE_URL", "")
            if dimensions == 384
            else "https://api.openai.com/v1"
        )
        base_url = os.getenv("RUSA_EMBEDDING_BASE_URL") or default_base_url
        if not base_url:
            raise RuntimeError("RUSA_EMBEDDING_BASE_URL or RUSA_MODEL_BASE_URL is required for 384-dimensional embeddings")
        provider = OpenAICompatibleEmbeddingProvider(
            base_url=base_url,
            api_key=(
                os.getenv("RUSA_EMBEDDING_API_KEY")
                or (os.getenv("RUSA_MODEL_API_KEY", "") if dimensions == 384 else "")
            ),
            model=os.getenv(
                "RUSA_EMBEDDING_MODEL",
                "rusa-embedding-small" if dimensions == 384 else "text-embedding-3-small",
            ),
            dimensions=dimensions,
            timeout_seconds=float(os.getenv("RUSA_EMBEDDING_TIMEOUT", "30")),
            input_type_enabled=input_type_enabled,
        )
        store = PostgresKnowledgeStore(
            dsn,
            dimensions=dimensions,
            db_role=os.getenv("RUSA_KNOWLEDGE_DB_ROLE") or None,
        )
        service = cls(store, provider)
        if check_ready:
            try:
                ready = store.check_ready()
            except Exception:
                store.close()
                raise
            if not ready:
                store.close()
                required_migrations = (
                    "migrations/007_rag_knowledge_pgvector.sql, "
                    "migrations/009_knowledge_embedding_384.sql, and "
                    "migrations/011_rag_agent_project_scope.sql"
                    if dimensions == 384
                    else "migrations/007_rag_knowledge_pgvector.sql and "
                    "migrations/011_rag_agent_project_scope.sql"
                )
                raise RuntimeError(
                    f"knowledge database is not ready; apply {required_migrations}"
                )
        return service

    def _embed(
        self,
        texts: list[str],
        input_type: Literal["query", "passage"],
    ) -> list[list[float]]:
        if getattr(self.embeddings, "input_type_enabled", False):
            return self.embeddings.embed(texts, input_type=input_type)
        return self.embeddings.embed(texts)

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        """Embed document chunks using the configured provider's passage mode."""
        return self._embed(texts, "passage")

    def ingest_text(
        self,
        *,
        tenant_id: str,
        collection: str,
        source_uri: str,
        title: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        chunk_size: int = 1200,
        overlap: int = 180,
    ) -> dict[str, Any]:
        if not isinstance(metadata, (dict, type(None))):
            raise ValueError("document metadata must be an object")
        if (
            not isinstance(source_uri, str)
            or not source_uri.strip()
            or len(source_uri) > 2048
            or len(source_uri.encode("utf-8")) > 2048
            or any(ord(character) < 32 for character in source_uri)
        ):
            raise ValueError("source_uri must be a bounded UTF-8 string without control characters")
        if not isinstance(title, str) or not title.strip() or len(title) > 256:
            raise ValueError("title must contain 1 to 256 characters")
        normalized = normalize_text(content)
        if not normalized:
            raise ValueError("document content is empty")
        if len(normalized) > 2_000_000:
            raise ValueError("document content exceeds the 2 million character limit")
        reject_credential_material(normalized)
        validate_document_metadata(source_uri, title, metadata or {})
        chunks = chunk_text(normalized, chunk_size=chunk_size, overlap=overlap)
        if not chunks:
            raise ValueError("document did not produce any searchable chunks")
        # The provider caps each request at 64 inputs. Batch larger documents
        # without changing chunk order so the stored vectors remain aligned.
        vectors: list[list[float]] = []
        for offset in range(0, len(chunks), 64):
            vectors.extend(self._embed(chunks[offset : offset + 64], "passage"))
        if len(vectors) != len(chunks):
            raise RuntimeError("embedding provider returned an incomplete document vector set")
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        document_id = self.store.ingest_document(
            tenant_id=tenant_id,
            collection=collection,
            source_uri=source_uri,
            title=title,
            content_hash=digest,
            metadata=metadata or {},
            chunks=chunks,
            embeddings=vectors,
        )
        return {
            "document_id": document_id,
            "collection": collection,
            "chunk_count": len(chunks),
            "content_hash": digest,
            "source_uri": source_uri,
        }

    def search(
        self,
        *,
        tenant_id: str,
        collection: str,
        query: str,
        limit: int = 5,
        agent_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("query must contain 1 to 2000 characters")
        if agent_id is not None and (
            not isinstance(agent_id, str) or not AGENT_PATTERN.fullmatch(agent_id)
        ):
            raise ValueError("agent_id is invalid")
        if project_id is not None and (
            not isinstance(project_id, str) or not PROJECT_PATTERN.fullmatch(project_id)
        ):
            raise ValueError("project_id is invalid")
        reject_credential_material(query)
        vector = self._embed([query], "query")[0]
        return self.store.search(
            tenant_id=tenant_id,
            collection=collection,
            query=query,
            query_embedding=vector,
            limit=limit,
            agent_id=agent_id,
            project_id=project_id,
        )

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
        if agent_id is not None and (
            not isinstance(agent_id, str) or not AGENT_PATTERN.fullmatch(agent_id)
        ):
            raise ValueError("agent_id is invalid")
        if project_id is not None and (
            not isinstance(project_id, str) or not PROJECT_PATTERN.fullmatch(project_id)
        ):
            raise ValueError("project_id is invalid")
        return self.store.get_document(
            tenant_id=tenant_id,
            collection=collection,
            document_id=document_id,
            max_chars=max_chars,
            agent_id=agent_id,
            project_id=project_id,
        )

    def list_collections(
        self,
        *,
        tenant_id: str,
        agent_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if agent_id is not None and (
            not isinstance(agent_id, str) or not AGENT_PATTERN.fullmatch(agent_id)
        ):
            raise ValueError("agent_id is invalid")
        if project_id is not None and (
            not isinstance(project_id, str) or not PROJECT_PATTERN.fullmatch(project_id)
        ):
            raise ValueError("project_id is invalid")
        return self.store.list_collections(
            tenant_id=tenant_id, agent_id=agent_id, project_id=project_id
        )

    def close(self) -> None:
        self.store.close()
