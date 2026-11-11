-- Durable, process-independent inference admission for the API gateway.
-- Apply after migrations 001-009. The scheduler stores no prompts or tenant data;
-- it stores only request IDs, status, slot ownership, and expiry timestamps.
BEGIN;

DO $rusa$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rusa_api_runtime') THEN
        RAISE EXCEPTION 'rusa_api_runtime is missing; apply migrations/004_aicode_tenant_rls.sql first';
    END IF;
END;
$rusa$;

CREATE TABLE IF NOT EXISTS public.rusa_inference_admission_config (
    singleton_id SMALLINT PRIMARY KEY CHECK (singleton_id = 1),
    max_active SMALLINT NOT NULL CHECK (max_active BETWEEN 1 AND 256),
    max_waiting INTEGER NOT NULL CHECK (max_waiting BETWEEN 0 AND 1024),
    lease_seconds INTEGER NOT NULL CHECK (lease_seconds BETWEEN 5 AND 3600),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

INSERT INTO public.rusa_inference_admission_config
    (singleton_id, max_active, max_waiting, lease_seconds)
VALUES (1, 1, 3, 30)
ON CONFLICT (singleton_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS public.rusa_inference_admission_requests (
    request_id UUID PRIMARY KEY,
    status TEXT NOT NULL CHECK (
        status IN ('waiting', 'active', 'released', 'cancelled', 'timed_out', 'expired')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    queue_expires_at TIMESTAMPTZ NOT NULL,
    lease_owner UUID,
    slot_id SMALLINT,
    error_code TEXT,
    CHECK (
        (status = 'active' AND lease_owner IS NOT NULL AND slot_id IS NOT NULL)
        OR
        (status <> 'active' AND lease_owner IS NULL AND slot_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_rusa_inference_admission_waiting
    ON public.rusa_inference_admission_requests(created_at, request_id)
    WHERE status = 'waiting';

CREATE INDEX IF NOT EXISTS idx_rusa_inference_admission_history
    ON public.rusa_inference_admission_requests(updated_at)
    WHERE status IN ('released', 'cancelled', 'timed_out', 'expired');

CREATE TABLE IF NOT EXISTS public.rusa_inference_admission_slots (
    slot_id SMALLINT PRIMARY KEY CHECK (slot_id BETWEEN 0 AND 255),
    active_request_id UUID UNIQUE
        REFERENCES public.rusa_inference_admission_requests(request_id),
    lease_owner UUID,
    lease_expires_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        (active_request_id IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL)
        OR
        (active_request_id IS NOT NULL AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    )
);

INSERT INTO public.rusa_inference_admission_slots(slot_id)
VALUES (0)
ON CONFLICT (slot_id) DO NOTHING;

-- This is a platform-wide capacity table with no tenant payload. Only the API
-- runtime role may read or mutate it; the worker reaches it only through API.
REVOKE ALL ON public.rusa_inference_admission_config,
              public.rusa_inference_admission_requests,
              public.rusa_inference_admission_slots
    FROM PUBLIC, rusa_api_runtime, rusa_worker_runtime;

GRANT USAGE ON SCHEMA public TO rusa_api_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON public.rusa_inference_admission_config,
       public.rusa_inference_admission_requests,
       public.rusa_inference_admission_slots
    TO rusa_api_runtime;

COMMIT;
