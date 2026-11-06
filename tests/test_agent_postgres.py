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
    lease_token = claimed["lease_token"]
    assert queue.checkpoint(
        tenant_id, task_id, "integration-worker", lease_token, record,
    )
    queue.acknowledge(
        tenant_id,
        task_id,
        record,
        worker_id="integration-worker",
        lease_token=lease_token,
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


def test_postgres_api_role_has_minimal_task_access_and_tenant_rls():
    queue = PostgresTaskQueue(DSN)
    memory = PostgresMemoryStore(DSN)

    with psycopg.connect(DSN) as connection:
        connection.execute("""
            DO $rusa$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname='rusa_agent_worker'
                ) THEN
                    CREATE ROLE rusa_agent_worker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname='rusa_agent_api'
                ) THEN
                    CREATE ROLE rusa_agent_api NOLOGIN NOSUPERUSER NOBYPASSRLS;
                END IF;
            END
            $rusa$
        """)
        connection.execute(
            "ALTER ROLE rusa_agent_api NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
        )
        connection.execute("GRANT USAGE ON SCHEMA public TO rusa_agent_api")
        connection.execute("GRANT USAGE, CREATE ON SCHEMA public TO rusa_agent_worker")
        connection.execute("""
            ALTER DEFAULT PRIVILEGES FOR ROLE rusa_agent_worker IN SCHEMA public
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO rusa_agent_api
        """)
        connection.execute("""
            ALTER DEFAULT PRIVILEGES FOR ROLE rusa_agent_worker IN SCHEMA public
            GRANT USAGE, SELECT ON SEQUENCES TO rusa_agent_api
        """)

    queue.initialize()
    memory.initialize()
    tenant_a = f"rusa-test-api-a-{uuid4().hex}"
    tenant_b = f"rusa-test-api-b-{uuid4().hex}"
    task_a = make_task(tenant_a, f"api-a-{uuid4().hex}")
    task_b = make_task(tenant_b, f"api-b-{uuid4().hex}")
    task_a_id = queue.enqueue(task_a)
    task_b_id = queue.enqueue(task_b)

    try:
        with psycopg.connect(DSN) as connection:
            privileges = connection.execute("""
                SELECT
                    has_table_privilege('rusa_agent_api', 'public.rusa_agent_tasks', 'SELECT'),
                    has_table_privilege('rusa_agent_api', 'public.rusa_agent_tasks', 'INSERT'),
                    has_table_privilege('rusa_agent_api', 'public.rusa_agent_tasks', 'UPDATE'),
                    has_table_privilege('rusa_agent_api', 'public.rusa_agent_tasks', 'DELETE'),
                    has_table_privilege('rusa_agent_api', 'public.rusa_agent_memory', 'SELECT'),
                    has_table_privilege('rusa_agent_api', 'public.rusa_agent_memory', 'INSERT'),
                    has_sequence_privilege(
                        'rusa_agent_api', 'public.rusa_agent_memory_memory_id_seq', 'USAGE'
                    ),
                    (SELECT rolbypassrls FROM pg_roles WHERE rolname='rusa_agent_api')
            """).fetchone()
        assert privileges == (True, True, False, False, False, False, False, False)

        with psycopg.connect(DSN) as connection:
            connection.execute("SET ROLE rusa_agent_worker")
            connection.execute("""
                CREATE TABLE public.rusa_agent_privilege_probe (
                    id BIGSERIAL PRIMARY KEY,
                    payload TEXT NOT NULL
                )
            """)
        with psycopg.connect(DSN) as connection:
            probe_privileges = connection.execute("""
                SELECT
                    has_table_privilege(
                        'rusa_agent_api', 'public.rusa_agent_privilege_probe', 'SELECT'
                    ),
                    has_table_privilege(
                        'rusa_agent_api', 'public.rusa_agent_privilege_probe', 'INSERT'
                    ),
                    has_sequence_privilege(
                        'rusa_agent_api', 'public.rusa_agent_privilege_probe_id_seq', 'USAGE'
                    )
            """).fetchone()
            connection.execute("DROP TABLE public.rusa_agent_privilege_probe")
        assert probe_privileges == (False, False, False)

        with psycopg.connect(DSN) as connection:
            connection.execute("SET ROLE rusa_agent_api")
            connection.execute(
                "SELECT set_config('rusa.tenant_id', %s, true)", (tenant_a,),
            )
            rows = connection.execute("""
                SELECT tenant_id, task_id FROM rusa_agent_tasks
                WHERE task_id IN (%s, %s)
            """, (task_a_id, task_b_id)).fetchall()
        assert rows == [(tenant_a, task_a_id)]

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with psycopg.connect(DSN) as connection:
                connection.execute("SET ROLE rusa_agent_api")
                connection.execute(
                    "SELECT set_config('rusa.tenant_id', %s, true)", (tenant_a,),
                )
                connection.execute("""
                    INSERT INTO rusa_agent_tasks
                        (tenant_id, task_id, idempotency_key, payload)
                    VALUES (%s, %s, %s, '{}'::jsonb)
                """, (tenant_b, f"forbidden-{uuid4().hex}", f"forbidden-{uuid4().hex}"))
    finally:
        with psycopg.connect(DSN) as connection:
            connection.execute(
                "DELETE FROM rusa_agent_tasks WHERE tenant_id IN (%s, %s)",
                (tenant_a, tenant_b),
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
        connection.execute("GRANT SELECT, INSERT ON rusa_agent_memory TO rusa_agent_test_rls")
        connection.execute(
            "GRANT USAGE, SELECT ON SEQUENCE rusa_agent_memory_memory_id_seq TO rusa_agent_test_rls"
        )
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

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(DSN) as connection:
            connection.execute("SET ROLE rusa_agent_test_rls")
            connection.execute(
                "SELECT set_config('rusa.tenant_id', %s, true)",
                (tenant_a,),
            )
            connection.execute(
                """
                INSERT INTO rusa_agent_memory
                    (tenant_id, namespace, content, source)
                VALUES (%s, 'episodic', %s, 'rls-test')
                """,
                (tenant_b, f"forbidden memory {marker}"),
            )

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
    first_token = first["lease_token"]
    assert queue.heartbeat(
        tenant_id, task_id, "worker-one", first_token, lease_seconds=60,
    )
    assert queue.owns_lease(tenant_id, task_id, "worker-one", first_token) is True
    assert queue.owns_lease(tenant_id, task_id, "worker-two", first_token) is False

    with psycopg.connect(DSN) as connection:
        connection.execute("""
            UPDATE rusa_agent_tasks
            SET lease_until=now() - interval '1 second'
            WHERE tenant_id=%s AND task_id=%s
        """, (tenant_id, task_id))

    # Reclaim with the same worker ID to prove that the lease token, not
    # the owner string, fences the stale execution.
    second = queue.claim("worker-one", lease_seconds=60)
    assert second is not None and second["task_id"] == task_id
    assert second["lease_owner"] == "worker-one"
    second_token = second["lease_token"]
    assert second_token != first_token
    assert not queue.heartbeat(
        tenant_id, task_id, "worker-one", first_token, lease_seconds=60,
    )
    assert queue.owns_lease(tenant_id, task_id, "worker-one", first_token) is False
    assert queue.owns_lease(tenant_id, task_id, "worker-one", second_token) is True
    stale_record = TaskRecord(task=task)
    stale_record.transition(TaskState.OBSERVING)
    assert not queue.checkpoint(
        tenant_id, task_id, "worker-one", first_token, stale_record,
    )
    with pytest.raises(RuntimeError, match="lease was lost"):
        queue.acknowledge(
            tenant_id,
            task_id,
            stale_record,
            worker_id="worker-one",
            lease_token=first_token,
        )
    assert queue.retry(
        tenant_id,
        task_id,
        "stale worker",
        worker_id="worker-one",
        lease_token=first_token,
    ) is False
    assert queue.checkpoint(
        tenant_id, task_id, "worker-one", second_token, stale_record,
    )
    queue.acknowledge(
        tenant_id,
        task_id,
        stale_record,
        worker_id="worker-one",
        lease_token=second_token,
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
    queue.configure_api_permissions()
    assert queue.check_ready() is True
    assert memory.check_ready() is True


def test_postgres_queue_readiness_requires_lease_token_column():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    PostgresMemoryStore(DSN).initialize()
    queue.configure_api_permissions()
    with psycopg.connect(DSN) as connection:
        connection.execute("ALTER TABLE rusa_agent_tasks DROP COLUMN lease_token")
    try:
        assert queue.check_ready() is False
    finally:
        with psycopg.connect(DSN) as connection:
            connection.execute("ALTER TABLE rusa_agent_tasks ADD COLUMN lease_token TEXT")
    assert queue.check_ready() is True


def test_postgres_memory_redacts_secrets_before_persistence():
    memory = PostgresMemoryStore(DSN)
    memory.initialize()
    tenant_id = f"rusa-test-memory-redact-{uuid4().hex}"
    memory.store(
        tenant_id,
        'configuration api_key = "raw-memory-secret-value-123456"',
        source="redaction-integration-test",
    )
    results = memory.search(tenant_id, "redacted")
    assert results
    assert "raw-memory-secret-value-123456" not in results[0]["content"]
    assert "[REDACTED]" in results[0]["content"]
    memory.delete_tenant(tenant_id)



def test_postgres_queue_cancel_revokes_active_lease_and_rejects_stale_worker():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    clean_test_rows()
    queue.configure_api_permissions()
    tenant_id = f"rusa-test-cancel-{uuid4().hex}"
    task = make_task(tenant_id, f"cancel-{uuid4().hex}")
    task_id = queue.enqueue(task)

    claimed = queue.claim("cancel-worker", lease_seconds=60)
    assert claimed is not None and claimed["task_id"] == task_id
    token = claimed["lease_token"]
    record = TaskRecord(task=task)
    record.transition(TaskState.OBSERVING)

    assert queue.cancel(tenant_id, task_id) is True
    assert queue.get(tenant_id, task_id)["status"] == "cancelled"
    assert queue.owns_lease(tenant_id, task_id, "cancel-worker", token) is False
    assert queue.heartbeat(
        tenant_id, task_id, "cancel-worker", token, lease_seconds=60
    ) is False
    assert queue.checkpoint(
        tenant_id, task_id, "cancel-worker", token, record
    ) is False
    with pytest.raises(RuntimeError, match="lease was lost"):
        queue.acknowledge(
            tenant_id, task_id, record, "cancel-worker", token
        )

    with psycopg.connect(DSN) as connection:
        connection.execute(
            "DELETE FROM rusa_agent_tasks WHERE tenant_id=%s",
            (tenant_id,),
        )


def test_postgres_queue_requeues_only_failed_tasks_for_the_same_tenant():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    clean_test_rows()
    queue.configure_api_permissions()
    tenant_id = f"rusa-test-requeue-{uuid4().hex}"
    task = make_task(tenant_id, f"requeue-{uuid4().hex}")
    task_id = queue.enqueue(task)

    with psycopg.connect(DSN) as connection:
        connection.execute(
            """
            UPDATE rusa_agent_tasks
            SET status='failed', attempts=2, last_error='transient failure',
                result='{"state":"failed"}'::jsonb
            WHERE tenant_id=%s AND task_id=%s
            """,
            (tenant_id, task_id),
        )

    assert queue.requeue_failed("wrong-tenant", task_id) is False
    assert queue.requeue_failed(tenant_id, task_id) is True
    restored = queue.get(tenant_id, task_id)
    assert restored is not None
    assert restored["status"] == "queued"
    assert restored["attempts"] == 0
    assert restored["result"] is None
    assert restored["last_error"] is None

    with psycopg.connect(DSN) as connection:
        connection.execute(
            "DELETE FROM rusa_agent_tasks WHERE tenant_id=%s",
            (tenant_id,),
        )


def test_postgres_queue_can_cancel_task_waiting_for_human_approval():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    clean_test_rows()
    queue.configure_api_permissions()
    tenant_id = f"rusa-test-approval-cancel-{uuid4().hex}"
    task = make_task(tenant_id, f"approval-cancel-{uuid4().hex}")
    task_id = queue.enqueue(task)

    claimed = queue.claim("approval-cancel-worker", lease_seconds=60)
    assert claimed is not None and claimed["task_id"] == task_id
    record = TaskRecord(task=task)
    record.transition(TaskState.OBSERVING)
    record.transition(TaskState.AWAITING_APPROVAL)
    queue.acknowledge(
        tenant_id,
        task_id,
        record,
        worker_id="approval-cancel-worker",
        lease_token=claimed["lease_token"],
    )

    assert queue.get(tenant_id, task_id)["status"] == "awaiting_approval"
    assert queue.cancel(tenant_id, task_id) is True
    assert queue.get(tenant_id, task_id)["status"] == "cancelled"

    with psycopg.connect(DSN) as connection:
        connection.execute(
            "DELETE FROM rusa_agent_tasks WHERE tenant_id=%s",
            (tenant_id,),
        )


def test_workspace_task_queries_are_scoped_by_tenant_and_user():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    clean_test_rows()
    tenant_id = f"rusa-test-workspace-{uuid4().hex}"
    user_id = str(uuid4())
    other_user_id = str(uuid4())
    own_task = replace(
        make_task(tenant_id, f"workspace-own-{uuid4().hex}"),
        metadata={"user_id": user_id},
    )
    other_task = replace(
        make_task(tenant_id, f"workspace-other-{uuid4().hex}"),
        metadata={"user_id": other_user_id},
    )
    own_id = queue.enqueue(own_task)
    other_id = queue.enqueue(other_task)

    own_rows = queue.list_tasks(tenant_id, user_id=user_id, limit=10)
    tenant_rows = queue.list_tasks(tenant_id, limit=10)
    own_stats = queue.stats(tenant_id, user_id=user_id)
    record = queue.get(tenant_id, own_id)

    assert [item["task_id"] for item in own_rows] == [own_id]
    assert {item["task_id"] for item in tenant_rows} == {own_id, other_id}
    assert own_stats["counts"]["queued"] == 1
    assert record is not None
    assert record["payload"]["metadata"]["user_id"] == user_id
    assert queue.list_dead_letters(tenant_id, user_id=user_id) == []

    with psycopg.connect(DSN) as connection:
        connection.execute("DELETE FROM rusa_agent_tasks WHERE tenant_id=%s", (tenant_id,))
