-- Apply as a database/schema owner, not as the runtime service account.
-- After applying, provision a login credential for role rusa_runtime through your secret manager.

CREATE TABLE IF NOT EXISTS public.aicode_jobs (
    id BIGSERIAL PRIMARY KEY,
    task_id UUID NOT NULL,
    tenant_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'awaiting_approval', 'succeeded', 'failed', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    priority INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, task_id)
);

CREATE INDEX IF NOT EXISTS idx_aicode_jobs_claim
    ON public.aicode_jobs(status, available_at, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_aicode_jobs_tenant
    ON public.aicode_jobs(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.aicode_memory (
    tenant_id TEXT NOT NULL,
    task_id UUID NOT NULL,
    repository TEXT NOT NULL,
    state TEXT NOT NULL,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_aicode_memory_tenant_repo
    ON public.aicode_memory(tenant_id, repository);

CREATE TABLE IF NOT EXISTS public.aicode_audit_events (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event TEXT NOT NULL,
    task_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    details JSONB NOT NULL,
    previous_hash TEXT NOT NULL,
    record_hash TEXT NOT NULL UNIQUE
);

CREATE OR REPLACE FUNCTION public.rusa_audit_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'aicode_audit_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_aicode_audit_immutable ON public.aicode_audit_events;
CREATE TRIGGER trg_aicode_audit_immutable
BEFORE UPDATE OR DELETE ON public.aicode_audit_events
FOR EACH ROW EXECUTE FUNCTION public.rusa_audit_immutable();

DROP TRIGGER IF EXISTS trg_aicode_audit_no_truncate ON public.aicode_audit_events;
CREATE TRIGGER trg_aicode_audit_no_truncate
BEFORE TRUNCATE ON public.aicode_audit_events
FOR EACH STATEMENT EXECUTE FUNCTION public.rusa_audit_immutable();

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rusa_runtime') THEN
        CREATE ROLE rusa_runtime NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO rusa_runtime;
GRANT SELECT, INSERT, UPDATE ON public.aicode_jobs TO rusa_runtime;
GRANT USAGE, SELECT ON SEQUENCE public.aicode_jobs_id_seq TO rusa_runtime;
GRANT SELECT, INSERT, UPDATE ON public.aicode_memory TO rusa_runtime;
GRANT SELECT, INSERT ON public.aicode_audit_events TO rusa_runtime;
GRANT USAGE, SELECT ON SEQUENCE public.aicode_audit_events_id_seq TO rusa_runtime;

-- Runtime cannot alter/delete audit rows. Keep ownership with the migration role.
