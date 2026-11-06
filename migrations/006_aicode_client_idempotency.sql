-- Make client retries idempotent per tenant without conflating different request bodies.
-- Apply after migrations 001-005 as the schema owner.
BEGIN;

ALTER TABLE public.aicode_jobs
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT,
    ADD COLUMN IF NOT EXISTS request_hash TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'aicode_jobs_idempotency_pair_check'
          AND conrelid = 'public.aicode_jobs'::regclass
    ) THEN
        ALTER TABLE public.aicode_jobs
            ADD CONSTRAINT aicode_jobs_idempotency_pair_check
            CHECK (
                (idempotency_key IS NULL AND request_hash IS NULL)
                OR (
                    idempotency_key IS NOT NULL
                    AND length(idempotency_key) BETWEEN 1 AND 128
                    AND request_hash ~ '^[0-9a-f]{64}$'
                )
            ) NOT VALID;
    END IF;
END;
$$;

ALTER TABLE public.aicode_jobs
    VALIDATE CONSTRAINT aicode_jobs_idempotency_pair_check;

CREATE UNIQUE INDEX IF NOT EXISTS uq_aicode_jobs_tenant_idempotency
    ON public.aicode_jobs (tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

COMMIT;
