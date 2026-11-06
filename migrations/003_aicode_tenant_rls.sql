-- Database-enforced tenant isolation and separate API/worker identities.
-- Apply with the migration owner after migrations/001 and /002.
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

-- Retire the broad legacy group grants from migration 001.
REVOKE ALL ON public.aicode_jobs FROM rusa_runtime;
REVOKE ALL ON public.aicode_memory FROM rusa_runtime;
REVOKE ALL ON public.aicode_rate_limits FROM rusa_runtime;
REVOKE ALL ON public.aicode_audit_events FROM rusa_runtime;
REVOKE ALL ON SEQUENCE public.aicode_jobs_id_seq FROM rusa_runtime;
REVOKE ALL ON SEQUENCE public.aicode_audit_events_id_seq FROM rusa_runtime;

GRANT USAGE ON SCHEMA public TO rusa_api_runtime, rusa_worker_runtime;

-- API: tenant-bound task intake, status, retry/cancel, and rate limiting.
GRANT SELECT, INSERT, UPDATE ON public.aicode_jobs TO rusa_api_runtime;
GRANT USAGE, SELECT ON SEQUENCE public.aicode_jobs_id_seq TO rusa_api_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.aicode_rate_limits TO rusa_api_runtime;

-- Worker: cross-tenant queue consumption is an explicit trusted service capability.
GRANT SELECT, INSERT, UPDATE ON public.aicode_jobs TO rusa_worker_runtime;
GRANT USAGE, SELECT ON SEQUENCE public.aicode_jobs_id_seq TO rusa_worker_runtime;
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

DROP POLICY IF EXISTS aicode_jobs_worker_all ON public.aicode_jobs;
CREATE POLICY aicode_jobs_worker_all ON public.aicode_jobs
    FOR ALL TO rusa_worker_runtime
    USING (true)
    WITH CHECK (true);

DROP POLICY IF EXISTS aicode_rate_limits_api_tenant ON public.aicode_rate_limits;
CREATE POLICY aicode_rate_limits_api_tenant ON public.aicode_rate_limits
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

DROP POLICY IF EXISTS aicode_memory_worker_all ON public.aicode_memory;
CREATE POLICY aicode_memory_worker_all ON public.aicode_memory
    FOR ALL TO rusa_worker_runtime
    USING (true)
    WITH CHECK (true);

DROP POLICY IF EXISTS aicode_audit_worker_all ON public.aicode_audit_events;
CREATE POLICY aicode_audit_worker_all ON public.aicode_audit_events
    FOR ALL TO rusa_worker_runtime
    USING (true)
    WITH CHECK (true);

COMMIT;
