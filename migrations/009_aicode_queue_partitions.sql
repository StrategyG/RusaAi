-- Partition the shared durable task queue by worker capability.
-- Existing AICode jobs retain queue_name='aicode'; DevOps workers must claim queue_name='devops'.
BEGIN;

ALTER TABLE public.aicode_jobs
    ADD COLUMN queue_name TEXT NOT NULL DEFAULT 'aicode';

ALTER TABLE public.aicode_jobs
    ADD CONSTRAINT aicode_jobs_queue_name_check
    CHECK (queue_name ~ '^[a-z][a-z0-9_-]{0,31}$');

CREATE INDEX idx_aicode_jobs_queue_claim
    ON public.aicode_jobs(queue_name, status, available_at, priority DESC, created_at);

COMMIT;
