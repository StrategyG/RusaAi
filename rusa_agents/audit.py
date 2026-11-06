"""Per-tenant serialized hash-chain audit log with integrity verification."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select, text

from .security import redact_sensitive_data
from .storage import AuditHeadRow, AuditRow, Database


ZERO_HASH = "0" * 64


class AuditLog:
    def __init__(self, database: Database):
        self.database = database

    async def record(
        self,
        tenant_id: str,
        task_id: str,
        actor: str,
        event: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        timestamp = datetime.now(timezone.utc)
        safe_details = redact_sensitive_data(details or {})
        async with self.database.session() as session:
            dialect = session.bind.dialect.name if session.bind is not None else ""
            if dialect == "postgresql":
                await session.execute(
                    text(
                        "INSERT INTO agent_audit_heads "
                        "(tenant_id, last_sequence, last_hash, updated_at) "
                        "VALUES (:tenant_id, 0, :zero_hash, :updated_at) "
                        "ON CONFLICT (tenant_id) DO NOTHING"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "zero_hash": ZERO_HASH,
                        "updated_at": timestamp,
                    },
                )
                head = await session.scalar(
                    select(AuditHeadRow)
                    .where(AuditHeadRow.tenant_id == tenant_id)
                    .with_for_update()
                )
            else:
                head = await session.scalar(
                    select(AuditHeadRow).where(AuditHeadRow.tenant_id == tenant_id)
                )
                if head is None:
                    head = AuditHeadRow(
                        tenant_id=tenant_id,
                        last_sequence=0,
                        last_hash=ZERO_HASH,
                        updated_at=timestamp,
                    )
                    session.add(head)
                    await session.flush()

            if head is None:
                raise RuntimeError("Unable to acquire tenant audit chain head")

            sequence = head.last_sequence + 1
            event_id = str(uuid4())
            body = {
                "id": event_id,
                "tenant_id": tenant_id,
                "task_id": task_id,
                "actor": actor,
                "event": event,
                "sequence": sequence,
                "details": safe_details,
                "previous_hash": head.last_hash,
                "created_at": timestamp.isoformat(),
            }
            digest = hashlib.sha256(
                json.dumps(body, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            session.add(
                AuditRow(
                    id=event_id,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    actor=actor,
                    event=event,
                    sequence=sequence,
                    details=safe_details,
                    previous_hash=head.last_hash,
                    event_hash=digest,
                    created_at=timestamp,
                )
            )
            head.last_sequence = sequence
            head.last_hash = digest
            head.updated_at = timestamp
            await session.commit()

    async def verify_chain(self, tenant_id: str) -> bool:
        async with self.database.session() as session:
            head = await session.scalar(
                select(AuditHeadRow)
                .where(AuditHeadRow.tenant_id == tenant_id)
                .with_for_update()
            )
            rows = (
                await session.scalars(
                    select(AuditRow)
                    .where(AuditRow.tenant_id == tenant_id)
                    .order_by(AuditRow.sequence.asc())
                )
            ).all()

        previous_hash = ZERO_HASH
        expected_sequence = 1
        for row in rows:
            timestamp = row.created_at
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            body = {
                "id": row.id,
                "tenant_id": row.tenant_id,
                "task_id": row.task_id,
                "actor": row.actor,
                "event": row.event,
                "sequence": row.sequence,
                "details": row.details,
                "previous_hash": row.previous_hash,
                "created_at": timestamp.isoformat(),
            }
            digest = hashlib.sha256(
                json.dumps(body, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            if (
                row.sequence != expected_sequence
                or row.previous_hash != previous_hash
                or row.event_hash != digest
            ):
                return False
            previous_hash = row.event_hash
            expected_sequence += 1

        if not rows:
            return head is None or (head.last_sequence == 0 and head.last_hash == ZERO_HASH)
        return bool(
            head
            and head.last_sequence == len(rows)
            and head.last_hash == previous_hash
        )
