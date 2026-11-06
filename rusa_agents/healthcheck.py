"""Container healthcheck for required PostgreSQL schemas and tenant RLS."""

import os
import sys

from .memory import PostgresMemoryStore
from .queue import PostgresTaskQueue


def main() -> int:
    dsn = os.getenv("RUSA_AGENT_DATABASE_URL", "")
    if not dsn:
        return 1
    try:
        return 0 if (
            PostgresTaskQueue(dsn).check_ready()
            and PostgresMemoryStore(dsn).check_ready()
        ) else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
