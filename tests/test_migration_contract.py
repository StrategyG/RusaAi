import re
from pathlib import Path


def test_database_migrations_have_unique_monotonic_versions():
    paths = sorted(Path("migrations").glob("*.sql"), key=lambda path: path.name)
    versions = [int(path.name.split("_", 1)[0]) for path in paths]

    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9]


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


def test_shared_postgres_fixture_applies_migrations_once_in_version_order():
    fixture = Path("tests/conftest.py").read_text(encoding="utf-8")
    migrations = re.findall(r'"(\d{3}_[A-Za-z0-9_-]+\.sql)"', fixture)

    assert migrations == [
        "001_aicode_core.sql",
        "002_aicode_lease_fencing.sql",
        "003_aicode_task_results.sql",
        "004_aicode_tenant_rls.sql",
        "005_aicode_task_policy_audit.sql",
        "006_aicode_idempotency.sql",
        "008_aicode_task_list_audit.sql",
        "009_aicode_explicit_approval.sql",
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



def test_explicit_approval_migration_extends_append_only_audit_actions():
    migration = Path("migrations/009_aicode_explicit_approval.sql").read_text(encoding="utf-8")
    assert "task.approve" in migration
    assert "task.reject" in migration
    assert "DROP CONSTRAINT IF EXISTS aicode_task_policy_events_action_check" in migration
    assert "ADD CONSTRAINT aicode_task_policy_events_action_check" in migration



def test_production_compose_cpu_budget_fits_four_core_host():
    compose = Path("docker-compose.production.yml").read_text(encoding="utf-8")
    cpu_limits = [
        float(value)
        for value in re.findall(r"(?m)^\s+cpus:\s*([0-9]+(?:\.[0-9]+)?)\s*$", compose)
    ]
    assert cpu_limits == [1.0, 0.5, 1.5]
    sandbox = re.search(
        r"(?m)^\s+RUSA_SANDBOX_CPUS:\s*\$\{RUSA_SANDBOX_CPUS:-([0-9.]+)\}",
        compose,
    )
    assert sandbox is not None
    assert sum(cpu_limits) + float(sandbox.group(1)) <= 4.0
