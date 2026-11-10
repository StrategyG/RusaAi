import re
from pathlib import Path


def test_database_migrations_have_unique_monotonic_versions():
    paths = sorted(Path("migrations").glob("*.sql"), key=lambda path: path.name)
    versions = [int(path.name.split("_", 1)[0]) for path in paths]

    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert versions == list(range(1, 15))


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
        "008_aicode_task_list_audit.sql",
        "010_platform_workflow_core.sql",
        "011_platform_governance_registry.sql",
        "012_devops_durable_communication.sql",
        "013_devops_task_leases_and_cancellation.sql",
        "014_devops_closed_loop_governance.sql",
    ]


def test_sql_migration_dollar_quote_delimiters_are_balanced():
    for migration in sorted(Path("migrations").glob("*.sql")):
        sql = migration.read_text(encoding="utf-8")
        assert sql.count("$$") % 2 == 0, (
            f"{migration.name} contains an unmatched dollar-quote delimiter"
        )
        assert not re.search(r"(?m)^\s*\$\s*;", sql), (
            f"{migration.name} contains a malformed single-dollar terminator"
        )


def test_platform_workflow_migration_has_tenant_scoped_cas_outbox_and_dedup_contracts():
    migration = Path("migrations/010_platform_workflow_core.sql").read_text(encoding="utf-8")
    assert "rusa_workflow_runs" in migration
    assert "rusa_workflow_tasks" in migration
    assert "rusa_workflow_dependencies" in migration
    assert "rusa_workflow_events" in migration
    assert "rusa_workflow_outbox" in migration
    assert "rusa_workflow_inbox" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    scheduler = Path("rusa_agents/workflow_store.py").read_text(encoding="utf-8")
    assert "FOR UPDATE OF r SKIP LOCKED" in scheduler
    assert "final_artifact_digest" in migration
    assert "side_effect_status" in migration
    assert "RAISE EXCEPTION '% is append-only'" in migration
    assert "idx_rusa_workflow_events_run_sequence" in migration
    assert "idx_rusa_workflow_events_task_sequence" in migration
    assert migration.count("BEGIN;") == 1
    assert migration.count("COMMIT;") == 1
    assert migration.count("CREATE TABLE IF NOT EXISTS public.rusa_workflow_runs") == 1


def test_governance_migration_has_registry_approval_consumption_and_immutable_evidence():
    migration = Path("migrations/011_platform_governance_registry.sql").read_text(encoding="utf-8")
    assert "rusa_agent_registry" in migration
    assert "rusa_agent_registry_events" in migration
    assert "rusa_policy_decisions" in migration
    assert "rusa_human_approvals" in migration
    assert "rusa_approval_consumptions" in migration
    assert "rusa_evidence_records" in migration
    assert "human approval binding is immutable" in migration
    assert "rusa_platform_append_only" in migration



def test_closed_loop_migration_has_tenant_scoped_incidents_and_kill_switches():
    migration = Path("migrations/014_devops_closed_loop_governance.sql").read_text(encoding="utf-8")
    for table in (
        "rusa_incidents",
        "rusa_incident_events",
        "rusa_closed_loop_inbox",
        "rusa_kill_switches",
        "rusa_kill_switch_events",
    ):
        assert f"CREATE TABLE IF NOT EXISTS public.{table}" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "tenant_id = 'platform'" in migration
    assert "REVOKE DELETE, TRUNCATE" in migration
    assert "UNIQUE (tenant_id, fingerprint)" in migration
    assert "UNIQUE (tenant_id, message_id)" in migration
