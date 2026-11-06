import os
from uuid import uuid4

import psycopg
import pytest

from rusa_agents.contracts import CodingTask, RepositoryTarget, TaskRecord, TaskState
from rusa_agents.memory import PostgresMemoryStore
from rusa_agents.queue import PostgresTaskQueue


DSN = os.getenv("RUSA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="RUSA_TEST_DATABASE_URL is not configured")


def make_task(tenant_id, key):
    return CodingTask(
        tenant_id=tenant_id,
        objective="PostgreSQL queue integration test",
        repository=RepositoryTarget(
            full_name="StrategyG/RusaAi",
            base_branch="main",
            work_branch=f"test/{uuid4().hex[:12]}",
        ),
        acceptance_criteria=("Queue is tenant scoped",),
        idempotency_key=key,
    )


def test_postgres_queue_idempotency_claim_and_checkpoint():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    key = f"queue-{uuid4().hex}"
    tenant_id = f"tenant-integration-{uuid4().hex}"
    task = make_task(tenant_id, key)
    duplicate = CodingTask.from_dict(task.to_dict())
    other_tenant = make_task(f"{tenant_id}-other", key)
    first_id = queue.enqueue(task)
    assert queue.enqueue(duplicate) == first_id
    assert queue.enqueue(other_tenant) != first_id

    claimed = queue.claim("integration-worker", lease_seconds=60)
    assert claimed is not None
    assert claimed["task_id"] == first_id
    record = TaskRecord(task=task)
    record.transition(TaskState.OBSERVING)
    queue.acknowledge(
        task.tenant_id,
        first_id,
        record,
        worker_id=claimed["lease_owner"],
    )
    fetched = queue.get(task.tenant_id, first_id)
    assert fetched is not None
    assert fetched["status"] == "observing"
    assert fetched["result"]["state"] == "observing"
    with pytest.raises(ValueError, match="different task payload"):
        changed = task.to_dict()
        changed["objective"] = "Different objective"
        queue.enqueue(CodingTask.from_dict(changed))
    with psycopg.connect(DSN) as connection:
        connection.execute(
            "DELETE FROM rusa_agent_tasks WHERE tenant_id IN (%s, %s)",
            (tenant_id, f"{tenant_id}-other"),
        )


def test_postgres_memory_is_tenant_scoped_and_deletable():
    memory = PostgresMemoryStore(DSN)
    memory.initialize()
    marker = uuid4().hex
    memory.store(
        "tenant-memory-a",
        f"unique project memory marker {marker}",
        source="integration-test",
    )
    memory.store(
        "tenant-memory-b",
        f"unique project memory marker {marker}",
        source="integration-test",
    )
    result_a = memory.search("tenant-memory-a", marker)
    result_b = memory.search("tenant-memory-b", marker)
    assert result_a and result_b
    assert all("tenant-memory-b" not in item["content"] for item in result_a)
    assert memory.delete_tenant("tenant-memory-a") >= 1
    assert memory.search("tenant-memory-a", marker) == []


def test_postgres_row_level_security_filters_and_rejects_other_tenants():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    memory = PostgresMemoryStore(DSN)
    memory.initialize()
    marker = uuid4().hex
    task_a = make_task("tenant-rls-a", f"rls-a-{marker}")
    task_b = make_task("tenant-rls-b", f"rls-b-{marker}")
    id_a = queue.enqueue(task_a)
    id_b = queue.enqueue(task_b)
    memory.store("tenant-rls-a", f"rls memory {marker}", source="test")
    memory.store("tenant-rls-b", f"rls memory {marker}", source="test")

    with psycopg.connect(DSN) as connection:
        connection.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='rusa_agent_test_rls') THEN
                    CREATE ROLE rusa_agent_test_rls NOLOGIN NOBYPASSRLS;
                END IF;
            END
            $$
        """)
        connection.execute("GRANT USAGE ON SCHEMA public TO rusa_agent_test_rls")
        connection.execute("GRANT SELECT, INSERT ON rusa_agent_tasks TO rusa_agent_test_rls")
        connection.execute("GRANT SELECT ON rusa_agent_memory TO rusa_agent_test_rls")
        connection.execute("SET ROLE rusa_agent_test_rls")
        connection.execute("SELECT set_config('rusa.tenant_id', %s, true)", ("tenant-rls-a",))
        task_rows = connection.execute(
            "SELECT tenant_id, task_id FROM rusa_agent_tasks WHERE task_id IN (%s, %s)",
            (id_a, id_b),
        ).fetchall()
        memory_rows = connection.execute(
            "SELECT tenant_id, content FROM rusa_agent_memory WHERE content LIKE %s",
            (f"%{marker}%",),
        ).fetchall()

    assert task_rows == [("tenant-rls-a", id_a)]
    assert memory_rows == [("tenant-rls-a", f"rls memory {marker}")]

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(DSN) as connection:
            connection.execute("SET ROLE rusa_agent_test_rls")
            connection.execute("SELECT set_config('rusa.tenant_id', %s, true)", ("tenant-rls-a",))
            connection.execute(
                """
                INSERT INTO rusa_agent_tasks
                    (tenant_id, task_id, idempotency_key, payload)
                VALUES (%s, %s, %s, '{}'::jsonb)
                """,
                ("tenant-rls-b", f"forbidden-{marker}", f"forbidden-{marker}"),
            )


def test_postgres_queue_lease_heartbeat_and_stale_owner_fencing():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    tenant_id = f"tenant-lease-{uuid4().hex}"
    task = make_task(tenant_id, f"lease-{uuid4().hex}")
    task_id = queue.enqueue(task)
    first = queue.claim("worker-one", lease_seconds=60)
    assert first is not None and first["task_id"] == task_id
    assert queue.heartbeat(tenant_id, task_id, "worker-one", lease_seconds=60)

    with psycopg.connect(DSN) as connection:
        connection.execute(
            """
            UPDATE rusa_agent_tasks
            SET lease_until=now() - interval '1 second'
            WHERE tenant_id=%s AND task_id=%s
            """,
            (tenant_id, task_id),
        )

    second = queue.claim("worker-two", lease_seconds=60)
    assert second is not None and second["task_id"] == task_id
    assert not queue.heartbeat(tenant_id, task_id, "worker-one", lease_seconds=60)
    stale_record = TaskRecord(task=task)
    stale_record.transition(TaskState.OBSERVING)
    with pytest.raises(RuntimeError, match="lease was lost"):
        queue.acknowledge(tenant_id, task_id, stale_record, worker_id="worker-one")
    queue.acknowledge(tenant_id, task_id, stale_record, worker_id="worker-two")
    with psycopg.connect(DSN) as connection:
        connection.execute("DELETE FROM rusa_agent_tasks WHERE tenant_id=%s", (tenant_id,))


def test_postgres_queue_readiness_requires_forced_tenant_rls():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    assert queue.check_ready() is True
