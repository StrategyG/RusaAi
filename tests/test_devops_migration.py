from pathlib import Path


def test_devops_migration_has_rls_and_separate_queue():
    migration = Path("migrations/009_rusa_devops_intelligence.sql").read_text(encoding="utf-8")
    assert "rusa_devops_jobs" in migration
    assert "rusa_devops_telemetry" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "rusa_devops_jobs_worker_queue" in migration
    assert "rusa_devops_audit_immutable" in migration
