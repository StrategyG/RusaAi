from pathlib import Path
import re


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


def test_rls_migrations_are_bootstrapped_before_any_test_module():
    source = Path("tests/conftest.py").read_text(encoding="utf-8")
    migrations = re.findall(r'"(\\d{3}_[A-Za-z0-9_-]+\\.sql)"', source)

    assert migrations == [
        "001_aicode_core.sql",
        "002_aicode_lease_fencing.sql",
        "003_aicode_task_results.sql",
        "004_aicode_tenant_rls.sql",
    ]
    assert '@pytest.fixture(scope="session", autouse=True)' in source


def test_role_provisioning_do_blocks_have_valid_dollar_terminators():
    for name in ("001_aicode_core.sql", "004_aicode_tenant_rls.sql"):
        source = (Path("migrations") / name).read_text(encoding="utf-8")
        assert re.search(r"DO \\$\\$\\s*BEGIN[\\s\\S]*?\\nEND;\\s*\\$\\$;", source)
