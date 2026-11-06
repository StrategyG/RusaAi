BEGIN;

ALTER TABLE public.aicode_jobs
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

DO $rusa$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.aicode_jobs
        WHERE idempotency_key IS NOT NULL
        GROUP BY tenant_id, idempotency_key
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION 'duplicate tenant idempotency keys exist; resolve them before applying migration';
    END IF;
END;
$rusa$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_aicode_jobs_idempotency
    ON public.aicode_jobs (tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

COMMIT;
