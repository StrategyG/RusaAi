-- Platform Core 2.0 agent registry, policy decisions, approvals, and evidence.
-- Apply after 010_platform_workflow_core.sql.
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
    PRIMARY KEY (tenant_id, agent_id, version),
    CHECK (manifest->>'status' = status)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rusa_agent_registry_active_version
    ON public.rusa_agent_registry(tenant_id, agent_id)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_rusa_agent_registry_capability
    ON public.rusa_agent_registry(tenant_id, domain, status);

CREATE TABLE IF NOT EXISTS public.rusa_agent_registry_events (
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    event_id UUID NOT NULL,
    agent_id TEXT NOT NULL CHECK (length(agent_id) BETWEEN 1 AND 128),
    version TEXT NOT NULL CHECK (length(version) BETWEEN 1 AND 64),
    action TEXT NOT NULL CHECK (
        action IN ('registered', 'validated', 'activated', 'suspended', 'deprecated', 'revoked', 'health.updated')
    ),
    actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 128),
    previous_status TEXT,
    new_status TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK (manifest_sha256 ~ '^(sha256:)?[a-f0-9]{64}$'),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_rusa_agent_registry_events_agent
    ON public.rusa_agent_registry_events(tenant_id, agent_id, occurred_at DESC);

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
CREATE UNIQUE INDEX IF NOT EXISTS idx_rusa_policy_decisions_identical_denial
    ON public.rusa_policy_decisions(
        tenant_id, action, resource_ref, policy_id, policy_version, input_sha256
    )
    WHERE outcome = 'deny';

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
    FOREIGN KEY (tenant_id, workflow_id, run_id)
        REFERENCES public.rusa_workflow_runs(tenant_id, workflow_id, run_id) ON DELETE RESTRICT,
    CHECK (
        (workflow_id IS NULL AND run_id IS NULL AND task_id IS NULL)
        OR (workflow_id IS NOT NULL AND run_id IS NOT NULL AND task_id IS NOT NULL)
    ),
    CHECK ((decided_by IS NULL) = (decided_at IS NULL)),
    CHECK (expires_at > created_at),
    CHECK (status = 'pending' OR decided_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_rusa_human_approvals_pending
    ON public.rusa_human_approvals(tenant_id, expires_at)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_rusa_human_approvals_task
    ON public.rusa_human_approvals(tenant_id, task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.rusa_approval_consumptions (
    tenant_id TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    binding_sha256 TEXT NOT NULL CHECK (binding_sha256 ~ '^(sha256:)?[a-f0-9]{64}$'),
    consumed_by TEXT NOT NULL CHECK (length(consumed_by) BETWEEN 1 AND 128),
    consumed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, approval_id),
    FOREIGN KEY (tenant_id, approval_id)
        REFERENCES public.rusa_human_approvals(tenant_id, approval_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_rusa_approval_consumptions_time
    ON public.rusa_approval_consumptions(tenant_id, consumed_at DESC);

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
        'rusa_agent_registry', 'rusa_agent_registry_events',
        'rusa_policy_decisions', 'rusa_human_approvals',
        'rusa_approval_consumptions', 'rusa_evidence_records'
    ] LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC, rusa_runtime, rusa_api_runtime, rusa_worker_runtime', table_name);
    END LOOP;
END;
$rusa$;

GRANT SELECT, INSERT, UPDATE ON public.rusa_agent_registry TO rusa_api_runtime;
GRANT SELECT ON public.rusa_agent_registry TO rusa_worker_runtime;
GRANT SELECT, INSERT ON public.rusa_agent_registry_events TO rusa_api_runtime, rusa_worker_runtime;
GRANT SELECT, INSERT ON public.rusa_policy_decisions TO rusa_api_runtime, rusa_worker_runtime;
GRANT SELECT, INSERT, UPDATE ON public.rusa_human_approvals TO rusa_api_runtime;
GRANT SELECT ON public.rusa_human_approvals TO rusa_worker_runtime;
GRANT SELECT, INSERT ON public.rusa_approval_consumptions TO rusa_api_runtime, rusa_worker_runtime;
GRANT SELECT, INSERT ON public.rusa_evidence_records TO rusa_api_runtime, rusa_worker_runtime;

DROP POLICY IF EXISTS rusa_agent_registry_api_read ON public.rusa_agent_registry;
CREATE POLICY rusa_agent_registry_api_read ON public.rusa_agent_registry
    FOR SELECT TO rusa_api_runtime
    USING (
        tenant_id = NULLIF(current_setting('rusa.tenant_id', true), '')
        OR tenant_id = 'platform'
    );
DROP POLICY IF EXISTS rusa_agent_registry_api_write ON public.rusa_agent_registry;
CREATE POLICY rusa_agent_registry_api_write ON public.rusa_agent_registry
    FOR INSERT TO rusa_api_runtime
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
DROP POLICY IF EXISTS rusa_agent_registry_api_update ON public.rusa_agent_registry;
CREATE POLICY rusa_agent_registry_api_update ON public.rusa_agent_registry
    FOR UPDATE TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
DROP POLICY IF EXISTS rusa_agent_registry_worker_read ON public.rusa_agent_registry;
CREATE POLICY rusa_agent_registry_worker_read ON public.rusa_agent_registry
    FOR SELECT TO rusa_worker_runtime
    USING (
        tenant_id = NULLIF(current_setting('rusa.tenant_id', true), '')
        OR tenant_id = 'platform'
    );

DROP POLICY IF EXISTS rusa_agent_registry_events_api_tenant ON public.rusa_agent_registry_events;
CREATE POLICY rusa_agent_registry_events_api_tenant ON public.rusa_agent_registry_events
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
DROP POLICY IF EXISTS rusa_agent_registry_events_worker_tenant ON public.rusa_agent_registry_events;
CREATE POLICY rusa_agent_registry_events_worker_tenant ON public.rusa_agent_registry_events
    FOR SELECT TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

DROP POLICY IF EXISTS rusa_policy_decisions_api_tenant ON public.rusa_policy_decisions;
CREATE POLICY rusa_policy_decisions_api_tenant ON public.rusa_policy_decisions
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
DROP POLICY IF EXISTS rusa_policy_decisions_worker_tenant ON public.rusa_policy_decisions;
CREATE POLICY rusa_policy_decisions_worker_tenant ON public.rusa_policy_decisions
    FOR ALL TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

DROP POLICY IF EXISTS rusa_human_approvals_api_tenant ON public.rusa_human_approvals;
CREATE POLICY rusa_human_approvals_api_tenant ON public.rusa_human_approvals
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
DROP POLICY IF EXISTS rusa_human_approvals_worker_read ON public.rusa_human_approvals;
CREATE POLICY rusa_human_approvals_worker_read ON public.rusa_human_approvals
    FOR SELECT TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

DROP POLICY IF EXISTS rusa_approval_consumptions_api_tenant ON public.rusa_approval_consumptions;
CREATE POLICY rusa_approval_consumptions_api_tenant ON public.rusa_approval_consumptions
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
DROP POLICY IF EXISTS rusa_approval_consumptions_worker_tenant ON public.rusa_approval_consumptions;
CREATE POLICY rusa_approval_consumptions_worker_tenant ON public.rusa_approval_consumptions
    FOR ALL TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

DROP POLICY IF EXISTS rusa_evidence_records_api_tenant ON public.rusa_evidence_records;
CREATE POLICY rusa_evidence_records_api_tenant ON public.rusa_evidence_records
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
DROP POLICY IF EXISTS rusa_evidence_records_worker_tenant ON public.rusa_evidence_records;
CREATE POLICY rusa_evidence_records_worker_tenant ON public.rusa_evidence_records
    FOR ALL TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

CREATE OR REPLACE FUNCTION public.rusa_agent_registry_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    allowed_status BOOLEAN;
BEGIN
    IF OLD.status <> 'draft' THEN
        IF NEW.tenant_id <> OLD.tenant_id
            OR NEW.agent_id <> OLD.agent_id
            OR NEW.version <> OLD.version
            OR NEW.domain <> OLD.domain
            OR (NEW.manifest - ARRAY['status', 'health', 'updated_at'])
                <> (OLD.manifest - ARRAY['status', 'health', 'updated_at'])
            OR NEW.manifest_sha256 <> OLD.manifest_sha256
            OR NEW.registered_by <> OLD.registered_by
            OR NEW.created_at <> OLD.created_at THEN
            RAISE EXCEPTION 'published agent manifest content is immutable';
        END IF;
    END IF;

    allowed_status :=
        (OLD.status = 'draft' AND NEW.status IN ('validated', 'revoked'))
        OR (OLD.status = 'validated' AND NEW.status IN ('active', 'suspended', 'deprecated', 'revoked'))
        OR (OLD.status = 'active' AND NEW.status IN ('suspended', 'deprecated', 'revoked'))
        OR (OLD.status = 'suspended' AND NEW.status IN ('active', 'deprecated', 'revoked'))
        OR (OLD.status = 'deprecated' AND NEW.status = 'revoked')
        OR (OLD.status = NEW.status);

    IF NOT allowed_status THEN
        RAISE EXCEPTION 'invalid agent registry status transition: % -> %', OLD.status, NEW.status;
    END IF;
    IF NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'agent registry updated_at cannot move backwards';
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION public.rusa_agent_registry_guard() FROM PUBLIC;
DROP TRIGGER IF EXISTS trg_rusa_agent_registry_guard ON public.rusa_agent_registry;
CREATE TRIGGER trg_rusa_agent_registry_guard
    BEFORE UPDATE ON public.rusa_agent_registry
    FOR EACH ROW EXECUTE FUNCTION public.rusa_agent_registry_guard();

CREATE OR REPLACE FUNCTION public.rusa_human_approval_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.tenant_id <> NEW.tenant_id
        OR OLD.approval_id <> NEW.approval_id
        OR OLD.workflow_id IS DISTINCT FROM NEW.workflow_id
        OR OLD.run_id IS DISTINCT FROM NEW.run_id
        OR OLD.task_id IS DISTINCT FROM NEW.task_id
        OR OLD.action <> NEW.action
        OR OLD.resource_ref <> NEW.resource_ref
        OR OLD.artifact_digest IS DISTINCT FROM NEW.artifact_digest
        OR OLD.binding_sha256 <> NEW.binding_sha256
        OR OLD.policy_id <> NEW.policy_id
        OR OLD.policy_version <> NEW.policy_version
        OR OLD.requested_by <> NEW.requested_by
        OR OLD.expires_at <> NEW.expires_at
        OR OLD.created_at <> NEW.created_at THEN
        RAISE EXCEPTION 'human approval binding is immutable';
    END IF;
    IF OLD.status <> 'pending' OR NEW.status NOT IN ('approved', 'rejected', 'expired', 'cancelled') THEN
        RAISE EXCEPTION 'human approval may transition once from pending to a terminal decision';
    END IF;
    IF NEW.decided_by IS NULL OR NEW.decided_at IS NULL THEN
        RAISE EXCEPTION 'human approval decision requires actor and timestamp';
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION public.rusa_human_approval_guard() FROM PUBLIC;
DROP TRIGGER IF EXISTS trg_rusa_human_approval_guard ON public.rusa_human_approvals;
CREATE TRIGGER trg_rusa_human_approval_guard
    BEFORE UPDATE ON public.rusa_human_approvals
    FOR EACH ROW EXECUTE FUNCTION public.rusa_human_approval_guard();

CREATE OR REPLACE FUNCTION public.rusa_platform_append_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;
REVOKE ALL ON FUNCTION public.rusa_platform_append_only() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_rusa_agent_registry_events_immutable ON public.rusa_agent_registry_events;
CREATE TRIGGER trg_rusa_agent_registry_events_immutable
    BEFORE UPDATE OR DELETE ON public.rusa_agent_registry_events
    FOR EACH ROW EXECUTE FUNCTION public.rusa_platform_append_only();
DROP TRIGGER IF EXISTS trg_rusa_agent_registry_events_no_truncate ON public.rusa_agent_registry_events;
CREATE TRIGGER trg_rusa_agent_registry_events_no_truncate
    BEFORE TRUNCATE ON public.rusa_agent_registry_events
    FOR EACH STATEMENT EXECUTE FUNCTION public.rusa_platform_append_only();

DROP TRIGGER IF EXISTS trg_rusa_policy_decisions_immutable ON public.rusa_policy_decisions;
CREATE TRIGGER trg_rusa_policy_decisions_immutable
    BEFORE UPDATE OR DELETE ON public.rusa_policy_decisions
    FOR EACH ROW EXECUTE FUNCTION public.rusa_platform_append_only();
DROP TRIGGER IF EXISTS trg_rusa_policy_decisions_no_truncate ON public.rusa_policy_decisions;
CREATE TRIGGER trg_rusa_policy_decisions_no_truncate
    BEFORE TRUNCATE ON public.rusa_policy_decisions
    FOR EACH STATEMENT EXECUTE FUNCTION public.rusa_platform_append_only();

DROP TRIGGER IF EXISTS trg_rusa_approval_consumptions_immutable ON public.rusa_approval_consumptions;
CREATE TRIGGER trg_rusa_approval_consumptions_immutable
    BEFORE UPDATE OR DELETE ON public.rusa_approval_consumptions
    FOR EACH ROW EXECUTE FUNCTION public.rusa_platform_append_only();
DROP TRIGGER IF EXISTS trg_rusa_approval_consumptions_no_truncate ON public.rusa_approval_consumptions;
CREATE TRIGGER trg_rusa_approval_consumptions_no_truncate
    BEFORE TRUNCATE ON public.rusa_approval_consumptions
    FOR EACH STATEMENT EXECUTE FUNCTION public.rusa_platform_append_only();

DROP TRIGGER IF EXISTS trg_rusa_evidence_records_immutable ON public.rusa_evidence_records;
CREATE TRIGGER trg_rusa_evidence_records_immutable
    BEFORE UPDATE OR DELETE ON public.rusa_evidence_records
    FOR EACH ROW EXECUTE FUNCTION public.rusa_platform_append_only();
DROP TRIGGER IF EXISTS trg_rusa_evidence_records_no_truncate ON public.rusa_evidence_records;
CREATE TRIGGER trg_rusa_evidence_records_no_truncate
    BEFORE TRUNCATE ON public.rusa_evidence_records
    FOR EACH STATEMENT EXECUTE FUNCTION public.rusa_platform_append_only();

COMMIT;
