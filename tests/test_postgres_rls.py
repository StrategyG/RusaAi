import asyncio
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

DSN = os.getenv("RUSA_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="PostgreSQL integration DSN is not configured")


def connect():
    import psycopg
    return psycopg.connect(DSN)


@pytest.fixture(scope="module", autouse=True)
def apply_database_migrations():
    if not DSN:
        pytest.skip("PostgreSQL integration DSN is not configured")
    migration_root = Path(__file__).resolve().parents[1] / "migrations"
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        for migration_name in (
            "001_aicode_core.sql",
            "002_aicode_lease_fencing.sql",
            "003_aicode_task_results.sql",
            "004_aicode_tenant_rls.sql",
            "005_aicode_task_policy_audit.sql",
            "006_aicode_idempotency.sql",
            "014_aicode_memory_checkpoint_cas.sql",
        ):
            conn.execute(
                (migration_root / migration_name).read_text(encoding="utf-8"),
                prepare=False,
            )


def set_role_and_tenant(conn, role, tenant):
    conn.execute(f"SET ROLE {role}")
    conn.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant,))


def test_api_rls_limits_queue_rows_to_current_tenant():
    import psycopg

    task_a, task_b = str(uuid4()), str(uuid4())
    with connect() as conn:
        set_role_and_tenant(conn, "rusa_api_runtime", "rls-tenant-a")
        conn.execute(
            "INSERT INTO aicode_jobs(task_id, tenant_id, payload) VALUES (%s, %s, %s::jsonb)",
            (task_a, "rls-tenant-a", '{"objective":"visible"}'),
        )
        rows = conn.execute("SELECT tenant_id FROM aicode_jobs").fetchall()
        assert rows and {row[0] for row in rows} == {"rls-tenant-a"}

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO aicode_jobs(task_id, tenant_id, payload) VALUES (%s, %s, %s::jsonb)",
                    (task_b, "rls-tenant-b", '{"objective":"must be denied"}'),
                )


def test_api_role_cannot_read_memory_or_audit():
    import psycopg

    with connect() as conn:
        set_role_and_tenant(conn, "rusa_api_runtime", "rls-tenant-a")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute("SELECT * FROM aicode_memory")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute("SELECT * FROM aicode_audit_events")


