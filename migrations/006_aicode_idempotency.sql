-- Apply after migrations 001-005 as the database migration owner.
-- Store only a digest of the client Idempotency-Key; never persist the raw header.
BEGIN;

ALTER TABLE public.aicode_jobs
    ADD COLUMN IF NOT EXISTS idempotency_key_hash TEXT;
ALTER TABLE public.aicode_jobs
    ADD COLUMN IF NOT EXISTS request_hash TEXT;

DO $
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.aicode_jobs'::regclass
          AND conname = 'aicode_jobs_idempotency_key_hash_check'
    ) THEN
        ALTER TABLE public.aicode_jobs
            ADD CONSTRAINT aicode_jobs_idempotency_key_hash_check
            CHECK (
                idempotency_key_hash IS NULL
                OR idempotency_key_hash ~ '^[0-9a-f]{64}
-- PostgreSQL permits multiple NULLs while enforcing one key digest per tenant.
CREATE UNIQUE INDEX IF NOT EXISTS uq_aicode_jobs_tenant_idempotency_key_hash
    ON public.aicode_jobs (tenant_id, idempotency_key_hash);

COMMIT;

            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.aicode_jobs'::regclass
          AND conname = 'aicode_jobs_request_hash_check'
    ) THEN
        ALTER TABLE public.aicode_jobs
            ADD CONSTRAINT aicode_jobs_request_hash_check
            CHECK (request_hash IS NULL OR request_hash ~ '^[0-9a-f]{64}
-- PostgreSQL permits multiple NULLs while enforcing one key digest per tenant.
CREATE UNIQUE INDEX IF NOT EXISTS uq_aicode_jobs_tenant_idempotency_key_hash
    ON public.aicode_jobs (tenant_id, idempotency_key_hash);

COMMIT;
);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.aicode_jobs'::regclass
          AND conname = 'aicode_jobs_idempotency_pair_check'
    ) THEN
        ALTER TABLE public.aicode_jobs
            ADD CONSTRAINT aicode_jobs_idempotency_pair_check
            CHECK (
                (idempotency_key_hash IS NULL AND request_hash IS NULL)
                OR (idempotency_key_hash IS NOT NULL AND request_hash IS NOT NULL)
            );
    END IF;
END;
$;

-- NULL values preserve compatibility for tasks created before this migration.
-- PostgreSQL permits multiple NULLs while enforcing one key digest per tenant.
CREATE UNIQUE INDEX IF NOT EXISTS uq_aicode_jobs_tenant_idempotency_key_hash
    ON public.aicode_jobs (tenant_id, idempotency_key_hash);

COMMIT;
