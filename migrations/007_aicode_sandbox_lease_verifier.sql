-- Minimal lease-verification capability for the isolated sandbox executor.
-- Apply after migrations 001-006 as a database/schema administrator.
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rusa_sandbox_runtime') THEN
        CREATE ROLE rusa_sandbox_runtime NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rusa_sandbox_guard') THEN
        CREATE ROLE rusa_sandbox_guard NOLOGIN NOSUPERUSER BYPASSRLS;
    END IF;
END;
$$;

ALTER ROLE rusa_sandbox_runtime NOLOGIN NOSUPERUSER NOBYPASSRLS;
ALTER ROLE rusa_sandbox_guard NOLOGIN NOSUPERUSER BYPASSRLS;

GRANT USAGE ON SCHEMA public TO rusa_sandbox_runtime, rusa_sandbox_guard;

-- The guard role can read only the queue table, and cannot log in or be assumed
-- by either application runtime. It owns one SECURITY DEFINER boolean function.
GRANT SELECT ON public.aicode_jobs TO rusa_sandbox_guard;

CREATE OR REPLACE FUNCTION public.rusa_verify_sandbox_lease(
    p_task_id UUID,
    p_tenant_id TEXT,
    p_worker_id TEXT,
    p_lease_token UUID
)
RETURNS BOOLEAN
LANGUAGE SQL
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM public.aicode_jobs AS job
        WHERE job.task_id = p_task_id
          AND job.tenant_id = p_tenant_id
          AND job.lease_owner = p_worker_id
          AND job.lease_token = p_lease_token
          AND job.status = 'running'
          AND job.lease_until > clock_timestamp()
    )
$function$;

ALTER FUNCTION public.rusa_verify_sandbox_lease(UUID, TEXT, TEXT, UUID)
    OWNER TO rusa_sandbox_guard;
REVOKE ALL ON FUNCTION public.rusa_verify_sandbox_lease(UUID, TEXT, TEXT, UUID)
    FROM PUBLIC, rusa_runtime, rusa_api_runtime, rusa_worker_runtime;
GRANT EXECUTE ON FUNCTION public.rusa_verify_sandbox_lease(UUID, TEXT, TEXT, UUID)
    TO rusa_sandbox_runtime;

-- The sandbox login receives no direct access to job rows or other tenant data.
REVOKE ALL ON public.aicode_jobs FROM PUBLIC, rusa_sandbox_runtime;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM rusa_sandbox_runtime;

COMMIT;

-- Create the login out of band using a secret manager:
-- CREATE ROLE rusa_sandbox_login LOGIN PASSWORD 'managed-secret' IN ROLE rusa_sandbox_runtime;
-- GRANT CONNECT ON DATABASE rusa TO rusa_sandbox_login;
