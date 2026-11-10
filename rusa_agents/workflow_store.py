"""PostgreSQL workflow control plane with DAG gating and transactional outbox/inbox."""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .platform_contracts import WorkflowEventV2
from .workflow_graph import WorkflowGraph, WorkflowRunSpec, WorkflowTaskSpec


class WorkflowStoreError(RuntimeError):
    """Base error for durable workflow operations."""


class WorkflowConflictError(WorkflowStoreError):
    """A compare-and-set, uniqueness, or lifecycle precondition failed."""


class WorkflowLeaseLostError(WorkflowStoreError):
    """A worker attempted to update a task without its current lease epoch."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("workflow timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _event(
    *,
    tenant_id: str,
    workflow_id: str,
    run_id: str,
    event_type: str,
    producer_id: str,
    producer_version: str,
    payload: Mapping[str, Any],
    task_id: str | None = None,
    attempt: int | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    sequence: int | None = None,
) -> WorkflowEventV2:
    return WorkflowEventV2(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        run_id=run_id,
        task_id=task_id,
        attempt=attempt,
        correlation_id=correlation_id or workflow_id,
        causation_id=causation_id,
        event_type=event_type,
        producer_id=producer_id,
        producer_version=producer_version,
        sequence=sequence,
        occurred_at=datetime.now(timezone.utc),
        payload=dict(payload),
    )


class PostgresWorkflowStore:
    """Durable workflow state. PostgreSQL remains the source of truth and broker boundary."""

    def __init__(self, dsn: str):
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self.dsn = dsn

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install the PostgreSQL runtime dependencies to use PostgresWorkflowStore") from exc
        return psycopg.connect(self.dsn)

    @staticmethod
    def _set_tenant(conn, tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id is required for workflow data access")
        conn.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))

    def initialize(self) -> None:
        """Verify the workflow migrations and forced RLS; never create production schema at runtime."""
        with self._connect() as conn:
            required = {
                "rusa_workflow_runs", "rusa_workflow_tasks",
                "rusa_workflow_dependencies", "rusa_workflow_events",
                "rusa_workflow_outbox", "rusa_workflow_inbox",
                "rusa_agent_registry", "rusa_policy_decisions",
                "rusa_human_approvals", "rusa_evidence_records",
            }
            rows = conn.execute(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND c.relname = ANY(%s)
                """,
                (list(required),),
            ).fetchall()
            states = {row[0]: (row[1], row[2]) for row in rows}
            if set(states) != required:
                missing = sorted(required - set(states))
                raise RuntimeError(
                    "Platform Core migrations are missing; apply 009 and 010. Missing: "
                    + ", ".join(missing)
                )
            if any(not enabled or not forced for enabled, forced in states.values()):
                raise RuntimeError("Platform Core tables require ENABLE and FORCE ROW LEVEL SECURITY")

    @staticmethod
    def _append_event(conn, event: WorkflowEventV2, destination: str = "workflow.events") -> bool:
        payload = event.model_dump(mode="json")
        inserted = conn.execute(
            """
            INSERT INTO public.rusa_workflow_events(
                tenant_id, event_id, workflow_id, run_id, task_id,
                schema_version, event_type, producer_id, producer_version,
                message_id, correlation_id, causation_id, traceparent,
                sequence, occurred_at, payload
            )
            VALUES (
                %s, %s::uuid, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s::jsonb
            )
            ON CONFLICT (tenant_id, message_id) DO NOTHING
            RETURNING event_id
            """,
            (
                event.tenant_id, event.event_id, event.workflow_id, event.run_id,
                event.task_id, event.schema_version, event.event_type,
                event.producer_id, event.producer_version, event.message_id,
                event.correlation_id, event.causation_id, event.traceparent,
                event.sequence, event.occurred_at, _json(event.payload),
            ),
        ).fetchone()
        if inserted is None:
            return False
        conn.execute(
            """
            INSERT INTO public.rusa_workflow_outbox(tenant_id, event_id, destination)
            VALUES (%s, %s::uuid, %s)
            ON CONFLICT (tenant_id, event_id) DO NOTHING
            """,
            (event.tenant_id, event.event_id, destination),
        )
        return True

    def create_workflow(
        self,
        run: WorkflowRunSpec,
        graph: WorkflowGraph,
        *,
        producer_version: str = "2.0.0",
    ) -> dict[str, Any]:
        graph.validate()
        if graph.run != run:
            raise ValueError("workflow graph run identity does not match the submitted run")
        max_parallel = run.budget.get("max_parallel_tasks", 1)
        if type(max_parallel) is not int or not 1 <= max_parallel <= 100:
            raise ValueError("workflow max_parallel_tasks must be an integer between 1 and 100")

        with self._connect() as conn:
            self._set_tenant(conn, run.tenant_id)
            inserted = conn.execute(
                """
                INSERT INTO public.rusa_workflow_runs(
                    tenant_id, workflow_id, run_id, domain, objective, state,
                    deadline_at, budget, idempotency_key, created_by
                )
                VALUES (%s, %s, %s, %s, %s, 'queued', %s, %s::jsonb, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING run_id
                """,
                (
                    run.tenant_id, run.workflow_id, run.run_id, run.domain,
                    run.objective, _aware(run.deadline_at), _json(dict(run.budget)),
                    run.idempotency_key, run.created_by,
                ),
            ).fetchone()
            if inserted is None:
                raise WorkflowConflictError("workflow run or idempotency key already exists")

            for task in graph.tasks:
                if task.deadline_at > run.deadline_at:
                    raise ValueError(f"task {task.task_id} deadline exceeds workflow deadline")
                conn.execute(
                    """
                    INSERT INTO public.rusa_workflow_tasks(
                        tenant_id, workflow_id, run_id, task_id, domain,
                        agent_id, agent_version, capability, status, phase,
                        max_attempts, priority, deadline_at, budget, input,
                        input_refs, idempotency_key
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, 'queued', 'received',
                        %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s
                    )
                    """,
                    (
                        run.tenant_id, run.workflow_id, run.run_id, task.task_id,
                        task.domain, task.agent_id, task.agent_version, task.capability,
                        task.max_attempts, task.priority, _aware(task.deadline_at),
                        _json(dict(task.budget)), _json(dict(task.input)),
                        _json([dict(item) for item in task.input_refs]), task.idempotency_key,
                    ),
                )
            dependency_count = 0
            for task in graph.tasks:
                for dependency_id in task.dependencies:
                    conn.execute(
                        """
                        INSERT INTO public.rusa_workflow_dependencies(
                            tenant_id, workflow_id, run_id, task_id, depends_on_task_id
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        (run.tenant_id, run.workflow_id, run.run_id, task.task_id, dependency_id),
                    )
                    dependency_count += 1

            self._append_event(
                conn,
                _event(
                    tenant_id=run.tenant_id,
                    workflow_id=run.workflow_id,
                    run_id=run.run_id,
                    event_type="workflow.created",
                    producer_id="rusa.workflow_store",
                    producer_version=producer_version,
                    payload={
                        "task_count": len(graph.tasks),
                        "dependency_count": dependency_count,
                        "state_version": 1,
                    },
                    sequence=1,
                ),
            )
        return {
            "tenant_id": run.tenant_id,
            "workflow_id": run.workflow_id,
            "run_id": run.run_id,
            "task_ids": [task.task_id for task in graph.tasks],
            "state": "queued",
            "state_version": 1,
        }

    def start_run(
        self,
        tenant_id: str,
        run_id: str,
        *,
        expected_state_version: int = 1,
        producer_version: str = "2.0.0",
    ) -> int:
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute(
                """
                UPDATE public.rusa_workflow_runs
                SET state='running', state_version=state_version+1, updated_at=clock_timestamp()
                WHERE tenant_id=%s AND run_id=%s AND state='queued'
                  AND state_version=%s AND deadline_at > clock_timestamp()
                RETURNING workflow_id, state_version
                """,
                (tenant_id, run_id, expected_state_version),
            ).fetchone()
            if row is None:
                raise WorkflowConflictError("workflow run is stale, expired, or not queued")
            self._append_event(
                conn,
                _event(
                    tenant_id=tenant_id,
                    workflow_id=row[0],
                    run_id=run_id,
                    event_type="workflow.started",
                    producer_id="rusa.workflow_store",
                    producer_version=producer_version,
                    payload={"state": "running", "state_version": row[1]},
                    sequence=row[1],
                ),
            )
            return int(row[1])

    def block_failed_dependencies(
        self,
        *,
        producer_version: str = "2.0.0",
    ) -> int:
        """Block queued tasks whose upstream dependency reached a terminal failure."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                UPDATE public.rusa_workflow_tasks t
                SET status='blocked', phase='blocked',
                    last_error='A required dependency did not succeed',
                    state_version=t.state_version+1, updated_at=clock_timestamp()
                WHERE t.status='queued'
                  AND EXISTS (
                      SELECT 1
                      FROM public.rusa_workflow_dependencies d
                      JOIN public.rusa_workflow_tasks dependency
                        ON dependency.tenant_id=d.tenant_id
                       AND dependency.run_id=d.run_id
                       AND dependency.task_id=d.depends_on_task_id
                      WHERE d.tenant_id=t.tenant_id AND d.run_id=t.run_id
                        AND d.task_id=t.task_id
                        AND dependency.status IN ('failed', 'blocked', 'cancelled', 'skipped', 'expired')
                  )
                RETURNING t.tenant_id, t.workflow_id, t.run_id, t.task_id,
                          t.attempt, t.state_version
                """
            ).fetchall()
            for tenant_id, workflow_id, run_id, task_id, attempt, state_version in rows:
                self._set_tenant(conn, tenant_id)
                self._append_event(
                    conn,
                    _event(
                        tenant_id=tenant_id,
                        workflow_id=workflow_id,
                        run_id=run_id,
                        task_id=task_id,
                        attempt=max(1, attempt),
                        event_type="workflow.task.blocked",
                        producer_id="rusa.workflow_scheduler",
                        producer_version=producer_version,
                        payload={
                            "reason_code": "dependency_failed",
                            "state_version": state_version,
                        },
                        sequence=state_version,
                    ),
                )
            return len(rows)

    def claim_ready_tasks(
        self,
        worker_id: str,
        *,
        limit: int = 10,
        lease_seconds: int = 30,
        producer_version: str = "2.0.0",
    ) -> list[dict[str, Any]]:
        if not worker_id or len(worker_id) > 128 or not 1 <= limit <= 100:
            raise ValueError("worker_id and a claim limit between 1 and 100 are required")
        if not 15 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 15 and 3600")
        self.block_failed_dependencies(producer_version=producer_version)
        claimed: list[dict[str, Any]] = []
        with self._connect() as conn:
            # Lock each eligible run row first. Concurrent schedulers skip a locked run,
            # which makes the per-run max_parallel_tasks check serializable.
            rows = conn.execute(
                """
                WITH eligible_runs AS (
                    SELECT r.tenant_id, r.workflow_id, r.run_id,
                           GREATEST(1, LEAST(100,
                               COALESCE((r.budget->>'max_parallel_tasks')::integer, 1)
                           )) AS max_parallel,
                           (
                               SELECT COUNT(*) FROM public.rusa_workflow_tasks active
                               WHERE active.tenant_id=r.tenant_id AND active.run_id=r.run_id
                                 AND active.status='running' AND active.lease_until > clock_timestamp()
                           ) AS active_count
                    FROM public.rusa_workflow_runs r
                    WHERE r.state='running' AND r.deadline_at > clock_timestamp()
                    ORDER BY r.created_at ASC
                    FOR UPDATE OF r SKIP LOCKED
                    LIMIT 100
                ),
                candidate_rows AS (
                    SELECT t.tenant_id, t.workflow_id, t.run_id, t.task_id,
                           r.max_parallel - r.active_count AS slots,
                           ROW_NUMBER() OVER (
                               PARTITION BY t.tenant_id, t.run_id
                               ORDER BY t.priority DESC, t.created_at ASC
                           ) AS slot
                    FROM eligible_runs r
                    JOIN public.rusa_workflow_tasks t
                      ON t.tenant_id=r.tenant_id AND t.run_id=r.run_id
                    WHERE (
                        (t.status='queued' AND t.available_at <= clock_timestamp())
                        OR (t.status='running' AND t.lease_until < clock_timestamp())
                    )
                      AND t.attempt < t.max_attempts
                      AND t.deadline_at > clock_timestamp()
                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.rusa_workflow_dependencies d
                          JOIN public.rusa_workflow_tasks dependency
                            ON dependency.tenant_id=d.tenant_id
                           AND dependency.run_id=d.run_id
                           AND dependency.task_id=d.depends_on_task_id
                          WHERE d.tenant_id=t.tenant_id AND d.run_id=t.run_id
                            AND d.task_id=t.task_id
                            AND dependency.status <> 'succeeded'
                      )
                ),
                selected AS (
                    SELECT tenant_id, task_id
                    FROM candidate_rows
                    WHERE slot <= slots
                    ORDER BY slots DESC, task_id
                    LIMIT %s
                )
                UPDATE public.rusa_workflow_tasks t
                SET status='running', phase='executing', attempt=t.attempt+1,
                    lease_epoch=t.lease_epoch+1, lease_owner=%s,
                    lease_until=clock_timestamp() + (%s * interval '1 second'),
                    state_version=t.state_version+1, updated_at=clock_timestamp()
                FROM selected s
                WHERE t.tenant_id=s.tenant_id AND t.task_id=s.task_id
                  AND (
                    (t.status='queued' AND t.available_at <= clock_timestamp())
                    OR (t.status='running' AND t.lease_until < clock_timestamp())
                  )
                RETURNING t.tenant_id, t.workflow_id, t.run_id, t.task_id,
                          t.domain, t.agent_id, t.agent_version, t.capability,
                          t.attempt, t.lease_epoch, t.state_version, t.deadline_at,
                          t.budget, t.input, t.input_refs
                """,
                (limit, worker_id, lease_seconds),
            ).fetchall()

            for row in rows:
                item = {
                    "tenant_id": row[0], "workflow_id": row[1], "run_id": row[2],
                    "task_id": row[3], "domain": row[4], "agent_id": row[5],
                    "agent_version": row[6], "capability": row[7], "attempt": row[8],
                    "lease_epoch": row[9], "state_version": row[10],
                    "deadline_at": row[11].isoformat(), "budget": row[12],
                    "input": row[13], "input_refs": row[14],
                    "worker_id": worker_id,
                }
                self._set_tenant(conn, item["tenant_id"])
                self._append_event(
                    conn,
                    _event(
                        tenant_id=item["tenant_id"],
                        workflow_id=item["workflow_id"],
                        run_id=item["run_id"],
                        task_id=item["task_id"],
                        attempt=item["attempt"],
                        event_type="task.accepted",
                        producer_id="rusa.workflow_worker",
                        producer_version=producer_version,
                        payload={
                            "worker_id": worker_id,
                            "lease_epoch": item["lease_epoch"],
                            "state_version": item["state_version"],
                        },
                        sequence=item["state_version"],
                    ),
                )
                claimed.append(item)
        return claimed

    def heartbeat_task(
        self,
        tenant_id: str,
        task_id: str,
        worker_id: str,
        lease_epoch: int,
        *,
        lease_seconds: int = 30,
    ) -> bool:
        if not 15 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 15 and 3600")
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute(
                """
                UPDATE public.rusa_workflow_tasks
                SET lease_until=clock_timestamp() + (%s * interval '1 second'),
                    updated_at=clock_timestamp()
                WHERE tenant_id=%s AND task_id=%s AND status='running'
                  AND lease_owner=%s AND lease_epoch=%s AND lease_until > clock_timestamp()
                RETURNING task_id
                """,
                (lease_seconds, tenant_id, task_id, worker_id, lease_epoch),
            ).fetchone()
        return row is not None

    def complete_task(
        self,
        tenant_id: str,
        task_id: str,
        worker_id: str,
        lease_epoch: int,
        expected_state_version: int,
        *,
        result_ref: str,
        result_digest: str,
        producer_version: str = "2.0.0",
    ) -> int:
        import re

        if not result_ref or not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", result_digest):
            raise ValueError("completion requires a result reference and SHA-256 digest")
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute(
                """
                UPDATE public.rusa_workflow_tasks
                SET status='succeeded', phase='succeeded', result_ref=%s,
                    state_version=state_version+1, lease_owner=NULL, lease_until=NULL,
                    updated_at=clock_timestamp()
                WHERE tenant_id=%s AND task_id=%s AND status='running'
                  AND lease_owner=%s AND lease_epoch=%s
                  AND state_version=%s AND lease_until > clock_timestamp()
                RETURNING workflow_id, run_id, attempt, state_version
                """,
                (
                    result_ref, tenant_id, task_id, worker_id, lease_epoch,
                    expected_state_version,
                ),
            ).fetchone()
            if row is None:
                raise WorkflowLeaseLostError("task completion rejected: lease or state version is stale")
            self._append_event(
                conn,
                _event(
                    tenant_id=tenant_id,
                    workflow_id=row[0],
                    run_id=row[1],
                    task_id=task_id,
                    attempt=row[2],
                    event_type="task.completed",
                    producer_id=f"rusa.worker.{worker_id}",
                    producer_version=producer_version,
                    payload={
                        "lease_epoch": lease_epoch,
                        "state_version": row[3],
                        "result_ref": result_ref,
                        "sha256": result_digest,
                    },
                    sequence=row[3],
                ),
            )
            return int(row[3])

    def fail_task(
        self,
        tenant_id: str,
        task_id: str,
        worker_id: str,
        lease_epoch: int,
        expected_state_version: int,
        *,
        code: str,
        category: str,
        side_effect: str = "NONE",
        diagnostic_ref: str | None = None,
        producer_version: str = "2.0.0",
    ) -> int:
        allowed_categories = {"TRANSIENT", "PERMANENT", "POLICY", "AMBIGUOUS"}
        allowed_effects = {"NONE", "APPLIED", "UNKNOWN"}
        if category not in allowed_categories or side_effect not in allowed_effects or not code:
            raise ValueError("failure category, side-effect status, or code is invalid")
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute(
                """
                UPDATE public.rusa_workflow_tasks
                SET status='failed', phase='failed', last_error=%s,
                    state_version=state_version+1, lease_owner=NULL, lease_until=NULL,
                    updated_at=clock_timestamp()
                WHERE tenant_id=%s AND task_id=%s AND status='running'
                  AND lease_owner=%s AND lease_epoch=%s
                  AND state_version=%s AND lease_until > clock_timestamp()
                RETURNING workflow_id, run_id, attempt, state_version
                """,
                (
                    code[:4000], tenant_id, task_id, worker_id, lease_epoch,
                    expected_state_version,
                ),
            ).fetchone()
            if row is None:
                raise WorkflowLeaseLostError("task failure report rejected: lease or state version is stale")
            self._append_event(
                conn,
                _event(
                    tenant_id=tenant_id,
                    workflow_id=row[0],
                    run_id=row[1],
                    task_id=task_id,
                    attempt=row[2],
                    event_type="task.failed",
                    producer_id=f"rusa.worker.{worker_id}",
                    producer_version=producer_version,
                    payload={
                        "lease_epoch": lease_epoch,
                        "state_version": row[3],
                        "code": code[:128],
                        "category": category,
                        "side_effect": side_effect,
                        "diagnostic_ref": diagnostic_ref,
                    },
                    sequence=row[3],
                ),
            )
            return int(row[3])

    def retry_task(
        self,
        tenant_id: str,
        task_id: str,
        *,
        retry_delay_seconds: int = 30,
        producer_version: str = "2.0.0",
    ) -> int:
        if not 0 <= retry_delay_seconds <= 3600:
            raise ValueError("retry delay must be between 0 and 3600 seconds")
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute(
                """
                UPDATE public.rusa_workflow_tasks t
                SET status='queued', phase='received', available_at=clock_timestamp()
                    + (%s * interval '1 second'), last_error=NULL,
                    state_version=t.state_version+1, updated_at=clock_timestamp()
                FROM public.rusa_workflow_runs r
                WHERE t.tenant_id=%s AND t.task_id=%s AND t.status='failed'
                  AND t.attempt < t.max_attempts
                  AND t.deadline_at > clock_timestamp()
                  AND r.tenant_id=t.tenant_id AND r.run_id=t.run_id
                  AND r.state='running' AND r.deadline_at > clock_timestamp()
                RETURNING t.workflow_id, t.run_id, t.attempt, t.state_version
                """,
                (retry_delay_seconds, tenant_id, task_id),
            ).fetchone()
            if row is None:
                raise WorkflowConflictError("task is not retryable or its workflow has expired")
            self._append_event(
                conn,
                _event(
                    tenant_id=tenant_id,
                    workflow_id=row[0],
                    run_id=row[1],
                    task_id=task_id,
                    attempt=row[2] + 1,
                    event_type="workflow.task.retry_scheduled",
                    producer_id="rusa.workflow_store",
                    producer_version=producer_version,
                    payload={"next_attempt": row[2] + 1, "state_version": row[3]},
                    sequence=row[3],
                ),
            )
            return int(row[3])

    def cancel_run(
        self,
        tenant_id: str,
        run_id: str,
        *,
        expected_state_version: int,
        reason_code: str = "user_requested",
        producer_version: str = "2.0.0",
    ) -> int:
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute(
                """
                UPDATE public.rusa_workflow_runs
                SET state='cancel_requested', cancel_epoch=cancel_epoch+1,
                    state_version=state_version+1, updated_at=clock_timestamp()
                WHERE tenant_id=%s AND run_id=%s
                  AND state IN ('queued', 'planning', 'running', 'validating', 'awaiting_approval')
                  AND state_version=%s
                RETURNING workflow_id, cancel_epoch, state_version
                """,
                (tenant_id, run_id, expected_state_version),
            ).fetchone()
            if row is None:
                raise WorkflowConflictError("workflow run is stale or already terminal")
            conn.execute(
                """
                UPDATE public.rusa_workflow_tasks
                SET status='cancelled', phase='cancelled', state_version=state_version+1,
                    lease_owner=NULL, lease_until=NULL, updated_at=clock_timestamp()
                WHERE tenant_id=%s AND run_id=%s AND status='queued'
                """,
                (tenant_id, run_id),
            )
            running = conn.execute(
                """
                SELECT task_id, attempt, state_version
                FROM public.rusa_workflow_tasks
                WHERE tenant_id=%s AND run_id=%s AND status='running'
                """,
                (tenant_id, run_id),
            ).fetchall()
            for task_id, attempt, state_version in running:
                self._append_event(
                    conn,
                    _event(
                        tenant_id=tenant_id,
                        workflow_id=row[0],
                        run_id=run_id,
                        task_id=task_id,
                        attempt=attempt,
                        event_type="task.cancel.requested",
                        producer_id="rusa.workflow_store",
                        producer_version=producer_version,
                        payload={
                            "cancel_epoch": row[1],
                            "reason_code": reason_code[:128],
                        },
                        sequence=state_version + 1,
                    ),
                )
            self._append_event(
                conn,
                _event(
                    tenant_id=tenant_id,
                    workflow_id=row[0],
                    run_id=run_id,
                    event_type="workflow.cancel_requested",
                    producer_id="rusa.workflow_store",
                    producer_version=producer_version,
                    payload={
                        "cancel_epoch": row[1],
                        "state_version": row[2],
                        "reason_code": reason_code[:128],
                    },
                    sequence=row[2],
                ),
            )
            return int(row[2])

    def publish_pending(
        self,
        worker_id: str,
        publisher: Callable[[dict[str, Any]], None],
        *,
        limit: int = 50,
        lease_seconds: int = 30,
        max_attempts: int = 10,
        retry_delay_seconds: int = 15,
    ) -> dict[str, int]:
        """Publish at least once. Consumers must deduplicate with the transactional inbox."""
        if not worker_id or not callable(publisher) or not 1 <= limit <= 500:
            raise ValueError("outbox worker, publisher, and bounded limit are required")
        if not 15 <= lease_seconds <= 3600 or max_attempts < 1 or retry_delay_seconds < 0:
            raise ValueError("outbox lease/retry configuration is invalid")
        token = f"{worker_id}:{uuid4()}"
        with self._connect() as conn:
            claimed = conn.execute(
                """
                WITH candidate AS (
                    SELECT tenant_id, event_id
                    FROM public.rusa_workflow_outbox
                    WHERE (
                        (status='pending' AND available_at <= clock_timestamp())
                        OR (status='publishing' AND locked_until < clock_timestamp())
                    )
                    ORDER BY available_at, created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE public.rusa_workflow_outbox o
                SET status='publishing', attempts=o.attempts+1,
                    locked_by=%s,
                    locked_until=clock_timestamp() + (%s * interval '1 second')
                FROM candidate c
                WHERE o.tenant_id=c.tenant_id AND o.event_id=c.event_id
                RETURNING o.tenant_id, o.event_id::text, o.attempts
                """,
                (limit, token, lease_seconds),
            ).fetchall()

        counts = {"published": 0, "retried": 0, "dead": 0}
        for tenant_id, event_id, attempts in claimed:
            try:
                with self._connect() as conn:
                    self._set_tenant(conn, tenant_id)
                    row = conn.execute(
                        """
                        SELECT tenant_id, event_id::text, workflow_id, run_id, task_id,
                               schema_version, event_type, producer_id, producer_version,
                               message_id, correlation_id, causation_id, traceparent,
                               sequence, occurred_at, payload
                        FROM public.rusa_workflow_events
                        WHERE tenant_id=%s AND event_id=%s::uuid
                        """,
                        (tenant_id, event_id),
                    ).fetchone()
                if row is None:
                    raise WorkflowStoreError("outbox event is missing from the immutable event log")
                publisher({
                    "tenant_id": row[0], "event_id": row[1], "workflow_id": row[2],
                    "run_id": row[3], "task_id": row[4], "schema_version": row[5],
                    "event_type": row[6], "producer_id": row[7], "producer_version": row[8],
                    "message_id": row[9], "correlation_id": row[10], "causation_id": row[11],
                    "traceparent": row[12], "sequence": row[13],
                    "occurred_at": row[14].isoformat(), "payload": row[15],
                })
                with self._connect() as conn:
                    self._set_tenant(conn, tenant_id)
                    conn.execute(
                        """
                        UPDATE public.rusa_workflow_outbox
                        SET status='published', published_at=clock_timestamp(),
                            locked_by=NULL, locked_until=NULL, last_error=NULL
                        WHERE tenant_id=%s AND event_id=%s::uuid
                          AND status='publishing' AND locked_by=%s
                        """,
                        (tenant_id, event_id, token),
                    )
                counts["published"] += 1
            except Exception as exc:
                terminal = attempts >= max_attempts
                with self._connect() as conn:
                    self._set_tenant(conn, tenant_id)
                    conn.execute(
                        """
                        UPDATE public.rusa_workflow_outbox
                        SET status=%s,
                            available_at=clock_timestamp() + (%s * interval '1 second'),
                            locked_by=NULL, locked_until=NULL, last_error=%s
                        WHERE tenant_id=%s AND event_id=%s::uuid
                          AND status='publishing' AND locked_by=%s
                        """,
                        (
                            "dead" if terminal else "pending",
                            retry_delay_seconds, str(exc)[:4000],
                            tenant_id, event_id, token,
                        ),
                    )
                counts["dead" if terminal else "retried"] += 1
        return counts

    def consume_once(
        self,
        tenant_id: str,
        consumer_name: str,
        event: WorkflowEventV2,
        handler: Callable[[Any, WorkflowEventV2], Any],
    ) -> tuple[bool, Any]:
        """Run a database-only handler once; inbox insert and handler share one transaction."""
        if event.tenant_id != tenant_id:
            raise ValueError("event tenant does not match authenticated consumer tenant")
        if not consumer_name or len(consumer_name) > 128 or not callable(handler):
            raise ValueError("consumer identity and handler are required")
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            inserted = conn.execute(
                """
                INSERT INTO public.rusa_workflow_inbox(
                    tenant_id, consumer_name, message_id, event_id, event_type
                )
                VALUES (%s, %s, %s, %s::uuid, %s)
                ON CONFLICT (tenant_id, consumer_name, message_id) DO NOTHING
                RETURNING message_id
                """,
                (tenant_id, consumer_name, event.message_id, event.event_id, event.event_type),
            ).fetchone()
            if inserted is None:
                return False, None
            result = handler(conn, event)
            return True, result
