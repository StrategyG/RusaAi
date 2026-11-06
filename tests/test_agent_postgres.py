import os
from uuid import uuid4

import psycopg
import pytest

from rusa_agents.contracts import CodingTask, RepositoryTarget, TaskRecord, TaskState
from rusa_agents.memory import PostgresMemoryStore
from rusa_agents.queue import PostgresTaskQueue


DSN = os.getenv("RUSA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="RUSA_TEST_DATABASE_URL is not configured")


def make_task(tenant_id, key, repository=None, objective="PostgreSQL queue integration test"):
    return CodingTask(
        tenant_id=tenant_id,
        objective=objective,
        repository=repository or RepositoryTarget(
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
    task = make_task("tenant-integration-a", key)
    duplicate = make_task("tenant-integration-a", key, repository=task.repository)
    other_tenant = make_task("tenant-integration-b", key)
    first_id = queue.enqueue(task)
    assert queue.enqueue(duplicate) == first_id
    conflict = make_task(
        "tenant-integration-a", key, repository=task.repository,
        objective="Different payload with the same key",
    )
    with pytest.raises(ValueError, match="different task payload"):
        queue.enqueue(conflict)
    assert queue.enqueue(other_tenant) != first_id

    claimed = queue.claim()
    assert claimed is not None
    assert claimed["task_id"] == first_id
    record = TaskRecord(task=task)
    record.transition(TaskState.OBSERVING)
    queue.acknowledge(task.tenant_id, first_id, record, claimed["lease_token"])
    fetched = queue.get(task.tenant_id, first_id)
    assert fetched is not None
    assert fetched["status"] == "observing"
    assert fetched["result"]["state"] == "observing"


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



def test_postgres_queue_fences_expired_lease_owners():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    marker = uuid4().hex
    task = make_task("tenant-lease-test", f"lease-{marker}")
    task_id = queue.enqueue(task)
    try:
        with psycopg.connect(DSN) as connection:
            connection.execute(
                "UPDATE rusa_agent_tasks SET created_at=now()-interval '100 years' "
                "WHERE tenant_id=%s AND task_id=%s",
                (task.tenant_id, task_id),
            )
        first = queue.claim(lease_seconds=10)
        assert first is not None and first["task_id"] == task_id
        with psycopg.connect(DSN) as connection:
            connection.execute(
                "UPDATE rusa_agent_tasks SET lease_until=now()-interval '1 second' "
                "WHERE tenant_id=%s AND task_id=%s",
                (task.tenant_id, task_id),
            )
        second = queue.claim(lease_seconds=10)
        assert second is not None and second["task_id"] == task_id
        assert second["lease_token"] != first["lease_token"]

        record = TaskRecord(task=task)
        record.transition(TaskState.OBSERVING)
        with pytest.raises(RuntimeError, match="lease was lost"):
            queue.acknowledge(task.tenant_id, task_id, record, first["lease_token"])
        assert not queue.checkpoint(
            task.tenant_id, task_id, first["lease_token"], record,
        )
        assert queue.checkpoint(
            task.tenant_id, task_id, second["lease_token"], record,
        )
        assert not queue.renew_lease(
            task.tenant_id, task_id, first["lease_token"], lease_seconds=10,
        )
        assert queue.renew_lease(
            task.tenant_id, task_id, second["lease_token"], lease_seconds=10,
        )
        queue.acknowledge(task.tenant_id, task_id, record, second["lease_token"])
    finally:
        with psycopg.connect(DSN) as connection:
            connection.execute(
                "DELETE FROM rusa_agent_tasks WHERE tenant_id=%s AND task_id=%s",
                (task.tenant_id, task_id),
            )



def test_api_database_role_has_only_task_submit_and_read_privileges():
    with psycopg.connect(DSN) as connection:
        connection.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rusa_agent_api') THEN
                    CREATE ROLE rusa_agent_api NOLOGIN NOBYPASSRLS;
                END IF;
            END $$;
        """)
        connection.execute("GRANT USAGE ON SCHEMA public TO rusa_agent_api")
    PostgresTaskQueue(DSN).initialize()
    PostgresMemoryStore(DSN).initialize()
    with psycopg.connect(DSN) as connection:
        privileges = connection.execute("""
            SELECT
                has_table_privilege('rusa_agent_api', 'rusa_agent_tasks', 'SELECT'),
                has_table_privilege('rusa_agent_api', 'rusa_agent_tasks', 'INSERT'),
                has_table_privilege('rusa_agent_api', 'rusa_agent_tasks', 'UPDATE'),
                has_table_privilege('rusa_agent_api', 'rusa_agent_tasks', 'DELETE'),
                has_table_privilege('rusa_agent_api', 'rusa_agent_memory', 'SELECT')
        """).fetchone()
    assert privileges == (True, True, False, False, False)
