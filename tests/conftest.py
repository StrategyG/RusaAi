"""Shared test import setup and PostgreSQL migration bootstrap."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVE_PATH = ROOT / "llm-project" / "serve"
if str(SERVE_PATH) not in sys.path:
    sys.path.insert(0, str(SERVE_PATH))

MIGRATION_NAMES = (
    "001_aicode_core.sql",
    "002_aicode_lease_fencing.sql",
    "003_aicode_task_results.sql",
    "004_aicode_tenant_rls.sql",
)


@pytest.fixture(scope="session", autouse=True)
def apply_postgres_migrations_before_integration_tests():
    """Create the canonical schema before any module exercises runtime roles."""
    dsn = os.getenv("RUSA_DATABASE_URL")
    if not dsn:
        return

    import psycopg

    migration_dir = ROOT / "migrations"
    with psycopg.connect(dsn, connect_timeout=5, autocommit=True) as connection:
        for migration_name in MIGRATION_NAMES:
            connection.execute(
                (migration_dir / migration_name).read_text(encoding="utf-8"),
                prepare=False,
            )
