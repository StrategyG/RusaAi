-- Apply after migrations 001-005 as the database migration owner.
-- Store only a digest of the client Idempotency-Key; never persist the raw header.
BEGIN;

ALTER TABLE public.aicode_jobs
    ADD COLUMN IF NOT EXISTS idempotency_key_hash TEXT;
ALTER TABLE public.aicode_jobs
    ADD COLUMN IF NOT EXISTS request_hash TEXT;

-- NULL values preserve compatibility for tasks created before this migration.
-- PostgreSQL permits multiple NULLs while enforcing one key digest per tenant.
CREATE UNIQUE INDEX IF NOT EXISTS uq_aicode_jobs_tenant_idempotency_key_hash
    ON public.aicode_jobs (tenant_id, idempotency_key_hash);

COMMIT;
