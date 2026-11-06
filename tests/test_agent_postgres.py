import os
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest

from rusa_agents.contracts import CodingTask, RepositoryTarget, TaskRecord, TaskState
from rusa_agents.memory import PostgresMemoryStore
from rusa_agents.queue import PostgresTaskQueue


DSN = os.getenv("RUSA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="RUSA_TEST_DATABASE_URL is not configured")


def make_task(tenant_id, key, objective="PostgreSQL queue integration test"):
    task_id = uuid4().hex
    branch = CodingTask.expected_work_branch_for(tenant_id, key, task_id)
    return CodingTask(
        tenant_id=tenant_id,
        objective=objective,
        repository=RepositoryTarget(
            full_name="StrategyG/RusaAi",
            base_branch="main",
            work_branch=branch,
        ),
        acceptance_criteria=("Queue is tenant scoped",),
        idempotency_key=key,
        task_id=task_id,
    )


def clean_test_rows():
    PostgresMemoryStore(DSN).initialize()
    with psycopg.connect(DSN) as connection:
        connection.execute(
            "DELETE FROM rusa_agent_tasks WHERE tenant_id LIKE 'rusa-test-%'"
        )
        connection.execute(
            "DELETE FROM rusa_agent_memory WHERE tenant_id LIKE 'rusa-test-%'"
        )


def test_postgres_queue_idempotency_checks_payload_and_reuses_task_id():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    clean_test_rows()
    tenant_id = f"rusa-test-idem-{uuid4().hex}"
    key = f"same-key-{uuid4().hex}"
    first = make_task(tenant_id, key)
    duplicate = make_task(tenant_id, key)
    first_id = queue.enqueue(first)
    assert queue.enqueue(duplicate) == first_id

    changed = replace(first, objective="Different task payload")
    with pytest.raises(ValueError, match="different task payload"):
        queue.enqueue(changed)

    with psycopg.connect(DSN) as connection:
        connection.execute(
            "DELETE FROM rusa_agent_tasks WHERE tenant_id=%s",
            (tenant_id,),
        )


def test_postgres_queue_claim_checkpoint_and_acknowledgement():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    clean_test_rows()
    tenant_id = f"rusa-test-queue-{uuid4().hex}"
    task = make_task(tenant_id, f"queue-{uuid4().hex}")
    task_id = queue.enqueue(task)

    claimed = queue.claim("integration-worker", lease_seconds=60)
    assert claimed is not None
    assert claimed["task_id"] == task_id
    record = TaskRecord(task=task)
    record.transition(TaskState.OBSERVING)
    assert queue.checkpoint(tenant_id, task_id, "integration-worker", record)
    queue.acknowledge(
        tenant_id,
        task_id,
        record,
        worker_id="integration-worker",
    )
    fetched = queue.get(tenant_id, task_id)
    assert fetched is not None
    assert fetched["status"] == "observing"
    assert fetched["result"]["state"] == "observing"
    with psycopg.connect(DSN) as connection:
        connection.execute(
            "DELETE FROM rusa_agent_tasks WHERE tenant_id=%s",
            (tenant_id,),
        )


def test_postgres_memory_is_tenant_scoped_and_deletable():
    memory = PostgresMemoryStore(DSN)
    memory.initialize()
    marker = uuid4().hex
    tenant_a = f"rusa-test-memory-a-{uuid4().hex}"
    tenant_b = f"rusa-test-memory-b-{uuid4().hex}"
    memory.store(tenant_a, f"unique project memory marker {marker}", source="integration-test")
    memory.store(tenant_b, f"unique project memory marker {marker}", source="integration-test")
    result_a = memory.search(tenant_a, marker)
    result_b = memory.search(tenant_b, marker)
    assert result_a and result_b
    assert all(item["content"].startswith("unique project") for item in result_a)
    assert memory.delete_tenant(tenant_a) >= 1
    assert memory.search(tenant_a, marker) == []
    with psycopg.connect(DSN) as connection:
        connection.execute(
            "DELETE FROM rusa_agent_memory WHERE tenant_id=%s",
            (tenant_b,),
        )


def test_postgres_row_level_security_filters_and_rejects_other_tenants():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    memory = PostgresMemoryStore(DSN)
    memory.initialize()
    marker = uuid4().hex
    tenant_a = f"rusa-test-rls-a-{uuid4().hex}"
    tenant_b = f"rusa-test-rls-b-{uuid4().hex}"
    task_a = make_task(tenant_a, f"rls-a-{marker}")
    task_b = make_task(tenant_b, f"rls-b-{marker}")
    id_a = queue.enqueue(task_a)
    id_b = queue.enqueue(task_b)
    memory.store(tenant_a, f"rls memory {marker}", source="test")
    memory.store(tenant_b, f"rls memory {marker}", source="test")

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
        connection.execute(
            "SELECT set_config('rusa.tenant_id', %s, true)",
            (tenant_a,),
        )
        task_rows = connection.execute(
            "SELECT tenant_id, task_id FROM rusa_agent_tasks WHERE task_id IN (%s, %s)",
            (id_a, id_b),
        ).fetchall()
        memory_rows = connection.execute(
            "SELECT tenant_id, content FROM rusa_agent_memory WHERE content LIKE %s",
            (f"%{marker}%",),
        ).fetchall()

    assert task_rows == [(tenant_a, id_a)]
    assert memory_rows == [(tenant_a, f"rls memory {marker}")]

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(DSN) as connection:
            connection.execute("SET ROLE rusa_agent_test_rls")
            connection.execute(
                "SELECT set_config('rusa.tenant_id', %s, true)",
                (tenant_a,),
            )
            connection.execute("""
                INSERT INTO rusa_agent_tasks
                    (tenant_id, task_id, idempotency_key, payload)
                VALUES (%s, %s, %s, '{}'::jsonb)
            """, (tenant_b, f"forbidden-{marker}", f"forbidden-{marker}"))

    with psycopg.connect(DSN) as connection:
        connection.execute(
            "DELETE FROM rusa_agent_tasks WHERE tenant_id IN (%s, %s)",
            (tenant_a, tenant_b),
        )
        connection.execute(
            "DELETE FROM rusa_agent_memory WHERE tenant_id IN (%s, %s)",
            (tenant_a, tenant_b),
        )


def test_postgres_queue_lease_heartbeat_and_stale_owner_fencing():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    clean_test_rows()
    tenant_id = f"rusa-test-lease-{uuid4().hex}"
    task = make_task(tenant_id, f"lease-{uuid4().hex}")
    task_id = queue.enqueue(task)

    first = queue.claim("worker-one", lease_seconds=60)
    assert first is not None and first["task_id"] == task_id
    assert queue.heartbeat(tenant_id, task_id, "worker-one", lease_seconds=60)
    assert queue.owns_lease(tenant_id, task_id, "worker-one") is True
    assert queue.owns_lease(tenant_id, task_id, "worker-two") is False

    with psycopg.connect(DSN) as connection:
        connection.execute("""
            UPDATE rusa_agent_tasks
            SET lease_until=now() - interval '1 second'
            WHERE tenant_id=%s AND task_id=%s
        """, (tenant_id, task_id))

    second = queue.claim("worker-two", lease_seconds=60)
    assert second is not None and second["task_id"] == task_id
    assert second["lease_owner"] == "worker-two"
    assert not queue.heartbeat(tenant_id, task_id, "worker-one", lease_seconds=60)
    assert queue.owns_lease(tenant_id, task_id, "worker-one") is False
    assert queue.owns_lease(tenant_id, task_id, "worker-two") is True
    stale_record = TaskRecord(task=task)
    stale_record.transition(TaskState.OBSERVING)
    assert not queue.checkpoint(tenant_id, task_id, "worker-one", stale_record)
    with pytest.raises(RuntimeError, match="lease was lost"):
        queue.acknowledge(
            tenant_id,
            task_id,
            stale_record,
            worker_id="worker-one",
        )
    assert queue.retry(
        tenant_id,
        task_id,
        "stale worker",
        worker_id="worker-one",
    ) is False
    queue.acknowledge(
        tenant_id,
        task_id,
        stale_record,
        worker_id="worker-two",
    )
    with psycopg.connect(DSN) as connection:
        connection.execute(
            "DELETE FROM rusa_agent_tasks WHERE tenant_id=%s",
            (tenant_id,),
        )


def test_postgres_queue_and_memory_readiness_require_forced_tenant_rls():
    queue = PostgresTaskQueue(DSN)
    memory = PostgresMemoryStore(DSN)
    queue.initialize()
    memory.initialize()
    assert queue.check_ready() is True
    assert memory.check_ready() is True
