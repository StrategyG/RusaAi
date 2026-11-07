"""PostgreSQL persistence primitives for the DevOps communication plane.

The workflow store is authoritative. This module writes state and outbox records
in one transaction; broker adapters must publish only claimed outbox envelopes
and acknowledge them only after a broker publisher-confirm response.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from .contracts import validate_workflow_event


class WorkflowIdempotencyConflictError(ValueError):
    """A tenant reused an idempotency key with a different request body."""


class OutboxLeaseLostError(RuntimeError):
    """A relay attempted to settle an outbox row without its current lease."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class PostgresWorkflowStore:
    """Tenant-scoped canonical workflow intake and transactional outbox."""

    def __init__(self, dsn: str):
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self.dsn = dsn

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install psycopg to use PostgresWorkflowStore") from exc
        return psycopg.connect(self.dsn)

    @staticmethod
    def _set_tenant(conn, tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id is required")
        conn.execute(
            "SELECT set_config('rusa.tenant_id', %s, true)",
            (tenant_id,),
        )

    def submit_workflow(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        request: dict[str, Any],
        initial_event: dict[str, Any],
    ) -> tuple[str, bool]:
        """Persist a workflow and its first command atomically.

        Returns (run_id, created). Replaying the same key and body returns the
        original run. Reusing the key with a different body is rejected.
        """
        validate_workflow_event(initial_event)
        if initial_event.get("tenant_id") != tenant_id:
            raise ValueError("event tenant_id does not match authenticated tenant")
        if initial_event.get("type") != "task.request":
            raise ValueError("initial workflow event must be task.request")
        if not idempotency_key or len(idempotency_key) > 256:
            raise ValueError("idempotency_key must contain 1..256 characters")
        run_id = str(UUID(initial_event["run_id"]))
        request_hash = _sha256(request)
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            inserted = conn.execute(
                """
                INSERT INTO public.devops_workflows
                    (run_id, tenant_id, idempotency_key, request_sha256, request)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                RETURNING run_id
                """,
                (run_id, tenant_id, idempotency_key, request_hash, _canonical_json(request)),
            ).fetchone()
            if inserted is None:
                existing = conn.execute(
                    """
                    SELECT run_id, request_sha256
                    FROM public.devops_workflows
                    WHERE tenant_id=%s AND idempotency_key=%s
                    """,
                    (tenant_id, idempotency_key),
                ).fetchone()
                if existing is None:
                    raise RuntimeError("idempotent workflow row disappeared during intake")
                if existing[1] != request_hash:
                    raise WorkflowIdempotencyConflictError(
                        "idempotency key was already used for a different request body"
                    )
                return str(existing[0]), False

            conn.execute(
                """
                INSERT INTO public.devops_event_outbox
                    (tenant_id, run_id, task_id, attempt, message_id,
                     destination, event_type, aggregate_version, envelope)
                VALUES (%s, %s, %s, %s, %s, 'commands', %s, 1, %s::jsonb)
                """,
                (
                    tenant_id,
                    run_id,
                    initial_event["task_id"],
                    initial_event["attempt"],
                    initial_event["message_id"],
                    initial_event["type"],
                    _canonical_json(initial_event),
                ),
            )
        return run_id, True

    def append_outbox_event(
        self,
        *,
        tenant_id: str,
        event: dict[str, Any],
        destination: str = "events",
        aggregate_version: int,
    ) -> None:
        """Append a validated event in the caller's tenant transaction."""
        validate_workflow_event(event)
        if event.get("tenant_id") != tenant_id:
            raise ValueError("event tenant_id does not match authenticated tenant")
        if destination not in {"commands", "events"}:
            raise ValueError("destination must be commands or events")
        if not isinstance(aggregate_version, int) or aggregate_version < 1:
            raise ValueError("aggregate_version must be a positive integer")
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            conn.execute(
                """
                INSERT INTO public.devops_event_outbox
                    (tenant_id, run_id, task_id, attempt, message_id,
                     destination, event_type, aggregate_version, envelope)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    tenant_id,
                    UUID(event["run_id"]),
                    UUID(event["task_id"]) if event.get("task_id") else None,
                    event.get("attempt"),
                    event["message_id"],
                    destination,
                    event["type"],
                    aggregate_version,
                    _canonical_json(event),
                ),
            )


class PostgresOutboxRelay:
    """Cross-tenant relay primitive; connect only with the dedicated relay DB role."""

    def __init__(self, relay_dsn: str):
        if not isinstance(relay_dsn, str) or not relay_dsn.strip():
            raise ValueError("relay PostgreSQL DSN is required")
        self.dsn = relay_dsn

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install psycopg to use PostgresOutboxRelay") from exc
        return psycopg.connect(self.dsn)

    def claim(self, *, destination: str, limit: int = 50, lease_seconds: int = 30) -> list[dict[str, Any]]:
        if destination not in {"commands", "events"}:
            raise ValueError("destination must be commands or events")
        if not 1 <= limit <= 500 or not 1 <= lease_seconds <= 3600:
            raise ValueError("outbox claim limits are outside the supported range")
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH candidates AS (
                    SELECT outbox_id
                    FROM public.devops_event_outbox
                    WHERE destination=%s
                      AND available_at <= clock_timestamp()
                      AND (
                          status='pending'
                          OR (status='publishing' AND lease_until < clock_timestamp())
                      )
                    ORDER BY outbox_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE public.devops_event_outbox AS outbox
                SET status='publishing',
                    attempts=outbox.attempts + 1,
                    lease_token=gen_random_uuid(),
                    lease_until=clock_timestamp() + (%s * INTERVAL '1 second')
                FROM candidates
                WHERE outbox.outbox_id=candidates.outbox_id
                RETURNING outbox.outbox_id, outbox.tenant_id, outbox.message_id,
                          outbox.destination, outbox.envelope, outbox.attempts,
                          outbox.lease_token, outbox.lease_until
                """,
                (destination, limit, lease_seconds),
            ).fetchall()
        return [
            {
                "outbox_id": row[0],
                "tenant_id": row[1],
                "message_id": row[2],
                "destination": row[3],
                "envelope": row[4],
                "attempts": row[5],
                "lease_token": str(row[6]),
                "lease_until": row[7].isoformat(),
            }
            for row in rows
        ]

    def mark_published(self, *, outbox_id: int, lease_token: str) -> None:
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE public.devops_event_outbox
                SET status='published', published_at=clock_timestamp(),
                    lease_token=NULL, lease_until=NULL, last_error=NULL
                WHERE outbox_id=%s AND status='publishing' AND lease_token=%s
                RETURNING outbox_id
                """,
                (outbox_id, lease_token),
            ).fetchone()
            if row is None:
                raise OutboxLeaseLostError("outbox lease is no longer owned by this relay")

    def retry(
        self,
        *,
        outbox_id: int,
        lease_token: str,
        error: str,
        delay_seconds: int,
        max_attempts: int = 8,
    ) -> str:
        if not 0 <= delay_seconds <= 86400 or not 1 <= max_attempts <= 100:
            raise ValueError("outbox retry limits are outside the supported range")
        safe_error = str(error).replace("\x00", "")[:1000]
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE public.devops_event_outbox
                SET status=CASE WHEN attempts >= %s THEN 'dead_letter' ELSE 'pending' END,
                    available_at=clock_timestamp() + (%s * INTERVAL '1 second'),
                    lease_token=NULL, lease_until=NULL, last_error=%s
                WHERE outbox_id=%s AND status='publishing' AND lease_token=%s
                RETURNING status
                """,
                (max_attempts, delay_seconds, safe_error, outbox_id, lease_token),
            ).fetchone()
            if row is None:
                raise OutboxLeaseLostError("outbox lease is no longer owned by this relay")
            return row[0]


