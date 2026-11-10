-- Platform Core 2.0 agent registry, policy decisions, approvals, and evidence.
-- Apply after 009_platform_workflow_core.sql.
BEGIN;

CREATE TABLE IF NOT EXISTS public.rusa_agent_registry (
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    agent_id TEXT NOT NULL CHECK (length(agent_id) BETWEEN 1 AND 128),
    version TEXT NOT NULL CHECK (length(version) BETWEEN 1 AND 64),
    domain TEXT NOT NULL CHECK (length(domain) BETWEEN 1 AND 64),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'validated', 'active', 'suspended', 'deprecated', 'revoked')),
    manifest JSONB NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
    manifest_sha256 TEXT NOT NULL CHECK (manifest_sha256 ~ '^(sha256:)?[a-f0-9]{64}$'),
    registered_by TEXT NOT NULL CHECK (length(registered_by) BETWEEN 1 AND 128),
    validated_at TIMESTAMPTZ,
    activated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, agent_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rusa_agent_registry_active_version
    ON public.rusa_agent_registry(tenant_id, agent_id)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_rusa_agent_registry_capability
    ON public.rusa_agent_registry(tenant_id, domain, status);

CREATE TABLE IF NOT EXISTS public.rusa_policy_decisions (
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    decision_id TEXT NOT NULL CHECK (length(decision_id) BETWEEN 1 AND 128),
    actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 128),
    action TEXT NOT NULL CHECK (length(action) BETWEEN 1 AND 256),
    resource_ref TEXT NOT NULL CHECK (length(resource_ref) BETWEEN 1 AND 2048),
    policy_id TEXT NOT NULL CHECK (length(policy_id) BETWEEN 1 AND 128),
    policy_version TEXT NOT NULL CHECK (length(policy_version) BETWEEN 1 AND 64),
    outcome TEXT NOT NULL CHECK (outcome IN ('allow', 'deny', 'require_approval')),
    reason_code TEXT NOT NULL CHECK (length(reason_code) BETWEEN 1 AND 128),
    input_sha256 TEXT NOT NULL CHECK (input_sha256 ~ '^(sha256:)?[a-f0-9]{64}$'),
    evaluated_at TIMESTAMPTZ NOT NULL,
    approval_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, decision_id)
);
CREATE INDEX IF NOT EXISTS idx_rusa_policy_decisions_resource
    ON public.rusa_policy_decisions(tenant_id, resource_ref, evaluated_at DESC);
CREATE INDEX IF NOT EXISTS idx_rusa_policy_decisions_actor
    ON public.rusa_policy_decisions(tenant_id, actor, evaluated_at DESC);

CREATE TABLE IF NOT EXISTS public.rusa_human_approvals (
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    approval_id TEXT NOT NULL CHECK (length(approval_id) BETWEEN 1 AND 128),
    workflow_id TEXT,
    run_id TEXT,
    task_id TEXT,
    action TEXT NOT NULL CHECK (length(action) BETWEEN 1 AND 256),
    resource_ref TEXT NOT NULL CHECK (length(resource_ref) BETWEEN 1 AND 2048),
    artifact_digest TEXT CHECK (
        artifact_digest IS NULL OR artifact_digest ~ '^(sha256:)?[a-f0-9]{64}$'
    ),
    binding_sha256 TEXT NOT NULL CHECK (binding_sha256 ~ '^(sha256:)?[a-f0-9]{64}$'),
    policy_id TEXT NOT NULL CHECK (length(policy_id) BETWEEN 1 AND 128),
    policy_version TEXT NOT NULL CHECK (length(policy_version) BETWEEN 1 AND 64),
    requested_by TEXT NOT NULL CHECK (length(requested_by) BETWEEN 1 AND 128),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'cancelled')),
    expires_at TIMESTAMPTZ NOT NULL,
    decided_by TEXT,
    decided_at TIMESTAMPTZ,
    decision_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, approval_id),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES public.rusa_workflow_runs(tenant_id, run_id) ON DELETE RESTRICT,
    CHECK ((decided_by IS NULL) = (decided_at IS NULL)),
    CHECK (expires_at > created_at),
    CHECK (status = 'pending' OR decided_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_rusa_human_approvals_pending
    ON public.rusa_human_approvals(tenant_id, expires_at)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_rusa_human_approvals_task
    ON public.rusa_human_approvals(tenant_id, task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.rusa_evidence_records (
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    evidence_id TEXT NOT NULL CHECK (length(evidence_id) BETWEEN 1 AND 128),
    uri TEXT NOT NULL CHECK (length(uri) BETWEEN 1 AND 2048),
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^(sha256:)?[a-f0-9]{64}$'),
    kind TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 64),
    producer_id TEXT NOT NULL CHECK (length(producer_id) BETWEEN 1 AND 128),
    producer_version TEXT NOT NULL CHECK (length(producer_version) BETWEEN 1 AND 64),
    subject_digest TEXT NOT NULL CHECK (subject_digest ~ '^(sha256:)?[a-f0-9]{64}$'),
    subject_commit TEXT CHECK (subject_commit IS NULL OR subject_commit ~ '^[a-f0-9]{40,64}$'),
    test_profile TEXT NOT NULL CHECK (length(test_profile) BETWEEN 1 AND 128),
    occurred_at TIMESTAMPTZ NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, evidence_id),
    UNIQUE (tenant_id, uri, sha256)
);
CREATE INDEX IF NOT EXISTS idx_rusa_evidence_subject
    ON public.rusa_evidence_records(tenant_id, subject_digest, occurred_at DESC);

