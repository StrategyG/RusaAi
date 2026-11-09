-- Cooperative cancellation for running AICode workers.
-- Running jobs keep their lease until the worker acknowledges cancellation.
BEGIN;

ALTER TABLE public.aicode_jobs
    ADD COLUMN IF NOT EXISTS cancel_epoch BIGINT NOT NULL DEFAULT 0
    CHECK (cancel_epoch >= 0);

ALTER TABLE public.aicode_jobs
    DROP CONSTRAINT IF EXISTS aicode_jobs_status_check;

ALTER TABLE public.aicode_jobs
    ADD CONSTRAINT aicode_jobs_status_check
    CHECK (status IN (
        'queued',
        'running',
        'cancel_requested',
        'awaiting_approval',
        'succeeded',
        'failed',
        'blocked',
        'cancelled'
    ));

CREATE INDEX IF NOT EXISTS idx_aicode_jobs_cancel_requested
    ON public.aicode_jobs (lease_until)
    WHERE status = 'cancel_requested';

COMMIT;
