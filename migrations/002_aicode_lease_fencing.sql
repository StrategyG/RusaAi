-- Apply after stopping/draining all AICode workers and before deploying lease-fenced workers.
-- Each claim receives an unpredictable lease token. Worker state transitions must present it.
BEGIN;

ALTER TABLE public.aicode_jobs
    ADD COLUMN IF NOT EXISTS lease_token UUID;

-- Invalidate pre-upgrade leases. Old workers must be stopped before this migration.
UPDATE public.aicode_jobs
SET status='queued',
    lease_owner=NULL,
    lease_until=NULL,
    lease_token=NULL,
    updated_at=NOW()
WHERE status='running';

COMMIT;
