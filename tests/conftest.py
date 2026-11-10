"""Shared test import setup and PostgreSQL migration bootstrap."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
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
_PROTO_TEMP_DIR = None

if importlib.util.find_spec("grpc_tools") is not None and not all(
    path.is_file() for path in GENERATED_STUBS
):
    # Tests also run in a read-only Docker sandbox. Generate into a writable
    # temporary package path instead of mutating the checked-out source tree.
    _PROTO_TEMP_DIR = tempfile.TemporaryDirectory(prefix="rusa-devops-proto-")
    temporary_package_root = Path(_PROTO_TEMP_DIR.name) / "rusa_agents" / "devops"
    output_dir = temporary_package_root / "generated"
    environment = os.environ.copy()
    environment["RUSA_DEVOPS_PROTO_OUTPUT_DIR"] = str(output_dir)
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_devops_proto.py")],
        cwd=ROOT,
        env=environment,
        check=True,
    )
    import rusa_agents.devops as devops_package

    devops_package.__path__.append(str(temporary_package_root))


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
