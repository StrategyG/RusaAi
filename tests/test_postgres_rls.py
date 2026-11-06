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
    with connect() as conn:
        for migration_name in (
            "001_aicode_core.sql",
            "002_aicode_lease_fencing.sql",
            "003_aicode_task_results.sql",
            "004_aicode_tenant_rls.sql",
        ):
            conn.execute((migration_root / migration_name).read_text(encoding="utf-8"))


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
