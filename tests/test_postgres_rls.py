import hashlib
import json
import os
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
            # 007 is the optional pgvector migration; this PostgreSQL service image
            # does not bundle the vector extension. Platform Core is tested separately.
            "008_aicode_task_list_audit.sql",
            "009_platform_workflow_core.sql",
            "010_platform_governance_registry.sql",
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


def test_platform_core_migrations_enable_forced_rls_and_atomic_event_outbox():
    from datetime import datetime, timedelta, timezone

    import psycopg

    from rusa_agents.platform_contracts import WorkflowEventV2
    from rusa_agents.workflow_store import PostgresWorkflowStore

    PostgresWorkflowStore(DSN).initialize()

    now = datetime.now(timezone.utc)
    tenant_a = "platform-core-" + uuid4().hex
    tenant_b = "platform-core-" + uuid4().hex
    workflow_id = "workflow-" + uuid4().hex
    run_a = "run-" + uuid4().hex
    run_b = "run-" + uuid4().hex

    with connect() as admin:
        for tenant, run_id in ((tenant_a, run_a), (tenant_b, run_b)):
            admin.execute(
                """
                INSERT INTO public.rusa_workflow_runs(
                    tenant_id, workflow_id, run_id, domain, objective,
                    state, deadline_at, budget, created_by
                )
                VALUES (%s, %s, %s, 'software_engineering', 'RLS integration test',
                        'running', %s, '{}'::jsonb, 'pytest')
                """,
                (tenant, workflow_id + "-" + tenant, run_id, now + timedelta(minutes=10)),
            )

    event = WorkflowEventV2(
        tenant_id=tenant_a,
        workflow_id=workflow_id + "-" + tenant_a,
        run_id=run_a,
        correlation_id=workflow_id,
        traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        event_type="workflow.created",
        producer_id="rusa.test",
        producer_version="1.0.0",
        sequence=1,
        occurred_at=now,
        payload={"test": "tenant-scoped event and outbox atomicity"},
    )
    with connect() as conn:
        set_role_and_tenant(conn, "rusa_api_runtime", tenant_a)
        assert PostgresWorkflowStore._append_event(conn, event) is True
        assert conn.execute(
            "SELECT count(*) FROM public.rusa_workflow_outbox WHERE tenant_id=%s",
            (tenant_a,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM public.rusa_workflow_runs WHERE tenant_id=%s",
            (tenant_a,),
        ).fetchone()[0] == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO public.rusa_workflow_runs(
                        tenant_id, workflow_id, run_id, domain, objective,
                        state, deadline_at, budget, created_by
                    )
                    VALUES (%s, %s, %s, 'software_engineering', 'cross-tenant write',
                            'queued', %s, '{}'::jsonb, 'pytest')
                    """,
                    (tenant_b, "cross-" + uuid4().hex, "cross-" + uuid4().hex,
                     now + timedelta(minutes=10)),
                )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(
                    "UPDATE public.rusa_workflow_events SET event_type='tampered' WHERE message_id=%s",
                    (event.message_id,),
                )

    with connect() as conn:
        set_role_and_tenant(conn, "rusa_api_runtime", tenant_b)
        assert conn.execute(
            "SELECT 1 FROM public.rusa_workflow_runs WHERE run_id=%s",
            (run_a,),
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM public.rusa_workflow_events WHERE message_id=%s",
            (event.message_id,),
        ).fetchone() is None

    # The trusted scheduler role may see work across tenants; event reads remain scoped.
    with connect() as conn:
        set_role_and_tenant(conn, "rusa_worker_runtime", tenant_a)
        visible = {
            row[0] for row in conn.execute(
                "SELECT tenant_id FROM public.rusa_workflow_runs WHERE run_id = ANY(%s)",
                ([run_a, run_b],),
            ).fetchall()
        }
        assert visible == {tenant_a, tenant_b}
        assert conn.execute(
            "SELECT 1 FROM public.rusa_workflow_events WHERE message_id=%s",
            (event.message_id,),
        ).fetchone() is not None

    # Table owners still cannot rewrite immutable event facts.
    with connect() as admin:
        with pytest.raises(psycopg.errors.RaiseException):
            with admin.transaction():
                admin.execute(
                    "UPDATE public.rusa_workflow_events SET event_type='tampered' WHERE message_id=%s",
                    (event.message_id,),
                )


def test_platform_registry_policy_approval_and_evidence_services_are_enforced():
    from datetime import datetime, timedelta, timezone

    from rusa_agents.approvals import (
        ApprovalError,
        PostgresHumanApprovalService,
    )
    from rusa_agents.evidence import EvidenceValidationError, PostgresEvidenceStore
    from rusa_agents.platform_contracts import (
        AgentDomain,
        AgentHealthSnapshot,
        AgentManifestV1,
        AgentStatus,
        EvidenceReference,
        RuntimeBinding,
        RuntimeKind,
        ValidationVerdictPayloadV2,
    )
    from rusa_agents.policy import RiskLevel
    from rusa_agents.policy_engine import (
        DeterministicPolicyEngine,
        PolicyDecisionService,
        PolicyRequest,
        PostgresPolicyDecisionStore,
    )
    from rusa_agents.registry import AgentRegistryError, PostgresAgentRegistry
    from rusa_agents.workflow_graph import (
        WorkflowGraph,
        WorkflowRunSpec,
        WorkflowTaskSpec,
    )
    from rusa_agents.workflow_store import PostgresWorkflowStore

    now = datetime.now(timezone.utc)
    tenant_id = "platform-services-" + uuid4().hex
    workflow_id = "workflow-" + uuid4().hex
    run_id = "run-" + uuid4().hex
    task_id = "task-" + uuid4().hex
    artifact_digest = "sha256:" + "a" * 64
    artifact_commit = "b" * 40

    run = WorkflowRunSpec(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        run_id=run_id,
        domain="software_engineering",
        objective="Exercise platform governance persistence",
        deadline_at=now + timedelta(hours=1),
        created_by="test:owner",
        budget={"max_parallel_tasks": 1},
    )
    task = WorkflowTaskSpec(
        task_id=task_id,
        domain="software_engineering",
        agent_id="rusa_test_agent",
        agent_version="1.0.0",
        capability="repo.read",
        deadline_at=now + timedelta(minutes=30),
    )
    workflow_store = PostgresWorkflowStore(DSN)
    workflow_store.create_workflow(run, WorkflowGraph(run, (task,)))
    workflow_store.start_run(tenant_id, run_id)

    manifest = AgentManifestV1(
        agent_id="rusa_test_agent",
        domain=AgentDomain.SOFTWARE_ENGINEERING,
        version="1.0.0",
        status=AgentStatus.DRAFT,
        capabilities=["repo.read"],
        input_schema_ref="schema://rusa/test/input.v1",
        output_schema_ref="schema://rusa/test/output.v1",
        runtime_binding=RuntimeBinding(
            kind=RuntimeKind.PYTHON_CALLABLE,
            target="tests.fake_agents:ReadAgent",
        ),
        tool_allowlist=["list_files"],
        tenant_scope={"mode": "tenant"},
        resource_scope={"include_patterns": ["workspace://*"]},
        policy_profile="test-policy",
        evaluation_profile="test-evaluation",
        health=AgentHealthSnapshot(status="healthy", checked_at=now),
        owner="platform-integration-tests",
        created_at=now,
        updated_at=now,
    )
    registry = PostgresAgentRegistry(
        DSN,
        runtime_resolver=lambda *_: True,
        schema_resolver=lambda *_: True,
        policy_resolver=lambda *_: True,
    )
    registry.register(tenant_id, manifest, actor="test:admin")
    validated = registry.validate(
        tenant_id, manifest.agent_id, manifest.version, actor="test:admin"
    )
    assert validated.status == AgentStatus.VALIDATED
    active = registry.activate(tenant_id, manifest.agent_id, manifest.version, actor="test:admin")
    assert active.status == AgentStatus.ACTIVE
    runnable = registry.assert_runnable(
        tenant_id, manifest.agent_id, manifest.version, "repo.read"
    )
    assert runnable.status == AgentStatus.ACTIVE
    assert registry.select(tenant_id, "repo.read").agent_id == manifest.agent_id
    with pytest.raises(AgentRegistryError, match="different immutable content"):
        registry.register(
            tenant_id,
            manifest.model_copy(update={"tool_allowlist": ["list_files", "write_file"]}),
            actor="test:admin",
        )

    engine = DeterministicPolicyEngine(
        policy_id="test-policy",
        policy_version="1",
        allowed_actions={"tool.invoke:list_files"},
    )
    policy_service = PolicyDecisionService(engine, PostgresPolicyDecisionStore(DSN))
    allowed = policy_service.evaluate(PolicyRequest(
        tenant_id=tenant_id,
        authenticated_tenant_id=tenant_id,
        actor=manifest.agent_id,
        action="tool.invoke:list_files",
        resource_ref="workspace://src",
        policy_id="test-policy",
        policy_version="1",
        risk=RiskLevel.LOW,
        workflow_id=workflow_id,
        run_id=run_id,
        task_id=task_id,
    ))
    assert allowed.outcome == "allow"
    denial_request = PolicyRequest(
        tenant_id=tenant_id,
        authenticated_tenant_id=tenant_id,
        actor=manifest.agent_id,
        action="tool.invoke:write_file",
        resource_ref="workspace://src/main.py",
        policy_id="test-policy",
        policy_version="1",
        workflow_id=workflow_id,
        run_id=run_id,
        task_id=task_id,
    )
    first_denial = policy_service.evaluate(denial_request)
    second_denial = policy_service.evaluate(denial_request)
    assert first_denial.outcome == "deny"
    assert second_denial.decision_id == first_denial.decision_id

    approvals = PostgresHumanApprovalService(
        DSN,
        approver_authorizer=lambda actor, _tenant, _action, _resource: actor == "human:approver",
        consumer_authorizer=lambda actor, _tenant, _action, _resource: actor == "agent:executor",
    )
    requested = approvals.request(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        run_id=run_id,
        task_id=task_id,
        requested_by="agent:requester",
        action="release.promote",
        resource_ref="artifact://release/candidate",
        reason="Promote the exact QA-verified candidate",
        policy_id="release-policy",
        policy_version="3",
        expires_at=now + timedelta(minutes=10),
        artifact_digest=artifact_digest,
    )
    with pytest.raises(PermissionError, match="cannot approve their own"):
        approvals.decide(
            tenant_id, requested["approval_id"],
            actor="agent:requester", decision="approved",
        )
    assert approvals.decide(
        tenant_id, requested["approval_id"],
        actor="human:approver", decision="approved",
    )["status"] == "approved"
    consume_args = {
        "actor": "agent:executor",
        "action": "release.promote",
        "resource_ref": "artifact://release/candidate",
        "workflow_id": workflow_id,
        "run_id": run_id,
        "task_id": task_id,
        "artifact_digest": artifact_digest,
    }
    assert approvals.consume(tenant_id, requested["approval_id"], **consume_args) is True
    assert approvals.consume(tenant_id, requested["approval_id"], **consume_args) is False
    with pytest.raises(ApprovalError, match="different action"):
        approvals.consume(
            tenant_id, requested["approval_id"],
            **{**consume_args, "action": "release.deploy"},
        )

    evidence_store = PostgresEvidenceStore(DSN)
    evidence_ref = EvidenceReference(
        uri="artifact://evidence/" + uuid4().hex,
        sha256="sha256:" + "c" * 64,
        kind="test_report",
        producer_id="rusa_test_agent",
        producer_version="1.0.0",
        subject_digest=artifact_digest,
        subject_commit=artifact_commit,
        test_profile="ci-full",
        occurred_at=now,
    )
    evidence_id = evidence_store.record(
        tenant_id,
        evidence_ref,
        subject_commit=artifact_commit,
        test_profile="ci-full",
        metadata={"workflow_id": workflow_id, "source": "pytest"},
    )
    verdict = ValidationVerdictPayloadV2(
        verdict="passed",
        subject_digest=artifact_digest,
        commit_sha=artifact_commit,
        producer_id="rusa_qa_agent",
        producer_version="1.0.0",
        test_profile="ci-full",
        evidence_refs=[evidence_ref],
        evaluated_at=now,
        summary="All required CI checks passed for the exact artifact.",
    )
    assert evidence_store.verify_verdict(
        tenant_id, verdict,
        expected_digest=artifact_digest,
        expected_commit=artifact_commit,
    ) == [evidence_id]
    wrong_kind_ref = evidence_ref.model_copy(update={"kind": "build_log"})
    wrong_kind_verdict = verdict.model_copy(update={"evidence_refs": [wrong_kind_ref]})
    with pytest.raises(EvidenceValidationError, match="provenance"):
        evidence_store.verify_verdict(
            tenant_id, wrong_kind_verdict,
            expected_digest=artifact_digest,
            expected_commit=artifact_commit,
        )
    with pytest.raises(EvidenceValidationError, match="different provenance"):
        evidence_store.record(
            tenant_id,
            evidence_ref,
            subject_commit=artifact_commit,
            test_profile="ci-full",
            metadata={"workflow_id": workflow_id, "source": "tampered"},
        )
    assert evidence_store.verify_and_publish_verdict(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        run_id=run_id,
        task_id=task_id,
        attempt=1,
        verdict=verdict,
        expected_digest=artifact_digest,
        expected_commit=artifact_commit,
    ) == [evidence_id]

    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM public.rusa_agent_registry_events WHERE tenant_id=%s",
            (tenant_id,),
        ).fetchone()[0] == 3
        assert conn.execute(
            "SELECT count(*) FROM public.rusa_policy_decisions WHERE tenant_id=%s",
            (tenant_id,),
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT count(*) FROM public.rusa_approval_consumptions WHERE tenant_id=%s",
            (tenant_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            """
            SELECT count(*) FROM public.rusa_workflow_events
            WHERE tenant_id=%s AND event_type='validation.verdict'
            """,
            (tenant_id,),
        ).fetchone()[0] == 1

    # The API role sees only the authenticated tenant's governance records.
    with connect() as conn:
        set_role_and_tenant(conn, "rusa_api_runtime", tenant_id)
        for table in (
            "rusa_agent_registry",
            "rusa_agent_registry_events",
            "rusa_policy_decisions",
            "rusa_human_approvals",
            "rusa_approval_consumptions",
            "rusa_evidence_records",
        ):
            assert conn.execute(
                f"SELECT count(*) FROM public.{table}"
            ).fetchone()[0] > 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(
                    "UPDATE public.rusa_policy_decisions SET reason_code='tampered'"
                )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(
                    "UPDATE public.rusa_evidence_records SET kind='tampered'"
                )

    # Workers may read tenant-scoped approval/evidence data, but cannot mutate registry or approvals.
    with connect() as conn:
        set_role_and_tenant(conn, "rusa_worker_runtime", tenant_id)
        assert conn.execute(
            "SELECT count(*) FROM public.rusa_human_approvals"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM public.rusa_evidence_records"
        ).fetchone()[0] == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(
                    "UPDATE public.rusa_human_approvals SET status='rejected'"
                )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(
                    "UPDATE public.rusa_agent_registry SET status='revoked'"
                )
