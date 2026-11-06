"""Hash-chained audit events. Production deployments should export to immutable storage."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from .storage import AuditRow, Database


class AuditLog:
    def __init__(self, database: Database):
        self.database = database

    async def record(self, tenant_id: str, task_id: str, actor: str, event: str, details: dict[str, Any] | None = None) -> None:
        async with self.database.session() as session:
            result = await session.execute(
                select(AuditRow).where(AuditRow.tenant_id == tenant_id).order_by(AuditRow.created_at.desc()).limit(1)
            )
            previous = result.scalar_one_or_none()
            previous_hash = previous.event_hash if previous else "0" * 64
            timestamp = datetime.now(timezone.utc)
            body = {
                "id": str(uuid4()), "tenant_id": tenant_id, "task_id": task_id, "actor": actor,
                "event": event, "details": details or {}, "previous_hash": previous_hash,
                "created_at": timestamp.isoformat(),
            }
            digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
            session.add(AuditRow(
                id=body["id"], tenant_id=tenant_id, task_id=task_id, actor=actor,
                event=event, details=body["details"], previous_hash=previous_hash,
                event_hash=digest, created_at=timestamp,
            ))
            await session.commit()
