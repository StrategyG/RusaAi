"""Durable tenant-scoped agent message bus."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from .contracts import WorkerMessage
from .storage import AgentMessageRow, Database


class AgentMessageBus:
    def __init__(self, database: Database):
        self.database = database

    async def publish(self, tenant_id: str, message: WorkerMessage) -> None:
        async with self.database.session() as session:
            session.add(
                AgentMessageRow(
                    id=message.message_id,
                    tenant_id=tenant_id,
                    task_id=message.task_id,
                    correlation_id=message.correlation_id,
                    from_agent=message.from_agent.value,
                    to_agent=message.to_agent.value,
                    message_type=message.message_type,
                    payload=message.payload,
                    created_at=message.created_at,
                )
            )
            await session.commit()

    async def history(self, tenant_id: str, task_id: str, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        async with self.database.session() as session:
            rows = (
                await session.execute(
                    select(AgentMessageRow)
                    .where(
                        AgentMessageRow.tenant_id == tenant_id,
                        AgentMessageRow.task_id == task_id,
                    )
                    .order_by(AgentMessageRow.created_at.asc())
                    .limit(limit)
                )
            ).scalars().all()
            return [
                {
                    "message_id": row.id,
                    "task_id": row.task_id,
                    "correlation_id": row.correlation_id,
                    "from_agent": row.from_agent,
                    "to_agent": row.to_agent,
                    "message_type": row.message_type,
                    "payload": row.payload,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]
