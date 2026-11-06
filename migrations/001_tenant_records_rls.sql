-- Run as a dedicated database migration administrator, never as rusa_app.
-- The application role must remain NOSUPERUSER, NOBYPASSRLS and must not own this table.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rusa_app') THEN
        CREATE ROLE rusa_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS public.tenant_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    owner_subject TEXT NOT NULL CHECK (length(owner_subject) BETWEEN 1 AND 128),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.tenant_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_records FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'tenant_records'
          AND policyname = 'tenant_records_isolation'
    ) THEN
        CREATE POLICY tenant_records_isolation ON public.tenant_records
            AS PERMISSIVE
            FOR ALL
            TO rusa_app
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
    END IF;
END
$$;

REVOKE ALL ON public.tenant_records FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO rusa_app;
GRANT SELECT, INSERT ON public.tenant_records TO rusa_app;
