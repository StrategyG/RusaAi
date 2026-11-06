-- Durable result metadata returned by tenant-scoped task status API.
-- Apply after migrations 001 and 002.
BEGIN;
ALTER TABLE public.aicode_jobs
    ADD COLUMN IF NOT EXISTS result JSONB;
COMMIT;
