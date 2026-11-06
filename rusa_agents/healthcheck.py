"""Container healthcheck for the agent worker's database schema."""

import os
import sys

import psycopg


def main() -> int:
    dsn = os.getenv("RUSA_AGENT_DATABASE_URL", "")
    if not dsn:
        return 1
    try:
        with psycopg.connect(dsn, connect_timeout=3) as connection:
            tasks = connection.execute(
                "SELECT to_regclass('public.rusa_agent_tasks')"
            ).fetchone()
            memory = connection.execute(
                "SELECT to_regclass('public.rusa_agent_memory')"
            ).fetchone()
        return 0 if tasks and tasks[0] and memory and memory[0] else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
