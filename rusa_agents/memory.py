"""Tenant-scoped durable memory operations."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import select

from .security import redact_sensitive_data
from .storage import Database, MemoryRow


class MemoryStore:
    """SQL memory store; semantic/vector retrieval is provided by the RAG adapter."""

    def __init__(self, database: Database):
        self.database = database

    async def put(
        self,
        tenant_id: str,
        namespace: str,
        memory_key: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not tenant_id or not namespace or not memory_key or not content.strip():
            raise ValueError("tenant, namespace, key, and non-empty content are required")
        if len(namespace) > 128 or len(memory_key) > 255 or len(content) > 100_000:
            raise ValueError("memory payload exceeds configured limits")
        content = redact_sensitive_data(content)
        metadata = redact_sensitive_data(metadata or {})
        async with self.database.session() as session:
            row = await session.scalar(
                select(MemoryRow).where(
                    MemoryRow.tenant_id == tenant_id,
                    MemoryRow.namespace == namespace,
                    MemoryRow.memory_key == memory_key,
                )
            )
            if row is None:
                row = MemoryRow(
                    id=str(uuid4()),
                    tenant_id=tenant_id,
                    namespace=namespace,
                    memory_key=memory_key,
                    content=content,
                    metadata_json=metadata,
                )
                session.add(row)
            else:
                row.content = content
                row.metadata_json = metadata
            await session.commit()
            return self._serialize(row)

    async def get(self, tenant_id: str, namespace: str, memory_key: str) -> dict[str, Any] | None:
        async with self.database.session() as session:
            row = await session.scalar(
                select(MemoryRow).where(
                    MemoryRow.tenant_id == tenant_id,
                    MemoryRow.namespace == namespace,
                    MemoryRow.memory_key == memory_key,
                )
            )
            return self._serialize(row) if row else None

    async def search(
        self,
        tenant_id: str,
        query: str,
        namespace: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            raise ValueError("search query cannot be empty")
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        statement = select(MemoryRow).where(
            MemoryRow.tenant_id == tenant_id,
            MemoryRow.content.ilike(f"%{query.strip()}%"),
        )
        if namespace:
            statement = statement.where(MemoryRow.namespace == namespace)
        statement = statement.order_by(MemoryRow.updated_at.desc()).limit(limit)
        async with self.database.session() as session:
            rows = (await session.execute(statement)).scalars().all()
            return [self._serialize(row) for row in rows]

    async def delete(self, tenant_id: str, namespace: str, memory_key: str) -> bool:
        async with self.database.session() as session:
            row = await session.scalar(
                select(MemoryRow).where(
                    MemoryRow.tenant_id == tenant_id,
                    MemoryRow.namespace == namespace,
                    MemoryRow.memory_key == memory_key,
                )
            )
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    @staticmethod
    def _serialize(row: MemoryRow) -> dict[str, Any]:
        return {
            "namespace": row.namespace,
            "key": row.memory_key,
            "content": redact_sensitive_data(row.content),
            "metadata": redact_sensitive_data(row.metadata_json),
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }
