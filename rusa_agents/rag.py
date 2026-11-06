"""ACL-aware RAG service contracts and retrieval orchestration.

A concrete vector backend and embedding model must be configured by deployment.
This module deliberately does not pretend keyword memory search is vector RAG.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    source_id: str
    text: str
    score: float
    metadata: dict[str, Any]


class EmbeddingProvider(Protocol):
    async def embed_query(self, query: str) -> list[float]: ...


class VectorIndex(Protocol):
    async def search(
        self,
        tenant_id: str,
        embedding: list[float],
        top_k: int,
        allowed_source_ids: list[str] | None = None,
    ) -> list[RetrievedChunk]: ...


class RAGService:
    def __init__(self, embeddings: EmbeddingProvider, index: VectorIndex):
        self.embeddings = embeddings
        self.index = index

    async def retrieve(
        self,
        tenant_id: str,
        query: str,
        top_k: int = 5,
        allowed_source_ids: list[str] | None = None,
    ) -> list[RetrievedChunk]:
        if not tenant_id or not query.strip():
            raise ValueError("tenant_id and query are required")
        if not 1 <= top_k <= 20:
            raise ValueError("top_k must be between 1 and 20")
        vector = await self.embeddings.embed_query(query)
        if not vector or any(not isinstance(value, (int, float)) for value in vector):
            raise ValueError("embedding provider returned an invalid vector")
        results = await self.index.search(
            tenant_id=tenant_id,
            embedding=vector,
            top_k=top_k,
            allowed_source_ids=allowed_source_ids,
        )
        return [
            chunk for chunk in results
            if chunk.source_id
            and chunk.text.strip()
            and 0.0 <= chunk.score <= 1.0
            and (allowed_source_ids is None or chunk.source_id in allowed_source_ids)
        ]
