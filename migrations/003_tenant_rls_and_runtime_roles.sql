-- Defense-in-depth tenant isolation and separate API/worker database identities.
-- Apply as the schema owner after migrations 001 and 002.
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rusa_api_runtime') THEN
        CREATE ROLE rusa_api_runtime NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rusa_worker_runtime') THEN
        CREATE ROLE rusa_worker_runtime NOLOGIN;
    END IF;
END
$$;

-- Retire the original shared runtime privilege group. New deployments must use
-- separate login roles that inherit exactly one of the following groups.
REVOKE ALL PRIVILEGES ON TABLE
    public.aicode_jobs,
    public.aicode_memory,
    public.aicode_rate_limits,
    public.aicode_audit_events
FROM PUBLIC, rusa_runtime;
REVOKE ALL PRIVILEGES ON SEQUENCE
    public.aicode_jobs_id_seq,
    public.aicode_audit_events_id_seq
FROM PUBLIC, rusa_runtime;

GRANT USAGE ON SCHEMA public TO rusa_api_runtime, rusa_worker_runtime;

-- API: enqueue and inspect/update only the authenticated tenant's tasks.
GRANT SELECT, INSERT, UPDATE ON public.aicode_jobs TO rusa_api_runtime;
GRANT USAGE, SELECT ON SEQUENCE public.aicode_jobs_id_seq TO rusa_api_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.aicode_rate_limits TO rusa_api_runtime;

-- Worker: claim jobs across tenants, but tenant-scope memory in PostgreSQL.
GRANT SELECT, UPDATE ON public.aicode_jobs TO rusa_worker_runtime;
GRANT SELECT, INSERT, UPDATE ON public.aicode_memory TO rusa_worker_runtime;
GRANT SELECT, INSERT ON public.aicode_audit_events TO rusa_worker_runtime;
GRANT USAGE, SELECT ON SEQUENCE public.aicode_audit_events_id_seq TO rusa_worker_runtime;

ALTER TABLE public.aicode_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aicode_jobs FORCE ROW LEVEL SECURITY;
ALTER TABLE public.aicode_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aicode_memory FORCE ROW LEVEL SECURITY;
ALTER TABLE public.aicode_rate_limits ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aicode_rate_limits FORCE ROW LEVEL SECURITY;
ALTER TABLE public.aicode_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aicode_audit_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS aicode_jobs_api_tenant ON public.aicode_jobs;
CREATE POLICY aicode_jobs_api_tenant ON public.aicode_jobs
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

-- The worker is the trusted queue consumer and must claim from all tenants.
-- Every task mutation is still fenced by tenant_id, worker_id, lease_token,
-- status, and lease expiry in the queue adapter.
DROP POLICY IF EXISTS aicode_jobs_worker_queue ON public.aicode_jobs;
CREATE POLICY aicode_jobs_worker_queue ON public.aicode_jobs
    FOR ALL TO rusa_worker_runtime
    USING (true)
    WITH CHECK (true);

DROP POLICY IF EXISTS aicode_memory_worker_tenant ON public.aicode_memory;
CREATE POLICY aicode_memory_worker_tenant ON public.aicode_memory
    FOR ALL TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

DROP POLICY IF EXISTS aicode_rate_limits_api_tenant ON public.aicode_rate_limits;
CREATE POLICY aicode_rate_limits_api_tenant ON public.aicode_rate_limits
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

-- Only the worker identity can read/append the global integrity chain.
-- It receives no UPDATE, DELETE, or TRUNCATE privilege; immutable triggers
-- remain an additional safeguard.
DROP POLICY IF EXISTS aicode_audit_worker_only ON public.aicode_audit_events;
CREATE POLICY aicode_audit_worker_only ON public.aicode_audit_events
    FOR ALL TO rusa_worker_runtime
    USING (true)
    WITH CHECK (true);

COMMIT;
