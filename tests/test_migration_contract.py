import re
from pathlib import Path


def test_database_migrations_have_unique_monotonic_versions():
    paths = sorted(Path("migrations").glob("*.sql"), key=lambda path: path.name)
    versions = [int(path.name.split("_", 1)[0]) for path in paths]

    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert versions == [1, 2, 3, 4]


def test_queue_readiness_policy_names_match_the_canonical_rls_migration():
    queue = Path("rusa_agents/queue.py").read_text(encoding="utf-8")
    migration = Path("migrations/004_aicode_tenant_rls.sql").read_text(encoding="utf-8")
    required = {
        "aicode_jobs_api_tenant",
        "aicode_jobs_worker_queue",
        "aicode_rate_limits_api_tenant",
        "aicode_memory_worker_tenant",
        "aicode_audit_worker_only",
    }

    assert all(f'"{name}"' in queue for name in required)
    assert all(f"CREATE POLICY {name}" in migration for name in required)


def test_ci_applies_all_database_migrations_once_in_version_order():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    migrations = re.findall(r"psql .*?-f migrations/(\d{3}_[A-Za-z0-9_-]+\.sql)", workflow)

    assert migrations == [
        "001_aicode_core.sql",
        "002_aicode_lease_fencing.sql",
        "003_aicode_task_results.sql",
        "004_aicode_tenant_rls.sql",
    ]

def test_ci_runs_required_checks_for_any_pull_request_base_branch():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    pull_request_trigger = workflow.split("  pull_request:\n", 1)[1].split(
        "\n  workflow_dispatch:", 1
    )[0]
    assert "branches:" not in pull_request_trigger
