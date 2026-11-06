"""Container healthcheck for the agent worker's database schema."""

import logging
import os
import sys

import psycopg

logger = logging.getLogger("rusa-agent-healthcheck")


def _tenant_rls_ready(connection, table_name: str, policy_name: str) -> bool:
    row = connection.execute(
        """
        SELECT c.relrowsecurity, c.relforcerowsecurity,
               EXISTS (
                   SELECT 1 FROM pg_policies
                   WHERE schemaname='public'
                     AND tablename=%s
                     AND policyname=%s
                     AND COALESCE(qual, '') LIKE '%rusa.tenant_id%'
                     AND COALESCE(with_check, '') LIKE '%rusa.tenant_id%'
               )
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname=%s
        """,
        (table_name, policy_name, table_name),
    ).fetchone()
    return bool(row and row[0] and row[1] and row[2])


def main() -> int:
    dsn = os.getenv("RUSA_AGENT_DATABASE_URL", "")
    if not dsn:
        logger.error("RUSA_AGENT_DATABASE_URL is not configured")
        return 1
    try:
        with psycopg.connect(dsn, connect_timeout=3) as connection:
            tasks_ready = _tenant_rls_ready(
                connection, "rusa_agent_tasks", "rusa_agent_tenant_isolation"
            )
            memory_ready = _tenant_rls_ready(
                connection, "rusa_agent_memory", "rusa_memory_tenant_isolation"
            )
        if not tasks_ready:
            logger.error("Task table is missing forced tenant RLS or its tenant policy")
        if not memory_ready:
            logger.error("Memory table is missing forced tenant RLS or its tenant policy")
        return 0 if tasks_ready and memory_ready else 1
    except Exception as exc:
        logger.error("Worker database health check failed: %s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
