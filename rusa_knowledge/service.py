"""Application service joining parsing, chunking, embeddings, and retrieval."""

from __future__ import annotations

import hashlib
import math
import os
from typing import Any

from .chunking import chunk_text, normalize_text
from .embeddings import OpenAICompatibleEmbeddingProvider
from .ollama_embeddings import OllamaEmbeddingProvider
from .safety import reject_credential_material, validate_document_metadata
from .store import PostgresKnowledgeStore


class KnowledgeService:
    def __init__(
        self,
        store: PostgresKnowledgeStore,
        embeddings: Any,
        *,
        vector_weight: float = 0.7,
        lexical_weight: float = 0.3,
        hnsw_ef_search: int = 80,
    ) -> None:
        if (
            not math.isfinite(vector_weight)
            or not math.isfinite(lexical_weight)
            or vector_weight < 0
            or lexical_weight < 0
            or vector_weight + lexical_weight <= 0
        ):
            raise ValueError("RAG retrieval weights must be finite, non-negative, and not both zero")
        if isinstance(hnsw_ef_search, bool) or not 1 <= hnsw_ef_search <= 1000:
            raise ValueError("HNSW ef_search must be between 1 and 1000")
        weight_total = vector_weight + lexical_weight
        self.store = store
        self.embeddings = embeddings
        self.vector_weight = vector_weight / weight_total
        self.lexical_weight = lexical_weight / weight_total
        self.hnsw_ef_search = hnsw_ef_search

    @classmethod
    def from_env(
        cls, *, check_ready: bool = True, database_url: str | None = None
    ) -> KnowledgeService:
        dsn = (
            database_url
            or os.getenv("RUSA_KNOWLEDGE_DATABASE_URL")
            or os.getenv("RUSA_AGENT_DATABASE_URL")
        )
        if not dsn:
            raise RuntimeError("RUSA_KNOWLEDGE_DATABASE_URL is required")

        provider_name = os.getenv("RUSA_EMBEDDING_PROVIDER", "openai-compatible").strip().lower()
        model = os.getenv(
            "RUSA_EMBEDDING_MODEL",
            "bge-m3" if provider_name == "ollama" else "text-embedding-3-small",
        )
        timeout = float(os.getenv("RUSA_EMBEDDING_TIMEOUT", "30"))
        if provider_name == "ollama":
            allowed_hosts = {
                item.strip().lower()
                for item in os.getenv("RUSA_EMBEDDING_ALLOWED_HOSTS", "ollama").split(",")
                if item.strip()
            }
            provider = OllamaEmbeddingProvider(
                base_url=os.getenv("RUSA_EMBEDDING_BASE_URL", "http://127.0.0.1:11434"),
                model=model,
                native_dimensions=int(os.getenv("RUSA_EMBEDDING_NATIVE_DIMENSIONS", "1024")),
                output_dimensions=1536,
                timeout_seconds=timeout,
                allow_http_hosts=allowed_hosts,
                max_retries=int(os.getenv("RUSA_EMBEDDING_MAX_RETRIES", "2")),
            )
        elif provider_name == "openai-compatible":
            provider = OpenAICompatibleEmbeddingProvider(
                base_url=os.getenv("RUSA_EMBEDDING_BASE_URL", "https://api.openai.com/v1"),
                api_key=os.getenv("RUSA_EMBEDDING_API_KEY", ""),
                model=model,
                dimensions=1536,
                timeout_seconds=timeout,
            )
        else:
            raise RuntimeError("RUSA_EMBEDDING_PROVIDER must be ollama or openai-compatible")

        pool_min = int(os.getenv("RUSA_KNOWLEDGE_DB_POOL_MIN", "1"))
        pool_max = int(os.getenv("RUSA_KNOWLEDGE_DB_POOL_MAX", "10"))
        store = PostgresKnowledgeStore(
            dsn,
            dimensions=1536,
            min_pool_size=pool_min,
            max_pool_size=pool_max,
            db_role=os.getenv("RUSA_KNOWLEDGE_DB_ROLE") or None,
        )
        service = cls(
            store,
            provider,
            vector_weight=float(os.getenv("RUSA_RAG_VECTOR_WEIGHT", "0.7")),
            lexical_weight=float(os.getenv("RUSA_RAG_LEXICAL_WEIGHT", "0.3")),
            hnsw_ef_search=int(os.getenv("RUSA_RAG_HNSW_EF_SEARCH", "80")),
        )
        if check_ready:
            try:
                ready = service.check_ready()
            except Exception:
                service.close()
                raise
            if not ready:
                service.close()
                raise RuntimeError(
                    "knowledge database or local embedding model is not ready; "
                    "apply migrations/007_rag_knowledge_pgvector.sql and verify the embedding service"
                )
        return service

    def check_ready(self) -> bool:
        if not self.store.check_ready():
            return False
        provider_check = getattr(self.embeddings, "check_ready", None)
        return bool(provider_check()) if callable(provider_check) else True

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
            vectors.extend(self.embeddings.embed(chunks[offset : offset + 64]))
        if len(vectors) != len(chunks):
            raise RuntimeError("embedding provider returned an incomplete document vector set")
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        document_id = self.store.ingest_document(
            tenant_id=tenant_id,
            collection=collection,
            source_uri=source_uri,
            title=title,
            content_hash=digest,
            metadata={
                **(metadata or {}),
                "embedding_provider": getattr(self.embeddings, "provider_name", "openai-compatible"),
                "embedding_model": getattr(self.embeddings, "model", "configured-model"),
                "embedding_dimensions": 1536,
                "embedding_native_dimensions": getattr(self.embeddings, "native_dimensions", 1536),
            },
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
    ) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("query must contain 1 to 2000 characters")
        reject_credential_material(query)
        vector = self.embeddings.embed([query])[0]
        results = self.store.search(
            tenant_id=tenant_id,
            collection=collection,
            query=query,
            query_embedding=vector,
            limit=limit,
            vector_weight=self.vector_weight,
            lexical_weight=self.lexical_weight,
            hnsw_ef_search=self.hnsw_ef_search,
        )
        # Older records may predate ingestion-time credential checks. Re-scan
        # retrieved material before it enters an agent or MCP context.
        import json

        reject_credential_material(json.dumps(results, ensure_ascii=False, default=str))
        return results

    def get_document(
        self,
        *,
        tenant_id: str,
        collection: str,
        document_id: str,
        max_chars: int = 20_000,
    ) -> dict[str, Any] | None:
        document = self.store.get_document(
            tenant_id=tenant_id,
            collection=collection,
            document_id=document_id,
            max_chars=max_chars,
        )
        if document is not None:
            import json

            reject_credential_material(json.dumps(document, ensure_ascii=False, default=str))
        return document

    def list_collections(self, *, tenant_id: str) -> list[dict[str, Any]]:
        return self.store.list_collections(tenant_id=tenant_id)

    def close(self) -> None:
        close_embeddings = getattr(self.embeddings, "close", None)
        if callable(close_embeddings):
            close_embeddings()
        self.store.close()
