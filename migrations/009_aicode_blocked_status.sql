-- Preserve policy-blocked work as a terminal state until a human explicitly retries it.
BEGIN;

ALTER TABLE public.aicode_jobs
    DROP CONSTRAINT IF EXISTS aicode_jobs_status_check;

ALTER TABLE public.aicode_jobs
    ADD CONSTRAINT aicode_jobs_status_check
    CHECK (status IN (
        'queued',
        'running',
        'awaiting_approval',
        'succeeded',
        'failed',
        'blocked',
        'cancelled'
    ));

COMMIT;