def test_worker_memory_is_tenant_scoped_and_audit_is_append_only():
    import psycopg

    task_id = str(uuid4())
    with connect() as conn:
        set_role_and_tenant(conn, "rusa_worker_runtime", "rls-worker-a")
        conn.execute(
            """INSERT INTO aicode_memory(tenant_id, task_id, repository, state, payload)
               VALUES (%s, %s, %s, %s, %s::jsonb)""",
            ("rls-worker-a", task_id, "StrategyG/RusaAi", "testing", '{"ok":true}'),
        )
        assert conn.execute("SELECT tenant_id FROM aicode_memory").fetchall() == [("rls-worker-a",)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(
                    """INSERT INTO aicode_memory(tenant_id, task_id, repository, state, payload)
                       VALUES (%s, %s, %s, %s, %s::jsonb)""",
                    ("rls-worker-b", str(uuid4()), "StrategyG/RusaAi", "testing", '{"ok":true}'),
                )

        conn.execute(
            """INSERT INTO aicode_audit_events
               (event, task_id, tenant_id, actor, details, previous_hash, record_hash)
               VALUES ('test.rls', %s, %s, 'pytest', '{}'::jsonb, 'GENESIS', %s)""",
            (task_id, "rls-worker-a", "rls-test-" + task_id),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(
                    "UPDATE aicode_audit_events SET event='tampered' WHERE task_id=%s",
                    (task_id,),
                )


def test_worker_queue_role_can_claim_across_tenants_but_api_cannot():
    import psycopg

    task_a, task_b = str(uuid4()), str(uuid4())
    with connect() as conn:
        # Seed as the migration/test owner; the worker role intentionally has no INSERT grant.
        conn.execute(
            "INSERT INTO aicode_jobs(task_id, tenant_id, payload) VALUES (%s, %s, %s::jsonb)",
            (task_a, "rls-worker-a", '{"objective":"a"}'),
        )
        conn.execute(
            "INSERT INTO aicode_jobs(task_id, tenant_id, payload) VALUES (%s, %s, %s::jsonb)",
            (task_b, "rls-worker-b", '{"objective":"b"}'),
        )
        set_role_and_tenant(conn, "rusa_worker_runtime", "rls-worker-a")
        visible = {row[0] for row in conn.execute("SELECT tenant_id FROM aicode_jobs").fetchall()}
        assert {"rls-worker-a", "rls-worker-b"}.issubset(visible)

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO aicode_jobs(task_id, tenant_id, payload) VALUES (%s, %s, %s::jsonb)",
                    (str(uuid4()), "rls-worker-a", '{"objective":"worker cannot enqueue"}'),
                )



def test_runtime_role_groups_are_non_login_non_bypass():
    with connect() as conn:
        rows = conn.execute("""
            SELECT rolname, rolcanlogin, rolsuper, rolbypassrls
            FROM pg_roles
            WHERE rolname = ANY(%s)
        """, (["rusa_api_runtime", "rusa_worker_runtime"],)).fetchall()

    assert {row[0] for row in rows} == {"rusa_api_runtime", "rusa_worker_runtime"}
    assert all(row[1] is False and row[2] is False and row[3] is False for row in rows)


def test_task_api_enforces_tenant_policy_under_runtime_roles(monkeypatch, caplog):
    import psycopg
    from app import app
    from fastapi.testclient import TestClient
    from psycopg import sql

    from rusa_agents.audit import PostgresTaskPolicyAudit
    from rusa_agents.queue import PostgresTaskQueue

    api_password = "test-api-" + uuid4().hex
    worker_password = "test-worker-" + uuid4().hex
    with connect() as admin:
        for role_name, group_name, password in (
            ("rusa_api_login", "rusa_api_runtime", api_password),
            ("rusa_worker_login", "rusa_worker_runtime", worker_password),
        ):
            admin.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role_name))
            )
            admin.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOBYPASSRLS "
                    "NOCREATEDB NOCREATEROLE PASSWORD {}"
                ).format(sql.Identifier(role_name), sql.Literal(password))
            )
            admin.execute(
                sql.SQL(
                    "GRANT {} TO {} WITH ADMIN FALSE, INHERIT TRUE, SET TRUE"
                ).format(sql.Identifier(group_name), sql.Identifier(role_name))
            )

    api_dsn = psycopg.conninfo.make_conninfo(
        DSN, user="rusa_api_login", password=api_password
    )
    worker_dsn = psycopg.conninfo.make_conninfo(
        DSN, user="rusa_worker_login", password=worker_password
    )
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("RUSA_AICODE_ENABLED", "true")
    key_a = "tenant-a-integration-key-000000000000000000000000"
    key_b = "tenant-b-integration-key-000000000000000000000000"
    registry = {
        hashlib.sha256(key_a.encode()).hexdigest(): {
            "subject": "service-a", "tenant_id": "audit-a", "roles": ["tenant_admin"],
        },
        hashlib.sha256(key_b.encode()).hexdigest(): {
            "subject": "service-b", "tenant_id": "audit-b", "roles": ["tenant_admin"],
        },
    }
    monkeypatch.setenv("RUSA_API_KEYS_JSON", json.dumps(registry))
    monkeypatch.delenv("RUSA_API_KEYS", raising=False)
    monkeypatch.delenv("RUSA_API_KEY", raising=False)
    api_queue = PostgresTaskQueue(api_dsn)
    monkeypatch.setattr(app.state, "queue", api_queue, raising=False)
    api_audit = PostgresTaskPolicyAudit(api_dsn)
    worker_audit = PostgresTaskPolicyAudit(worker_dsn)
    api_audit.verify_ready("rusa_api_runtime")
    worker_audit.verify_ready("rusa_worker_runtime")
    monkeypatch.setattr(app.state, "task_policy_audit", api_audit, raising=False)
    caplog.set_level("INFO", logger="rusa-ai.task-policy")

    # A task insert must roll back if its matching audit event cannot be committed.
    failed_task_id = str(uuid4())
    with connect() as admin:
        admin.execute(
            "REVOKE INSERT ON public.aicode_task_policy_events FROM rusa_api_runtime"
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        api_queue.enqueue(
            failed_task_id,
            "audit-b",
            {"objective": "atomic audit rollback"},
            audit_actor="api:service-b",
        )
    with connect() as admin:
        admin.execute(
            "GRANT INSERT ON public.aicode_task_policy_events TO rusa_api_runtime"
        )
        assert admin.execute(
            "SELECT count(*) FROM aicode_jobs WHERE task_id=%s", (failed_task_id,)
        ).fetchone()[0] == 0

    client = TestClient(app)
    api_a = {"X-API-Key": key_a, "X-Tenant-ID": "audit-b"}
    api_b = {"X-API-Key": key_b}
    response = client.post("/v1/aicode/tasks", headers=api_b, json={
        "objective": "sensitive objective sentinel",
        "repository": {"full_name": "StrategyG/RusaAi"},
        "acceptance_criteria": ["The test passes"],
    })
    assert response.status_code == 202
    rejected_tenant_override = client.post("/v1/aicode/tasks", headers=api_a, json={
        "objective": "Tenant identity must come from credentials",
        "repository": {"full_name": "StrategyG/RusaAi"},
        "acceptance_criteria": ["The request tenant must be rejected"],
        "tenant_id": "audit-b",
    })
    assert rejected_tenant_override.status_code == 422
    task_id = response.json()["task_id"]
    path = f"/v1/aicode/tasks/{task_id}"
    assert client.get(path).status_code == 401

    with psycopg.connect(api_dsn) as conn:
        assert conn.execute(
            """SELECT current_user, rolsuper, rolbypassrls,
                      row_security_active('aicode_jobs'),
                      pg_has_role(current_user, 'rusa_api_runtime', 'member')
               FROM pg_roles WHERE rolname=current_user"""
        ).fetchone() == ("rusa_api_login", False, False, True, True)
        conn.execute("SELECT set_config('rusa.tenant_id', 'audit-a', true)")
        assert conn.execute("SELECT 1 FROM aicode_jobs WHERE task_id=%s", (task_id,)).fetchone() is None
        assert conn.execute(
            "UPDATE aicode_jobs SET status='cancelled' WHERE task_id=%s", (task_id,),
        ).rowcount == 0

    # Ensure a cross-tenant retry would mutate an eligible row without isolation.
    with connect() as conn:
        conn.execute(
            "UPDATE aicode_jobs SET status='failed', priority=2147483647 WHERE task_id=%s", (task_id,),
        )
    assert client.get(path, headers=api_a).status_code == 404
    assert client.post(f"{path}/retry", headers=api_a).status_code == 409
    assert client.get(path, headers=api_b).json()["status"] == "failed"
    assert client.post(f"{path}/retry", headers=api_b).status_code == 202

    with psycopg.connect(worker_dsn) as conn:
        assert conn.execute(
            """SELECT current_user, rolsuper, rolbypassrls,
                      row_security_active('aicode_jobs'),
                      pg_has_role(current_user, 'rusa_worker_runtime', 'member')
               FROM pg_roles WHERE rolname=current_user"""
        ).fetchone() == ("rusa_worker_login", False, False, True, True)

    worker = PostgresTaskQueue(worker_dsn)
    job = worker.claim("policy-test-worker")
    assert job is not None and job.task_id == task_id and job.tenant_id == "audit-b"
    assert not worker.owns_lease(job.id, "audit-a", "policy-test-worker", job.lease_token)
    assert not worker.complete(job.id, "audit-a", "policy-test-worker", job.lease_token, {})
    assert client.delete(path, headers=api_a).status_code == 404
    assert client.get(path, headers=api_b).json()["status"] == "running"
    assert not worker.authorize_execution(
        job.id, task_id, "audit-b", "policy-test-worker", str(uuid4())
    )
    assert worker.authorize_execution(
        job.id, task_id, "audit-b", "policy-test-worker", job.lease_token
    )
    assert client.delete(path, headers=api_b).status_code == 202

    events = [json.loads(record.getMessage()) for record in caplog.records
              if record.name == "rusa-ai.task-policy"]
    assert {(event["tenant_id"], event["actor"], event["action"], event["decision"])
            for event in events} == {
        (None, "unauthenticated", "task.authenticate", "deny"),
        ("audit-a", "api:service-a", "task.read", "deny"),
        ("audit-a", "api:service-a", "task.retry", "deny"),
        ("audit-a", "api:service-a", "task.cancel", "deny"),
        ("audit-b", "api:service-b", "task.create", "allow"),
        ("audit-b", "api:service-b", "task.read", "allow"),
        ("audit-b", "api:service-b", "task.retry", "allow"),
        ("audit-b", "api:service-b", "task.cancel", "allow"),
    }
    assert all(event["task_id"] == task_id and event["event"] == "task.policy" for event in events)
    assert "sensitive objective sentinel" not in caplog.text
    assert key_a not in caplog.text and key_b not in caplog.text

    with psycopg.connect(worker_dsn) as conn:
        conn.execute("SELECT set_config('rusa.tenant_id', %s, true)", ("audit-b",))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(
                    """INSERT INTO aicode_task_policy_events
                       (tenant_id, task_id, actor, action, decision)
                       VALUES (%s, %s, %s, %s, %s)""",
                    ("audit-a", task_id, "worker:policy-test-worker", "task.execute", "deny"),
                )

    with psycopg.connect(api_dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute("SELECT * FROM aicode_task_policy_events")
        conn.execute("SELECT set_config('rusa.tenant_id', %s, true)", ("audit-a",))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(
                    """INSERT INTO aicode_task_policy_events
                       (tenant_id, task_id, actor, action, decision)
                       VALUES (%s, %s, %s, %s, %s)""",
                    ("audit-b", task_id, "api:service-a", "task.read", "deny"),
                )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(
                    "UPDATE aicode_task_policy_events SET decision='allow' WHERE task_id=%s",
                    (task_id,),
                )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute("TRUNCATE aicode_task_policy_events")

    with connect() as conn:
        persisted = conn.execute(
            """SELECT tenant_id, task_id, actor, action, decision
               FROM aicode_task_policy_events WHERE task_id=%s""",
            (task_id,),
        ).fetchall()
    persisted_decisions = {
        (row[0], row[2], row[3], row[4]) for row in persisted
    }
    assert {
        (None, "unauthenticated", "task.authenticate", "deny"),
        ("audit-a", "api:service-a", "task.read", "deny"),
        ("audit-a", "api:service-a", "task.retry", "deny"),
        ("audit-a", "api:service-a", "task.cancel", "deny"),
        ("audit-b", "api:service-b", "task.create", "allow"),
        ("audit-b", "api:service-b", "task.read", "allow"),
        ("audit-b", "api:service-b", "task.retry", "allow"),
        ("audit-b", "api:service-b", "task.cancel", "allow"),
        ("audit-b", "worker:policy-test-worker", "task.execute", "allow"),
        ("audit-b", "worker:policy-test-worker", "task.execute", "deny"),
    }.issubset(persisted_decisions)


def test_worker_memory_checkpoint_compare_and_set_is_atomic_and_tenant_scoped():
    from rusa_agents.memory import PostgresMemory

    memory = PostgresMemory(DSN)
    task_id = str(uuid4())
    tenant_id = f"checkpoint-cas-{uuid4().hex}"
    payloads = [
        {"tenant_id": tenant_id, "plan_fingerprint": "writer-a"},
        {"tenant_id": tenant_id, "plan_fingerprint": "writer-b"},
    ]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(
                lambda payload: memory.compare_and_set(
                    task_id,
                    "agent-runtime",
                    "agent_runtime_checkpoint",
                    payload,
                    tenant_id,
                    0,
                ),
                payloads,
            ))
        assert sorted(value for value in outcomes if value is not None) == [1]
        assert sum(value is not None for value in outcomes) == 1

        current = memory.get(task_id, tenant_id)
        assert current is not None
        assert current["checkpoint_version"] == 1
        assert current["payload"]["plan_fingerprint"] in {"writer-a", "writer-b"}
        assert memory.get(task_id, tenant_id + "-other") is None
        assert memory.compare_and_set(
            task_id,
            "agent-runtime",
            "agent_runtime_checkpoint",
            {"tenant_id": tenant_id, "plan_fingerprint": "stale"},
            tenant_id,
            0,
        ) is None
        assert memory.compare_and_set(
            task_id,
            "agent-runtime",
            "agent_runtime_checkpoint",
            {"tenant_id": tenant_id, "plan_fingerprint": "writer-next"},
            tenant_id,
            1,
        ) == 2
    finally:
        with connect() as conn:
            conn.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))
            conn.execute(
                "DELETE FROM aicode_memory WHERE task_id=%s AND tenant_id=%s",
                (task_id, tenant_id),
            )


