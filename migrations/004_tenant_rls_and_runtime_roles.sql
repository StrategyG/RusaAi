-- Database-enforced tenant isolation and separate API/worker identities.
-- Apply as the schema owner after migrations 001, 002, and 003.
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rusa_api_runtime') THEN
        CREATE ROLE rusa_api_runtime NOLOGIN NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rusa_worker_runtime') THEN
        CREATE ROLE rusa_worker_runtime NOLOGIN NOBYPASSRLS;
    END IF;
END
$$;

-- These are privilege groups, never application login roles and never BYPASSRLS.
ALTER ROLE rusa_api_runtime NOLOGIN NOBYPASSRLS;
ALTER ROLE rusa_worker_runtime NOLOGIN NOBYPASSRLS;

-- Retire the original shared runtime privilege group. Production must use
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

-- Worker: claim jobs across tenants, but memory remains tenant-scoped.
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

-- The trusted worker may claim across tenants. State transitions remain fenced
-- by tenant_id, worker_id, lease_token, status, and lease expiry in the adapter.
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

-- The worker alone can read/append the global integrity chain. Table grants
-- and immutable triggers still deny UPDATE, DELETE, and TRUNCATE.
DROP POLICY IF EXISTS aicode_audit_worker_only ON public.aicode_audit_events;
CREATE POLICY aicode_audit_worker_only ON public.aicode_audit_events
    FOR ALL TO rusa_worker_runtime
    USING (true)
    WITH CHECK (true);

COMMIT;
