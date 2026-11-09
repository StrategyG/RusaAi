-- Predictive DevOps Intelligence tables. Apply after migrations 001-008.
-- Run as the schema owner. Runtime roles remain non-login and tenant-scoped.
BEGIN;

CREATE TABLE IF NOT EXISTS public.rusa_devops_jobs (
    id BIGSERIAL PRIMARY KEY,
    task_id UUID NOT NULL,
    tenant_id TEXT NOT NULL,
    job_type TEXT NOT NULL CHECK (job_type IN (
        'synthetic_ingest', 'prometheus_collect', 'train_forecaster',
        'anomaly_scan', 'capacity_recommendation'
    )),
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
    priority INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    lease_token UUID,
    result JSONB,
    idempotency_key TEXT,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_rusa_devops_jobs_claim
    ON public.rusa_devops_jobs(status, available_at, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_rusa_devops_jobs_tenant
    ON public.rusa_devops_jobs(tenant_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rusa_devops_jobs_idempotency
    ON public.rusa_devops_jobs(tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.rusa_devops_telemetry (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    metric_name TEXT NOT NULL CHECK (length(metric_name) BETWEEN 1 AND 200),
    resource_id TEXT NOT NULL CHECK (length(resource_id) BETWEEN 1 AND 200),
    observed_at TIMESTAMPTZ NOT NULL,
    value DOUBLE PRECISION NOT NULL CHECK (value BETWEEN -1000000000000000 AND 1000000000000000),
    labels JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (tenant_id, metric_name, resource_id, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_rusa_devops_telemetry_lookup
    ON public.rusa_devops_telemetry(tenant_id, metric_name, resource_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS public.rusa_devops_model_runs (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    run_id UUID NOT NULL,
    metric_name TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed', 'rejected')),
    artifact_uri TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    metrics JSONB NOT NULL,
    config JSONB NOT NULL,
    train_rows INTEGER NOT NULL CHECK (train_rows >= 0),
    validation_rows INTEGER NOT NULL CHECK (validation_rows >= 0),
    test_rows INTEGER NOT NULL CHECK (test_rows >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_rusa_devops_model_runs_tenant_time
    ON public.rusa_devops_model_runs(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.rusa_devops_incidents (
    id BIGSERIAL PRIMARY KEY,
    incident_id UUID NOT NULL,
    tenant_id TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    observed_value DOUBLE PRECISION NOT NULL,
    z_score DOUBLE PRECISION NOT NULL,
    anomaly_probability DOUBLE PRECISION NOT NULL CHECK (anomaly_probability BETWEEN 0 AND 1),
    severity TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, incident_id),
    UNIQUE (tenant_id, metric_name, resource_id, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_rusa_devops_incidents_tenant_time
    ON public.rusa_devops_incidents(tenant_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS public.rusa_devops_action_proposals (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    proposal_id UUID NOT NULL,
    action_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_approval' CHECK (
        status IN ('pending_approval', 'approved', 'rejected', 'expired', 'executed')
    ),
    payload JSONB NOT NULL,
    approval_required BOOLEAN NOT NULL DEFAULT TRUE CHECK (approval_required),
    proposed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at TIMESTAMPTZ,
    decided_by TEXT,
    decision_reason TEXT,
    UNIQUE (tenant_id, proposal_id)
);
CREATE INDEX IF NOT EXISTS idx_rusa_devops_proposals_tenant_status
    ON public.rusa_devops_action_proposals(tenant_id, status, proposed_at DESC);

CREATE TABLE IF NOT EXISTS public.rusa_devops_audit_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    tenant_id TEXT NOT NULL,
    task_id TEXT,
    actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 128),
    action TEXT NOT NULL CHECK (action IN (
        'job.enqueue', 'job.retry', 'job.cancel', 'action.propose',
        'action.approve', 'action.reject', 'model.promote'
    )),
    decision TEXT NOT NULL CHECK (decision IN ('allow', 'deny')),
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_rusa_devops_audit_tenant_time
    ON public.rusa_devops_audit_events(tenant_id, occurred_at DESC);

ALTER TABLE public.rusa_devops_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_devops_jobs FORCE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_devops_telemetry ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_devops_telemetry FORCE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_devops_model_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_devops_model_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_devops_incidents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_devops_incidents FORCE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_devops_action_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_devops_action_proposals FORCE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_devops_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_devops_audit_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS rusa_devops_jobs_api_tenant ON public.rusa_devops_jobs;
CREATE POLICY rusa_devops_jobs_api_tenant ON public.rusa_devops_jobs
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

-- The worker may claim across tenants; it must set the claimed tenant before
-- reading telemetry or writing any tenant-owned result.
DROP POLICY IF EXISTS rusa_devops_jobs_worker_queue ON public.rusa_devops_jobs;
CREATE POLICY rusa_devops_jobs_worker_queue ON public.rusa_devops_jobs
    FOR ALL TO rusa_worker_runtime USING (true) WITH CHECK (true);

DO $$
DECLARE table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'rusa_devops_telemetry', 'rusa_devops_model_runs',
        'rusa_devops_incidents', 'rusa_devops_action_proposals'
    ] LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', table_name || '_tenant', table_name);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR ALL TO rusa_api_runtime, rusa_worker_runtime
             USING (tenant_id = NULLIF(current_setting(''rusa.tenant_id'', true), ''''))
             WITH CHECK (tenant_id = NULLIF(current_setting(''rusa.tenant_id'', true), ''''))',
            table_name || '_tenant', table_name
        );
    END LOOP;
END;
$$;

DROP POLICY IF EXISTS rusa_devops_audit_api_insert ON public.rusa_devops_audit_events;
CREATE POLICY rusa_devops_audit_api_insert ON public.rusa_devops_audit_events
    FOR INSERT TO rusa_api_runtime
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), '') AND actor LIKE 'api:%');
DROP POLICY IF EXISTS rusa_devops_audit_worker_insert ON public.rusa_devops_audit_events;
CREATE POLICY rusa_devops_audit_worker_insert ON public.rusa_devops_audit_events
    FOR INSERT TO rusa_worker_runtime
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), '') AND actor LIKE 'worker:%');

REVOKE ALL ON public.rusa_devops_jobs, public.rusa_devops_telemetry,
    public.rusa_devops_model_runs, public.rusa_devops_incidents,
    public.rusa_devops_action_proposals, public.rusa_devops_audit_events
    FROM PUBLIC, rusa_runtime;
REVOKE ALL ON SEQUENCE public.rusa_devops_jobs_id_seq,
    public.rusa_devops_telemetry_id_seq, public.rusa_devops_model_runs_id_seq,
    public.rusa_devops_incidents_id_seq, public.rusa_devops_action_proposals_id_seq,
    public.rusa_devops_audit_events_id_seq
    FROM PUBLIC, rusa_runtime;

GRANT USAGE ON SCHEMA public TO rusa_api_runtime, rusa_worker_runtime;
GRANT SELECT, INSERT, UPDATE ON public.rusa_devops_jobs TO rusa_api_runtime;
GRANT SELECT, UPDATE ON public.rusa_devops_jobs TO rusa_worker_runtime;
GRANT SELECT ON public.rusa_devops_telemetry, public.rusa_devops_model_runs,
    public.rusa_devops_incidents, public.rusa_devops_action_proposals TO rusa_api_runtime;
GRANT SELECT, INSERT, UPDATE ON public.rusa_devops_telemetry TO rusa_worker_runtime;
GRANT SELECT, INSERT ON public.rusa_devops_model_runs,
    public.rusa_devops_incidents, public.rusa_devops_action_proposals TO rusa_worker_runtime;
GRANT INSERT, UPDATE ON public.rusa_devops_action_proposals TO rusa_api_runtime;
GRANT INSERT ON public.rusa_devops_audit_events TO rusa_api_runtime, rusa_worker_runtime;
GRANT USAGE, SELECT ON SEQUENCE public.rusa_devops_jobs_id_seq,
    public.rusa_devops_telemetry_id_seq, public.rusa_devops_model_runs_id_seq,
    public.rusa_devops_incidents_id_seq, public.rusa_devops_action_proposals_id_seq,
    public.rusa_devops_audit_events_id_seq TO rusa_api_runtime, rusa_worker_runtime;

CREATE OR REPLACE FUNCTION public.rusa_devops_audit_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'rusa_devops_audit_events is append-only';
END;
$$;
REVOKE ALL ON FUNCTION public.rusa_devops_audit_immutable() FROM PUBLIC;
DROP TRIGGER IF EXISTS trg_rusa_devops_audit_immutable ON public.rusa_devops_audit_events;
CREATE TRIGGER trg_rusa_devops_audit_immutable
    BEFORE UPDATE OR DELETE ON public.rusa_devops_audit_events
    FOR EACH ROW EXECUTE FUNCTION public.rusa_devops_audit_immutable();
DROP TRIGGER IF EXISTS trg_rusa_devops_audit_no_truncate ON public.rusa_devops_audit_events;
CREATE TRIGGER trg_rusa_devops_audit_no_truncate
    BEFORE TRUNCATE ON public.rusa_devops_audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION public.rusa_devops_audit_immutable();

COMMIT;