DO $rusa$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'rusa_agent_registry', 'rusa_policy_decisions',
        'rusa_human_approvals', 'rusa_evidence_records'
    ] LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC, rusa_runtime, rusa_api_runtime, rusa_worker_runtime', table_name);
    END LOOP;
END;
$rusa$;

GRANT SELECT, INSERT, UPDATE ON public.rusa_agent_registry TO rusa_api_runtime;
GRANT SELECT ON public.rusa_agent_registry TO rusa_worker_runtime;
GRANT SELECT, INSERT ON public.rusa_policy_decisions TO rusa_api_runtime, rusa_worker_runtime;
GRANT SELECT, INSERT, UPDATE ON public.rusa_human_approvals TO rusa_api_runtime;
GRANT SELECT ON public.rusa_human_approvals TO rusa_worker_runtime;
GRANT SELECT, INSERT ON public.rusa_evidence_records TO rusa_api_runtime, rusa_worker_runtime;

CREATE POLICY rusa_agent_registry_api_read ON public.rusa_agent_registry
    FOR SELECT TO rusa_api_runtime
    USING (
        tenant_id = NULLIF(current_setting('rusa.tenant_id', true), '')
        OR tenant_id = 'platform'
    );
CREATE POLICY rusa_agent_registry_api_write ON public.rusa_agent_registry
    FOR INSERT TO rusa_api_runtime
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
CREATE POLICY rusa_agent_registry_api_update ON public.rusa_agent_registry
    FOR UPDATE TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
CREATE POLICY rusa_agent_registry_worker_read ON public.rusa_agent_registry
    FOR SELECT TO rusa_worker_runtime
    USING (
        tenant_id = NULLIF(current_setting('rusa.tenant_id', true), '')
        OR tenant_id = 'platform'
    );

CREATE POLICY rusa_policy_decisions_api_tenant ON public.rusa_policy_decisions
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
CREATE POLICY rusa_policy_decisions_worker_tenant ON public.rusa_policy_decisions
    FOR ALL TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

CREATE POLICY rusa_human_approvals_api_tenant ON public.rusa_human_approvals
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
CREATE POLICY rusa_human_approvals_worker_read ON public.rusa_human_approvals
    FOR SELECT TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

CREATE POLICY rusa_evidence_records_api_tenant ON public.rusa_evidence_records
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
CREATE POLICY rusa_evidence_records_worker_tenant ON public.rusa_evidence_records
    FOR ALL TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

-- Decisions and evidence are append-only. Approval rows may transition exactly once
-- from pending to a terminal decision and are protected by compare-and-set in the service.
CREATE OR REPLACE FUNCTION public.rusa_platform_append_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;
REVOKE ALL ON FUNCTION public.rusa_platform_append_only() FROM PUBLIC;
CREATE TRIGGER trg_rusa_policy_decisions_immutable
    BEFORE UPDATE OR DELETE ON public.rusa_policy_decisions
    FOR EACH ROW EXECUTE FUNCTION public.rusa_platform_append_only();
CREATE TRIGGER trg_rusa_policy_decisions_no_truncate
    BEFORE TRUNCATE ON public.rusa_policy_decisions
    FOR EACH STATEMENT EXECUTE FUNCTION public.rusa_platform_append_only();
CREATE TRIGGER trg_rusa_evidence_records_immutable
    BEFORE UPDATE OR DELETE ON public.rusa_evidence_records
    FOR EACH ROW EXECUTE FUNCTION public.rusa_platform_append_only();
CREATE TRIGGER trg_rusa_evidence_records_no_truncate
    BEFORE TRUNCATE ON public.rusa_evidence_records
    FOR EACH STATEMENT EXECUTE FUNCTION public.rusa_platform_append_only();

COMMIT;
