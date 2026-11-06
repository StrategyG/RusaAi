"""Shared service wiring for API and worker processes."""
from __future__ import annotations

import os
from typing import Any

from .mcp_gateway import MCPGateway
from .providers import OpenAICompatibleProvider
from .rag import OpenAICompatibleEmbeddingProvider, PostgresVectorIndex, RAGService
from .storage import Database
from .tools import GitHubTools, ToolRegistry


async def build_tooling(database: Database) -> tuple[ToolRegistry, MCPGateway, RAGService | None]:
    tools = ToolRegistry()
    GitHubTools().register(tools)

    mcp_gateway = MCPGateway.from_env()
    await mcp_gateway.connect_and_register(tools)

    rag_service: RAGService | None = None
    if os.getenv("RUSA_EMBEDDING_BASE_URL") and os.getenv("RUSA_EMBEDDING_MODEL"):
        embeddings = OpenAICompatibleEmbeddingProvider()
        rag_service = RAGService(embeddings, PostgresVectorIndex(database))

        async def rag_search(
            tenant_id: str,
            query: str,
            top_k: int = 5,
            allowed_source_ids: list[str] | None = None,
        ) -> dict[str, Any]:
            chunks = await rag_service.retrieve(
                tenant_id=tenant_id,
                query=query,
                top_k=top_k,
                allowed_source_ids=allowed_source_ids,
            )
            return {
                "chunks": [
                    {
                        "chunk_id": chunk.chunk_id,
                        "source_id": chunk.source_id,
                        "text": chunk.text,
                        "score": chunk.score,
                        "metadata": chunk.metadata,
                        "citation": f"[{chunk.source_id}#{chunk.chunk_id[:12]}]",
                    }
                    for chunk in chunks
                ]
            }

        tools.register(
            "rag.search",
            rag_search,
            "Retrieve tenant-scoped relevant document chunks with source citations.",
            {
                "type": "object",
                "properties": {
                    "tenant_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "query": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
                    "allowed_source_ids": {
                        "type": ["array", "null"],
                        "items": {"type": "string", "minLength": 1, "maxLength": 255},
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        )

    return tools, mcp_gateway, rag_service


def build_planner() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider()
