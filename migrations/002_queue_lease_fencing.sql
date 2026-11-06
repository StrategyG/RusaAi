-- Add per-claim fencing tokens to prevent expired workers from mutating jobs.
-- Apply once as the schema owner before deploying the new worker version.
BEGIN;

ALTER TABLE public.aicode_jobs
    ADD COLUMN IF NOT EXISTS lease_token UUID;

COMMENT ON COLUMN public.aicode_jobs.lease_token IS
    'Unique token for the current lease claim; stale worker updates must match it.';

COMMIT;
