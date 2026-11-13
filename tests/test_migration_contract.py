import re
from pathlib import Path


def test_database_migrations_have_unique_monotonic_versions():
    paths = sorted(Path("migrations").glob("*.sql"), key=lambda path: path.name)
    versions = [int(path.name.split("_", 1)[0]) for path in paths]

    # Gaps are allowed while independent draft PRs reserve migration numbers.
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert versions and versions[0] == 1


def test_queue_readiness_policy_names_match_the_canonical_rls_migration():
    queue = Path("rusa_agents/queue.py").read_text(encoding="utf-8")
    migration = Path("migrations/004_aicode_tenant_rls.sql").read_text(encoding="utf-8")
    audit_migration = Path("migrations/005_aicode_task_policy_audit.sql").read_text(encoding="utf-8")
    required = {
        "aicode_jobs_api_tenant",
        "aicode_jobs_worker_queue",
        "aicode_rate_limits_api_tenant",
        "aicode_memory_worker_tenant",
        "aicode_audit_worker_only",
    }

    assert all(f'"{name}"' in queue for name in required)
    assert all(f"CREATE POLICY {name}" in migration for name in required)
    assert "ENABLE ROW LEVEL SECURITY" in audit_migration
    assert "FORCE ROW LEVEL SECURITY" in audit_migration
    assert "aicode_task_policy_events_api_insert" in audit_migration
    assert "aicode_task_policy_events_worker_insert" in audit_migration
    assert "BEFORE UPDATE OR DELETE" in audit_migration
    assert "BEFORE TRUNCATE" in audit_migration


def test_rls_integration_fixture_applies_migrations_once_in_version_order():
    test_source = Path("tests/test_postgres_rls.py").read_text(encoding="utf-8")
    migrations = re.findall(r'"(\d{3}_[A-Za-z0-9_-]+\.sql)"', test_source)

    assert migrations == [
        "001_aicode_core.sql",
        "002_aicode_lease_fencing.sql",
        "003_aicode_task_results.sql",
        "004_aicode_tenant_rls.sql",
        "005_aicode_task_policy_audit.sql",
        "006_aicode_idempotency.sql",
        "014_aicode_memory_checkpoint_cas.sql",
    ]


def test_sql_migration_dollar_quote_delimiters_are_balanced():
    for migration in sorted(Path("migrations").glob("*.sql")):
        sql = migration.read_text(encoding="utf-8")
        assert sql.count("$$") % 2 == 0, (
            f"{migration.name} contains an unmatched dollar-quote delimiter"
        )
        assert not re.search(r"(?m)^\\s*\\$\\s*;", sql), (
            f"{migration.name} contains a malformed single-dollar terminator"
        )



def test_inference_admission_migration_is_api_runtime_scoped():
    migration = Path("migrations/010_inference_admission.sql").read_text(encoding="utf-8")

    assert "rusa_inference_admission_config" in migration
    assert "rusa_inference_admission_requests" in migration
    assert "rusa_inference_admission_slots" in migration
    assert "queue_expires_at" in migration
    assert "lease_expires_at" in migration
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" in migration
    assert "TO rusa_api_runtime" in migration
    assert "rusa_worker_runtime" in migration
    assert "No prompt or completion data" in migration or "no tenant payload" in migration



def test_agent_runtime_checkpoint_cas_migration_adds_monotonic_version():
    migration = Path("migrations/014_aicode_memory_checkpoint_cas.sql").read_text(
        encoding="utf-8"
    )

    assert "ADD COLUMN IF NOT EXISTS checkpoint_version BIGINT NOT NULL DEFAULT 0" in migration
    assert "CHECK (checkpoint_version >= 0)" in migration
