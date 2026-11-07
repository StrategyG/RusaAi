"""Shared test import setup and PostgreSQL migration bootstrap."""
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVE_PATH = ROOT / "llm-project" / "serve"
if str(SERVE_PATH) not in sys.path:
    sys.path.insert(0, str(SERVE_PATH))


@pytest.fixture(autouse=True)
def explicit_development_environment(monkeypatch):
    """Tests opt into development mode instead of relying on runtime defaults."""
    monkeypatch.setenv("ENVIRONMENT", "development")


@pytest.fixture(scope="session", autouse=True)
def apply_postgres_migrations_for_integration_suite():
    """Apply the complete ordered schema before any PostgreSQL integration test."""
    dsn = os.getenv("RUSA_DATABASE_URL")
    if not dsn:
        return

    import psycopg

    migration_dir = ROOT / "migrations"
    migrations = sorted(
        path for path in migration_dir.glob("*.sql")
        if re.match(r"^\d{3}_[A-Za-z0-9_-]+\.sql$", path.name)
        and int(path.name.split("_", 1)[0]) <= 6
    )
    expected = [
        "001_aicode_core.sql",
        "002_aicode_lease_fencing.sql",
        "003_aicode_task_results.sql",
        "004_aicode_tenant_rls.sql",
        "005_aicode_task_policy_audit.sql",
        "006_aicode_idempotency.sql",
    ]
    if [path.name for path in migrations] != expected:
        raise RuntimeError("PostgreSQL integration migration set is incomplete or out of order")

    with psycopg.connect(dsn, connect_timeout=5, autocommit=True) as connection:
        for migration in migrations:
            connection.execute(
                migration.read_text(encoding="utf-8"),
                prepare=False,
            )
