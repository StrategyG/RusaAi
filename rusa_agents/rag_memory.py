"""Composite episodic and semantic memory reader for specialist workers."""

from __future__ import annotations

from typing import Any


class CompositeMemoryReader:
    """Expose episodic memory and RAG evidence through the existing worker interface."""

    def __init__(
        self,
        episodic_memory: Any,
        knowledge_service: Any,
        *,
        collection: str = "agent",
    ) -> None:
        self.episodic_memory = episodic_memory
        self.knowledge_service = knowledge_service
        self.collection = collection

    def search(self, tenant_id: str, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        if not 1 <= limit <= 20:
            raise ValueError("memory retrieval limit must be between 1 and 20")
        episodic = self.episodic_memory.search(tenant_id, query, limit=limit)
        semantic = self.knowledge_service.search(
            tenant_id=tenant_id,
            collection=self.collection,
            query=query,
            limit=limit,
        )
        results: list[dict[str, Any]] = [
            {
                "memory_type": "episodic",
                "content": item.get("content", ""),
                "source": item.get("source", ""),
                "metadata": item.get("metadata", {}),
            }
            for item in episodic
        ]
        results.extend(
            {
                "memory_type": "knowledge",
                "content": item.get("content", ""),
                "source": item.get("source_uri", ""),
                "title": item.get("title", ""),
                "document_id": item.get("document_id", ""),
                "score": item.get("score", 0.0),
                "collection": self.collection,
            }
            for item in semantic
        )
        return results[: limit * 2]

    def close(self) -> None:
        self.knowledge_service.close()
