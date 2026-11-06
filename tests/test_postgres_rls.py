import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from rusa_agents.audit import PostgresAuditLog

DSN = os.getenv("RUSA_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="PostgreSQL integration DSN is not configured")


def connect():
    import psycopg
    return psycopg.connect(DSN)


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

        previous_row = conn.execute(
            "SELECT record_hash FROM aicode_audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        previous_hash = previous_row[0] if previous_row else "GENESIS"
        timestamp = datetime.now(timezone.utc).isoformat()
        audit = PostgresAuditLog(DSN, os.environ["RUSA_AUDIT_HMAC_KEY"])
        audit_row = {
            "timestamp": timestamp,
            "event": "test.rls",
            "task_id": task_id,
            "tenant_id": "rls-worker-a",
            "actor": "pytest",
            "details": {},
            "previous_hash": previous_hash,
        }
        conn.execute(
            """INSERT INTO aicode_audit_events
               (timestamp, event, task_id, tenant_id, actor, details, previous_hash, record_hash)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)""",
            (
                timestamp, "test.rls", task_id, "rls-worker-a", "pytest", "{}",
                previous_hash, audit._digest(audit_row),
            ),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(
                    "UPDATE aicode_audit_events SET event='tampered' WHERE task_id=%s",
                    (task_id,),
                )
    assert audit.verify()


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



def test_production_queue_readiness_uses_separate_login_roles(monkeypatch):
    from rusa_agents.queue import PostgresTaskQueue

    api_dsn = os.getenv("RUSA_API_DATABASE_URL")
    worker_dsn = os.getenv("RUSA_WORKER_DATABASE_URL")
    if not api_dsn or not worker_dsn:
        pytest.skip("Separate API/worker database login DSNs are not configured")

    monkeypatch.setenv("ENVIRONMENT", "production")
    PostgresTaskQueue(api_dsn).initialize()
    PostgresTaskQueue(worker_dsn).initialize()


def test_runtime_role_groups_are_non_login_non_bypass():
    with connect() as conn:
        rows = conn.execute("""
            SELECT rolname, rolcanlogin, rolsuper, rolbypassrls
            FROM pg_roles
            WHERE rolname = ANY(%s)
        """, (["rusa_api_runtime", "rusa_worker_runtime"],)).fetchall()

    assert {row[0] for row in rows} == {"rusa_api_runtime", "rusa_worker_runtime"}
    assert all(row[1] is False and row[2] is False and row[3] is False for row in rows)


def test_queue_rejects_privilege_group_as_login_identity():
    from rusa_agents.queue import PostgresTaskQueue

    with connect() as conn:
        conn.execute("SET LOCAL ROLE rusa_api_runtime")
        with pytest.raises(RuntimeError, match="non-superuser LOGIN role"):
            PostgresTaskQueue._validate_production_role(conn)
