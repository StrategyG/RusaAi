"""Application service joining parsing, chunking, embeddings, and retrieval."""

from __future__ import annotations

import hashlib
import os
from typing import Any

from .chunking import chunk_text, normalize_text
from .embeddings import OpenAICompatibleEmbeddingProvider
from .store import PostgresKnowledgeStore


class KnowledgeService:
    def __init__(
        self,
        store: PostgresKnowledgeStore,
        embeddings: OpenAICompatibleEmbeddingProvider,
    ) -> None:
        self.store = store
        self.embeddings = embeddings

    @classmethod
    def from_env(cls, *, check_ready: bool = True) -> KnowledgeService:
        dsn = os.getenv("RUSA_KNOWLEDGE_DATABASE_URL") or os.getenv("RUSA_AGENT_DATABASE_URL")
        if not dsn:
            raise RuntimeError("RUSA_KNOWLEDGE_DATABASE_URL is required")
        provider = OpenAICompatibleEmbeddingProvider(
            base_url=os.getenv("RUSA_EMBEDDING_BASE_URL", "https://api.openai.com/v1"),
            api_key=os.getenv("RUSA_EMBEDDING_API_KEY", ""),
            model=os.getenv("RUSA_EMBEDDING_MODEL", "text-embedding-3-small"),
            dimensions=1536,
            timeout_seconds=float(os.getenv("RUSA_EMBEDDING_TIMEOUT", "30")),
        )
        store = PostgresKnowledgeStore(dsn, dimensions=1536)
        service = cls(store, provider)
        if check_ready:
            try:
                ready = store.check_ready()
            except Exception:
                store.close()
                raise
            if not ready:
                store.close()
                raise RuntimeError(
                    "knowledge database is not ready; apply migrations/005_rag_knowledge_pgvector.sql"
                )
        return service

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
        normalized = normalize_text(content)
        if not normalized:
            raise ValueError("document content is empty")
        if len(normalized) > 2_000_000:
            raise ValueError("document content exceeds the 2 million character limit")
        chunks = chunk_text(normalized, chunk_size=chunk_size, overlap=overlap)
        if not chunks:
            raise ValueError("document did not produce any searchable chunks")
        vectors = self.embeddings.embed(chunks)
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
    ) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("query must contain 1 to 2000 characters")
        vector = self.embeddings.embed([query])[0]
        return self.store.search(
            tenant_id=tenant_id,
            collection=collection,
            query=query,
            query_embedding=vector,
            limit=limit,
        )

    def get_document(
        self,
        *,
        tenant_id: str,
        collection: str,
        document_id: str,
        max_chars: int = 20_000,
    ) -> dict[str, Any] | None:
        return self.store.get_document(
            tenant_id=tenant_id,
            collection=collection,
            document_id=document_id,
            max_chars=max_chars,
        )

    def list_collections(self, *, tenant_id: str) -> list[dict[str, Any]]:
        return self.store.list_collections(tenant_id=tenant_id)

    def close(self) -> None:
        self.store.close()
