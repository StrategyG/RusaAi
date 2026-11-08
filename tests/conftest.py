"""Shared test import setup and PostgreSQL migration bootstrap."""
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVE_PATH = ROOT / "llm-project" / "serve"
if str(SERVE_PATH) not in sys.path:
    sys.path.insert(0, str(SERVE_PATH))


GENERATED_DIR = ROOT / "rusa_agents" / "devops" / "generated"
GENERATED_STUBS = (
    GENERATED_DIR / "workflow_pb2.py",
    GENERATED_DIR / "workflow_pb2_grpc.py",
)

if importlib.util.find_spec("grpc_tools") is not None and not all(
    path.is_file() for path in GENERATED_STUBS
):
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_devops_proto.py")],
        cwd=ROOT,
        check=True,
    )


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
        and (
            int(path.name.split("_", 1)[0]) <= 6
            or path.name in {
                "008_devops_durable_communication.sql",
                "009_devops_task_leases_and_cancellation.sql",
            }
        )
    )
    expected = [
        "001_aicode_core.sql",
        "002_aicode_lease_fencing.sql",
        "003_aicode_task_results.sql",
        "004_aicode_tenant_rls.sql",
        "005_aicode_task_policy_audit.sql",
        "006_aicode_idempotency.sql",
        "008_devops_durable_communication.sql",
        "009_devops_task_leases_and_cancellation.sql",
    ]
    if [path.name for path in migrations] != expected:
        raise RuntimeError("PostgreSQL integration migration set is incomplete or out of order")

    with psycopg.connect(dsn, connect_timeout=5, autocommit=True) as connection:
        for migration in migrations:
            connection.execute(
                migration.read_text(encoding="utf-8"),
                prepare=False,
            )
