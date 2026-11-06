"""PostgreSQL integration tests for the tenant-facing API database role."""

import os
from uuid import uuid4

import psycopg
import pytest

from rusa_agents.memory import PostgresMemoryStore
from rusa_agents.queue import PostgresTaskQueue


DSN = os.getenv("RUSA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="RUSA_TEST_DATABASE_URL is not configured"
)


def test_api_role_has_only_minimum_queue_permissions_and_no_future_defaults():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    PostgresMemoryStore(DSN).initialize()

    with psycopg.connect(DSN) as connection:
        connection.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='rusa_agent_api') THEN
                    CREATE ROLE rusa_agent_api NOLOGIN NOSUPERUSER NOBYPASSRLS;
                END IF;
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='rusa_agent_worker') THEN
                    CREATE ROLE rusa_agent_worker NOLOGIN NOSUPERUSER BYPASSRLS;
                END IF;
            END
            $$
            """
        )
        connection.execute(
            "ALTER ROLE rusa_agent_api NOLOGIN NOSUPERUSER NOBYPASSRLS"
        )
        connection.execute(
            "GRANT USAGE ON SCHEMA public TO rusa_agent_api"
        )
        connection.execute(
            "GRANT USAGE, CREATE ON SCHEMA public TO rusa_agent_worker"
        )
        # Recreate the broad legacy defaults to prove migration removes them.
        connection.execute(
            """
            ALTER DEFAULT PRIVILEGES FOR ROLE rusa_agent_worker IN SCHEMA public
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO rusa_agent_api
            """
        )
        connection.execute(
            """
            ALTER DEFAULT PRIVILEGES FOR ROLE rusa_agent_worker IN SCHEMA public
            GRANT USAGE, SELECT ON SEQUENCES TO rusa_agent_api
            """
        )

    queue.configure_api_permissions()

    # Worker startup calls initialize() again; it must preserve the migration grants.
    queue.initialize()

    tenant_id = f"rusa-test-api-role-{uuid4().hex}"
    task_id = uuid4().hex
    idempotency_key = uuid4().hex
    probe_table = f"rusa_api_permission_probe_{uuid4().hex}"
    probe_sequence = f"{probe_table}_seq"

    try:
        with psycopg.connect(DSN) as connection:
            privileges = connection.execute(
                """
                SELECT
                    has_table_privilege(
                        'rusa_agent_api', 'public.rusa_agent_tasks', 'SELECT'
                    ),
                    has_table_privilege(
                        'rusa_agent_api', 'public.rusa_agent_tasks', 'INSERT'
                    ),
                    has_table_privilege(
                        'rusa_agent_api', 'public.rusa_agent_tasks', 'UPDATE'
                    ),
                    has_table_privilege(
                        'rusa_agent_api', 'public.rusa_agent_tasks', 'DELETE'
                    ),
                    has_table_privilege(
                        'rusa_agent_api', 'public.rusa_agent_memory', 'SELECT'
                    )
                """
            ).fetchone()
            assert privileges == (True, True, True, False, False)

            connection.execute("SET ROLE rusa_agent_worker")
            connection.execute(
                f'CREATE TABLE public."{probe_table}" (id INTEGER)'
            )
            connection.execute(
                f'CREATE SEQUENCE public."{probe_sequence}"'
            )
            connection.execute("RESET ROLE")

            future_table_access = connection.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT')",
                ("rusa_agent_api", f"public.{probe_table}"),
            ).fetchone()[0]
            future_sequence_access = connection.execute(
                "SELECT has_sequence_privilege(%s, %s, 'USAGE')",
                ("rusa_agent_api", f"public.{probe_sequence}"),
            ).fetchone()[0]
            assert future_table_access is False
            assert future_sequence_access is False

        with psycopg.connect(DSN) as connection:
            connection.execute("SET ROLE rusa_agent_api")
            connection.execute(
                "SELECT set_config('rusa.tenant_id', %s, true)",
                (tenant_id,),
            )
            connection.execute(
                """
                INSERT INTO rusa_agent_tasks
                    (tenant_id, task_id, idempotency_key, payload)
                VALUES (%s, %s, %s, '{}'::jsonb)
                """,
                (tenant_id, task_id, idempotency_key),
            )
            row = connection.execute(
                """
                SELECT status FROM rusa_agent_tasks
                WHERE tenant_id=%s AND task_id=%s
                """,
                (tenant_id, task_id),
            ).fetchone()
            assert row == ("queued",)
            updated = connection.execute(
                """
                UPDATE rusa_agent_tasks
                SET status='cancelled'
                WHERE tenant_id=%s AND task_id=%s
                """,
                (tenant_id, task_id),
            )
            assert updated.rowcount == 1

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with psycopg.connect(DSN) as connection:
                connection.execute("SET ROLE rusa_agent_api")
                connection.execute(
                    "SELECT set_config('rusa.tenant_id', %s, true)",
                    (tenant_id,),
                )
                connection.execute(
                    "DELETE FROM rusa_agent_tasks WHERE tenant_id=%s AND task_id=%s",
                    (tenant_id, task_id),
                )

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with psycopg.connect(DSN) as connection:
                connection.execute("SET ROLE rusa_agent_api")
                connection.execute(
                    "SELECT set_config('rusa.tenant_id', %s, true)",
                    (tenant_id,),
                )
                connection.execute("SELECT * FROM rusa_agent_memory LIMIT 1")
    finally:
        with psycopg.connect(DSN) as connection:
            connection.execute(
                "DELETE FROM rusa_agent_tasks WHERE tenant_id=%s",
                (tenant_id,),
            )
            connection.execute(
                f'DROP TABLE IF EXISTS public."{probe_table}"'
            )
            connection.execute(
                f'DROP SEQUENCE IF EXISTS public."{probe_sequence}"'
            )