class PostgresInbox:
    """Consumer deduplication primitive; invoke the handler in the same transaction."""

    def __init__(self, dsn: str):
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self.dsn = dsn

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install psycopg to use PostgresInbox") from exc
        return psycopg.connect(self.dsn)

    def process_once(
        self,
        *,
        tenant_id: str,
        consumer_id: str,
        event: dict[str, Any],
        handler: Callable[[Any, dict[str, Any]], None],
    ) -> bool:
        """Atomically deduplicate and apply a local DB handler; return False on replay."""
        validate_workflow_event(event)
        if event.get("tenant_id") != tenant_id:
            raise ValueError("event tenant_id does not match authenticated tenant")
        if not consumer_id or len(consumer_id) > 128:
            raise ValueError("consumer_id must contain 1..128 characters")
        with self._connect() as conn:
            conn.execute(
                "SELECT set_config('rusa.tenant_id', %s, true)",
                (tenant_id,),
            )
            inserted = conn.execute(
                """
                INSERT INTO public.devops_event_inbox
                    (tenant_id, consumer_id, message_id, envelope_sha256)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (tenant_id, consumer_id, message_id) DO NOTHING
                RETURNING inbox_id
                """,
                (tenant_id, consumer_id, event["message_id"], _sha256(event)),
            ).fetchone()
            if inserted is None:
                existing = conn.execute(
                    """
                    SELECT envelope_sha256
                    FROM public.devops_event_inbox
                    WHERE tenant_id=%s AND consumer_id=%s AND message_id=%s
                    """,
                    (tenant_id, consumer_id, event["message_id"]),
                ).fetchone()
                if existing is None or existing[0] != _sha256(event):
                    raise ValueError(
                        "message_id was replayed with a different event envelope"
                    )
                return False
            handler(conn, event)
        return True