@pytest.mark.asyncio
async def test_postgres_queue_to_dag_execution_uses_live_lease_and_checkpoint_cas():
    from rusa_agents.capability_registry import AgentWorkerRegistry
    from rusa_agents.memory import PostgresMemory
    from rusa_agents.queue import PostgresTaskQueue
    from rusa_agents.runtime import AICodeRuntime
    from rusa_agents.runtime_v1 import AgentRuntimeV1
    from rusa_agents.task_decomposer import TaskNode, TaskPlan
    from rusa_agents.worker_execution import (
        PolicyDecision,
        PlanStatus,
        VerificationDecision,
        WorkerOutput,
    )

    registry_dir = Path(__file__).resolve().parents[1] / "rusa_agents" / "data"
    registry_data = json.loads((registry_dir / "agent_worker_registry.v1.json").read_text())
    registry_schema = json.loads((registry_dir / "agent_worker_registry.v1.schema.json").read_text())
    registry_data["status"] = "active"
    registry_data["source_chunk_map_status"] = "approved_for_routing"
    for index, role in enumerate(registry_data["roles"], start=900):
        role["registration_status"] = "registered"
        role["runtime_status"] = "verified"
        role["activation_state"] = "active"
        role["source_chunk_ids"] = [f"RUSA-KB-{index:03d}"]
    registry_data["counts"]["source_chunk_refs_total"] = len(registry_data["roles"])
    registry = AgentWorkerRegistry.from_mapping(registry_data, registry_schema)

    queue = PostgresTaskQueue(DSN)
    memory = PostgresMemory(DSN)
    queue.initialize()
    memory.initialize()
    tenant_id = f"dag-e2e-{uuid4().hex}"
    task_id = str(uuid4())
    worker_id = f"dag-e2e-worker-{uuid4().hex[:8]}"
    job = None

    class Handler:
        async def execute(self, node, dependency_results):
            return WorkerOutput(
                summary="PostgreSQL queue-to-DAG acceptance passed",
                evidence_refs=("evidence://postgres-queue-dag",),
            )

    def authorize(node, trusted_tenant):
        return PolicyDecision(allowed=True)

    def verify(node, output):
        return VerificationDecision(accepted=bool(output.evidence_refs))

    runtime = AICodeRuntime.__new__(AICodeRuntime)
    runtime.agent_runtime = AgentRuntimeV1(registry)
    runtime.memory = memory
    runtime.queue = queue
    runtime.worker_id = worker_id

    try:
        assert queue.enqueue(
            task_id,
            tenant_id,
            {"objective": "PostgreSQL queue-to-DAG acceptance"},
            priority=2**31 - 1,
        )
        job = queue.claim(worker_id, lease_seconds=60)
        assert job is not None and job.task_id == task_id

        plan = TaskPlan(
            root_task_id=task_id,
            tenant_id=tenant_id,
            objective="Verify queue lease, DAG execution, and atomic checkpoint persistence",
            nodes=(
                TaskNode(
                    task_id="node-postgres-e2e",
                    selected_role_id="analyst",
                    scope="worker",
                    objective="Run a bounded integration acceptance task",
                    delegated_by_role_id="supreme_orchestrator",
                    acceptance_criteria=("Persist versioned checkpoint",),
                ),
            ),
        )
        result = await runtime.execute_agent_plan(
            job,
            plan,
            {"analyst": Handler()},
            authorize,
            verify,
            asyncio.Event(),
        )

        assert result.status == PlanStatus.COMPLETED
        record = memory.get(task_id, tenant_id)
        assert record is not None
        assert record["state"] == "agent_runtime_checkpoint"
        assert record["checkpoint_version"] >= 2
        assert queue.complete(
            job.id,
            tenant_id,
            worker_id,
            job.lease_token,
            {"plan_status": result.status.value},
        )
        assert queue.get(task_id, tenant_id)["status"] == "succeeded"
    finally:
        with connect() as conn:
            conn.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))
            conn.execute(
                "DELETE FROM aicode_memory WHERE task_id=%s AND tenant_id=%s",
                (task_id, tenant_id),
            )
            conn.execute(
                "DELETE FROM aicode_jobs WHERE task_id=%s AND tenant_id=%s",
                (task_id, tenant_id),
            )
