-- RUSA-AI user workspace identities and rotating browser sessions.
BEGIN;

CREATE TABLE IF NOT EXISTS public.rusa_users (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    email TEXT NOT NULL,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'admin')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_rusa_users_tenant_email
ON rusa_users (tenant_id, lower(email));

CREATE INDEX IF NOT EXISTS idx_rusa_users_tenant_created
ON rusa_users (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.rusa_sessions (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES rusa_users(id) ON DELETE CASCADE,
    refresh_token_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_rusa_sessions_user
ON rusa_sessions (user_id, expires_at DESC);

CREATE TABLE IF NOT EXISTS public.rusa_auth_login_attempts (
    bucket_key TEXT PRIMARY KEY,
    window_started_at TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts > 0)
);

GRANT USAGE ON SCHEMA public TO rusa_web_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.rusa_users TO rusa_web_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.rusa_sessions TO rusa_web_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.rusa_auth_login_attempts TO rusa_web_api;

COMMIT;
