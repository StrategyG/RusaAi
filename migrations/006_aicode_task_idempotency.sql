-- Tenant-scoped idempotency for task submissions. Apply after 005_aicode_task_policy_audit.sql.
BEGIN;

ALTER TABLE public.aicode_jobs
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE public.aicode_jobs
    ADD COLUMN IF NOT EXISTS request_hash TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.aicode_jobs'::regclass
          AND conname = 'aicode_jobs_idempotency_key_check'
    ) THEN
        ALTER TABLE public.aicode_jobs
            ADD CONSTRAINT aicode_jobs_idempotency_key_check
            CHECK (
                idempotency_key IS NULL
                OR idempotency_key ~ '^[A-Za-z0-9._:-]{1,128}$'
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.aicode_jobs'::regclass
          AND conname = 'aicode_jobs_request_hash_check'
    ) THEN
        ALTER TABLE public.aicode_jobs
            ADD CONSTRAINT aicode_jobs_request_hash_check
            CHECK (
                request_hash IS NULL
                OR request_hash ~ '^[0-9a-f]{64}$'
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
                (idempotency_key IS NULL AND request_hash IS NULL)
                OR (idempotency_key IS NOT NULL AND request_hash IS NOT NULL)
            );
    END IF;
END;
$$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_aicode_jobs_tenant_idempotency
    ON public.aicode_jobs(tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

COMMIT;
