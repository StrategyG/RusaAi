from pathlib import Path


def test_knowledge_migration_enforces_vector_and_tenant_rls():
    migration = Path("migrations/007_rag_knowledge_pgvector.sql").read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS vector" in migration
    assert "vector(1536)" in migration
    assert "USING (tenant_id = current_setting('rusa.tenant_id', true))" in migration
    assert "WITH CHECK (tenant_id = current_setting('rusa.tenant_id', true))" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "rusa_knowledge_worker" in migration


def test_384d_migration_preserves_legacy_vectors_and_adds_parallel_index():
    migration = Path("migrations/009_knowledge_embedding_384.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS embedding_384 vector(384)" in migration
    assert "ALTER COLUMN embedding DROP NOT NULL" in migration
    assert "embedding IS NOT NULL OR embedding_384 IS NOT NULL" in migration
    assert "rusa_knowledge_chunks_embedding_384_hnsw_idx" in migration
    assert "DROP COLUMN" not in migration
    assert "UPDATE public.rusa_knowledge_chunks" not in migration


def test_agent_project_scope_migration_adds_lookup_indexes():
    migration = Path("migrations/011_rag_agent_project_scope.sql").read_text(encoding="utf-8")
    assert "rusa_knowledge_documents_scope_idx" in migration
    assert "rusa_knowledge_documents_agent_ids_gin" in migration
    assert "rusa_knowledge_documents_project_scope_idx" in migration
    assert "tenant_id" in migration
    assert "DROP " not in migration


def test_document_version_history_migration_is_append_only_and_dimension_aware():
    migration = Path("migrations/012_rag_knowledge_version_history.sql").read_text(
        encoding="utf-8"
    )
    assert "rusa_knowledge_document_versions" in migration
    assert "rusa_knowledge_version_chunks" in migration
    assert "version_fingerprint" in migration
    assert "version_number" in migration
    assert "embedding vector(1536)" in migration
    assert "embedding_384 vector(384)" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "GRANT SELECT, INSERT" in migration
    assert "GRANT UPDATE" not in migration
    assert "GRANT DELETE" not in migration
    assert "REVOKE DELETE ON public.rusa_knowledge_documents" in migration
    assert "current_version_id" in migration


def test_governance_migration_separates_reviewer_approver_and_publisher():
    migration = Path("migrations/013_rag_knowledge_governance.sql").read_text(
        encoding="utf-8"
    )
    for table in (
        "rusa_knowledge_rights_attestations",
        "rusa_knowledge_review_findings",
        "rusa_knowledge_human_approvals",
        "rusa_knowledge_publication_events",
        "rusa_knowledge_audit_events",
    ):
        assert f"CREATE TABLE IF NOT EXISTS public.{table}" in migration
        assert f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY" in migration
        assert f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY" in migration
    assert "rusa_knowledge_reviewer" in migration
    assert "rusa_knowledge_approver" in migration
    assert "rusa_knowledge_publisher" in migration
    assert "session_user" in migration
    assert "rusa_publish_knowledge_version" in migration
    assert "rusa_withdraw_knowledge_version" in migration
    assert "rusa_knowledge_version_is_published" in migration
    assert "rusa_knowledge_version_attribution" in migration
    assert "GRANT EXECUTE ON FUNCTION public.rusa_knowledge_version_attribution" in migration
    assert "rusa_knowledge_governance_audit_insert" in migration
    assert "rusa_knowledge_governance_set_actor" in migration
    assert "rusa_knowledge_assert_governance_principal" in migration
    assert "WITH RECURSIVE inherited_roles" in migration
    assert "governance LOGIN must inherit only its assigned role" in migration
    assert "NOT r.rolcreaterole" in migration
    assert "NOT r.rolcreatedb" in migration
    assert "NOT r.rolreplication" in migration
    assert "m.roleid = v_expected_role_oid" in migration
    assert "NEW.approver_principal := session_user" in migration
    assert "MAX(v_latest.version_number)" in migration
    assert "pg_advisory_xact_lock" in migration
    assert "NEW.created_at := clock_timestamp()" in migration
    assert "ORDER BY created_at DESC, publication_event_id DESC" in migration
    assert "v_approval_created_at <= v_latest_preapproval_evidence_at" in migration
    assert "publication failed its post-insert governance eligibility check" in migration
    assert "v_rights.attested_at >= v_publication_created_at" in migration
    assert migration.count("CREATE OR REPLACE FUNCTION public.rusa_publish_knowledge_version") == 1
    assert migration.count("CREATE OR REPLACE FUNCTION public.rusa_withdraw_knowledge_version") == 1
    assert migration.count("CREATE OR REPLACE FUNCTION public.rusa_knowledge_version_is_published") == 1
    assert migration.count("COMMIT;") == 1
    assert migration.count("DROP TRIGGER IF EXISTS rusa_knowledge_") == 8
    assert "manifest_sha256 IS NOT NULL" in migration
    assert "SECURITY DEFINER" in migration
    assert "GRANT UPDATE" not in migration
    assert "GRANT DELETE" not in migration
