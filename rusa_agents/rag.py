"""Tenant-aware PostgreSQL pgvector RAG ingestion and retrieval."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

import httpx
from sqlalchemy import text

from .security import redact_sensitive_data
from .storage import Database


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    source_id: str
    text: str
    score: float
    metadata: dict[str, Any]


class EmbeddingProvider(Protocol):
    async def embed_query(self, query: str) -> list[float]: ...
    async def embed_documents(self, documents: list[str]) -> list[list[float]]: ...


class VectorIndex(Protocol):
    async def upsert(self, tenant_id: str, source_id: str, chunks: list[dict[str, Any]]) -> None: ...
    async def search(self, tenant_id: str, embedding: list[float], top_k: int, allowed_source_ids: list[str] | None = None) -> list[RetrievedChunk]: ...
    async def delete_source(self, tenant_id: str, source_id: str) -> int: ...


class OpenAICompatibleEmbeddingProvider:
    def __init__(self, base_url: str | None = None, model: str | None = None, api_key: str | None = None, dimensions: int = 384):
        self.base_url = (base_url or os.getenv("RUSA_EMBEDDING_BASE_URL", "")).rstrip("/")
        self.model = model or os.getenv("RUSA_EMBEDDING_MODEL", "")
        self.api_key = api_key or os.getenv("RUSA_EMBEDDING_API_KEY", "")
        self.dimensions = dimensions
        if not self.base_url or not self.model:
            raise ValueError("Embedding base URL and model are required")
        if dimensions != 384:
            raise ValueError("The current pgvector migration uses exactly 384 dimensions")

    async def _embed(self, inputs: str | list[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                response = await client.post(f"{self.base_url}/embeddings", headers=headers, json={"model": self.model, "input": inputs})
                response.raise_for_status()
                data = response.json()["data"]
                vectors = [[float(value) for value in item["embedding"]] for item in sorted(data, key=lambda entry: entry.get("index", 0))]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError("Embedding provider request failed") from exc
        if any(len(vector) != self.dimensions for vector in vectors):
            raise RuntimeError("Embedding provider returned a vector with the wrong dimension")
        return vectors

    async def embed_query(self, query: str) -> list[float]:
        vectors = await self._embed(query)
        if len(vectors) != 1:
            raise RuntimeError("Embedding provider returned an invalid query vector count")
        return vectors[0]

    async def embed_documents(self, documents: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for offset in range(0, len(documents), 32):
            vectors.extend(await self._embed(documents[offset:offset + 32]))
        if len(vectors) != len(documents):
            raise RuntimeError("Embedding provider returned an incomplete document batch")
        return vectors


class PostgresVectorIndex:
    DIMENSIONS = 384

    def __init__(self, database: Database):
        self.database = database

    @classmethod
    def _vector_literal(cls, embedding: list[float]) -> str:
        if len(embedding) != cls.DIMENSIONS:
            raise ValueError("Embedding dimension does not match pgvector schema")
        import math

        if any(not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in embedding):
            raise ValueError("Embedding contains a non-finite or non-numeric value")
        return "[" + ",".join(f"{float(value):.8f}" for value in embedding) + "]"

    async def upsert(self, tenant_id: str, source_id: str, chunks: list[dict[str, Any]]) -> None:
        if not tenant_id or not source_id:
            raise ValueError("tenant and source identifiers are required")
        statement = text("""
            INSERT INTO agent_documents
                (id, tenant_id, source_id, chunk_id, title, content, metadata_json, embedding, created_at)
            VALUES
                (:id, :tenant_id, :source_id, :chunk_id, :title, :content,
                 CAST(:metadata AS json), CAST(:embedding AS vector), now())
            ON CONFLICT (tenant_id, source_id, chunk_id)
            DO UPDATE SET title = EXCLUDED.title, content = EXCLUDED.content,
                          metadata_json = EXCLUDED.metadata_json, embedding = EXCLUDED.embedding
        """)
        async with self.database.engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM agent_documents WHERE tenant_id = :tenant_id AND source_id = :source_id"),
                {"tenant_id": tenant_id, "source_id": source_id},
            )
            for chunk in chunks:
                await connection.execute(statement, {
                    "id": str(uuid4()), "tenant_id": tenant_id, "source_id": source_id,
                    "chunk_id": chunk["chunk_id"], "title": chunk["title"],
                    "content": chunk["text"], "metadata": json.dumps(chunk["metadata"]),
                    "embedding": self._vector_literal(chunk["embedding"]),
                })

    async def search(self, tenant_id: str, embedding: list[float], top_k: int, allowed_source_ids: list[str] | None = None) -> list[RetrievedChunk]:
        parameters: dict[str, Any] = {
            "tenant_id": tenant_id, "embedding": self._vector_literal(embedding), "top_k": top_k,
        }
        acl_clause = ""
        if allowed_source_ids is not None:
            if not allowed_source_ids:
                return []
            acl_clause = "AND source_id = ANY(CAST(:source_ids AS text[]))"
            parameters["source_ids"] = allowed_source_ids
        statement = text(f"""
            SELECT chunk_id, source_id, content,
                   1 - ((embedding <=> CAST(:embedding AS vector)) / 2.0) AS score,
                   metadata_json
            FROM agent_documents
            WHERE tenant_id = :tenant_id {acl_clause}
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT :top_k
        """)
        async with self.database.engine.connect() as connection:
            rows = (await connection.execute(statement, parameters)).mappings().all()
        return [
            RetrievedChunk(
                chunk_id=row["chunk_id"], source_id=row["source_id"], text=row["content"],
                score=max(0.0, min(1.0, float(row["score"]))), metadata=row["metadata_json"] or {},
            )
            for row in rows
        ]

    async def delete_source(self, tenant_id: str, source_id: str) -> int:
        statement = text("DELETE FROM agent_documents WHERE tenant_id = :tenant_id AND source_id = :source_id")
        async with self.database.engine.begin() as connection:
            result = await connection.execute(statement, {"tenant_id": tenant_id, "source_id": source_id})
            return int(result.rowcount or 0)


class RAGService:
    def __init__(self, embeddings: EmbeddingProvider, index: VectorIndex):
        self.embeddings = embeddings
        self.index = index

    @staticmethod
    def _chunks(text_value: str, chunk_size: int = 1200, overlap: int = 160) -> list[str]:
        normalized = " ".join(text_value.split())
        if not normalized:
            return []
        if chunk_size <= overlap or chunk_size > 8000:
            raise ValueError("Invalid RAG chunking configuration")
        chunks = []
        start = 0
        while start < len(normalized):
            end = min(len(normalized), start + chunk_size)
            chunks.append(normalized[start:end])
            if end == len(normalized):
                break
            start = end - overlap
        return chunks

    async def ingest_text(self, tenant_id: str, source_id: str, title: str, content: str, metadata: dict[str, Any] | None = None) -> int:
        if not tenant_id or not source_id or not title.strip() or not content.strip():
            raise ValueError("tenant, source, title, and content are required")
        if len(content) > 2_000_000:
            raise ValueError("Document exceeds the 2 MB ingestion limit")
        content = redact_sensitive_data(content)
        title = redact_sensitive_data(title)
        metadata = redact_sensitive_data(metadata or {})
        chunks = self._chunks(content)
        embeddings = await self.embeddings.embed_documents(chunks)
        if len(embeddings) != len(chunks):
            raise ValueError("Embedding count does not match chunk count")
        records = []
        for index, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=True)):
            chunk_id = hashlib.sha256(f"{tenant_id}:{source_id}:{index}:{chunk}".encode("utf-8")).hexdigest()
            records.append({
                "chunk_id": chunk_id, "title": title[:500], "text": chunk,
                "metadata": {**metadata, "chunk_index": index}, "embedding": embedding,
            })
        await self.index.upsert(tenant_id, source_id, records)
        return len(records)

    async def retrieve(self, tenant_id: str, query: str, top_k: int = 5, allowed_source_ids: list[str] | None = None) -> list[RetrievedChunk]:
        if not tenant_id or not query.strip():
            raise ValueError("tenant_id and query are required")
        if not 1 <= top_k <= 20:
            raise ValueError("top_k must be between 1 and 20")
        vector = await self.embeddings.embed_query(query)
        if not vector or any(not isinstance(value, (int, float)) for value in vector):
            raise ValueError("Embedding provider returned an invalid vector")
        results = await self.index.search(tenant_id=tenant_id, embedding=vector, top_k=top_k, allowed_source_ids=allowed_source_ids)
        return [
            RetrievedChunk(
                chunk_id=chunk.chunk_id,
                source_id=chunk.source_id,
                text=redact_sensitive_data(chunk.text),
                score=chunk.score,
                metadata=redact_sensitive_data(chunk.metadata),
            )
            for chunk in results
            if chunk.source_id and chunk.text.strip() and 0.0 <= chunk.score <= 1.0
            and (allowed_source_ids is None or chunk.source_id in allowed_source_ids)
        ]

    async def delete_source(self, tenant_id: str, source_id: str) -> int:
        return await self.index.delete_source(tenant_id, source_id)
