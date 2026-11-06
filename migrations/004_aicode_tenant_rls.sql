-- Database-enforced tenant isolation and separate API/worker identities.
-- Apply as the migration owner after migrations 001, 002, and 003_aicode_task_results.
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

-- Keep privilege groups non-login and explicitly unable to bypass row security.
ALTER ROLE rusa_api_runtime NOLOGIN NOBYPASSRLS;
ALTER ROLE rusa_worker_runtime NOLOGIN NOBYPASSRLS;

-- Retire broad legacy shared-role grants from migration 001.
REVOKE ALL ON public.aicode_jobs FROM PUBLIC, rusa_runtime, rusa_api_runtime, rusa_worker_runtime;
REVOKE ALL ON public.aicode_memory FROM PUBLIC, rusa_runtime, rusa_api_runtime, rusa_worker_runtime;
REVOKE ALL ON public.aicode_rate_limits FROM PUBLIC, rusa_runtime, rusa_api_runtime, rusa_worker_runtime;
REVOKE ALL ON public.aicode_audit_events FROM PUBLIC, rusa_runtime, rusa_api_runtime, rusa_worker_runtime;
REVOKE ALL ON SEQUENCE public.aicode_jobs_id_seq FROM PUBLIC, rusa_runtime, rusa_api_runtime, rusa_worker_runtime;
REVOKE ALL ON SEQUENCE public.aicode_audit_events_id_seq FROM PUBLIC, rusa_runtime, rusa_api_runtime, rusa_worker_runtime;

GRANT USAGE ON SCHEMA public TO rusa_api_runtime, rusa_worker_runtime;

-- API can enqueue and manage only the authenticated tenant's jobs.
GRANT SELECT ON public.aicode_jobs TO rusa_api_runtime, rusa_worker_runtime;
GRANT INSERT (task_id, tenant_id, payload, max_attempts, priority)
    ON public.aicode_jobs TO rusa_api_runtime;
GRANT UPDATE (status, attempts, available_at, lease_owner, lease_until, lease_token,
              last_error, result, updated_at)
    ON public.aicode_jobs TO rusa_api_runtime, rusa_worker_runtime;
GRANT USAGE, SELECT ON SEQUENCE public.aicode_jobs_id_seq TO rusa_api_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.aicode_rate_limits TO rusa_api_runtime;

-- Worker can claim/update jobs across tenants, but cannot enqueue or rewrite task identity/payload.
GRANT SELECT, INSERT, UPDATE ON public.aicode_memory TO rusa_worker_runtime;
GRANT SELECT, INSERT ON public.aicode_audit_events TO rusa_worker_runtime;
GRANT USAGE, SELECT ON SEQUENCE public.aicode_audit_events_id_seq TO rusa_worker_runtime;

ALTER TABLE public.aicode_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aicode_jobs FORCE ROW LEVEL SECURITY;
ALTER TABLE public.aicode_rate_limits ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aicode_rate_limits FORCE ROW LEVEL SECURITY;
ALTER TABLE public.aicode_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aicode_memory FORCE ROW LEVEL SECURITY;
ALTER TABLE public.aicode_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aicode_audit_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS aicode_jobs_api_tenant ON public.aicode_jobs;
CREATE POLICY aicode_jobs_api_tenant ON public.aicode_jobs
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

-- Queue workers are trusted cross-tenant consumers. Each transition is still
-- fenced by tenant, worker ID, lease token, status, and lease expiry.
-- Remove legacy permissive policy names if an earlier candidate migration was applied.
DROP POLICY IF EXISTS aicode_jobs_worker_all ON public.aicode_jobs;
DROP POLICY IF EXISTS aicode_memory_worker_all ON public.aicode_memory;
DROP POLICY IF EXISTS aicode_audit_worker_all ON public.aicode_audit_events;

DROP POLICY IF EXISTS aicode_jobs_worker_queue ON public.aicode_jobs;
CREATE POLICY aicode_jobs_worker_queue ON public.aicode_jobs
    FOR ALL TO rusa_worker_runtime
    USING (true)
    WITH CHECK (true);

DROP POLICY IF EXISTS aicode_rate_limits_api_tenant ON public.aicode_rate_limits;
CREATE POLICY aicode_rate_limits_api_tenant ON public.aicode_rate_limits
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

DROP POLICY IF EXISTS aicode_memory_worker_tenant ON public.aicode_memory;
CREATE POLICY aicode_memory_worker_tenant ON public.aicode_memory
    FOR ALL TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

-- Only the worker can read/append the global audit integrity chain.
-- Table grants and immutable triggers prohibit mutation of existing records.
DROP POLICY IF EXISTS aicode_audit_worker_only ON public.aicode_audit_events;
CREATE POLICY aicode_audit_worker_only ON public.aicode_audit_events
    FOR ALL TO rusa_worker_runtime
    USING (true)
    WITH CHECK (true);

COMMIT;
