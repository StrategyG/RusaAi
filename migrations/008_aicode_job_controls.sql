-- Durable out-of-band cancellation control and bounded operator retries.
-- Apply after the core AICode migrations 001-006 (and after 007 when using one ordered schema).
BEGIN;

ALTER TABLE public.aicode_jobs
    ADD COLUMN IF NOT EXISTS manual_retry_count INTEGER NOT NULL DEFAULT 0
        CHECK (manual_retry_count >= 0),
    ADD COLUMN IF NOT EXISTS max_manual_retries INTEGER NOT NULL DEFAULT 3
        CHECK (max_manual_retries >= 0);

CREATE TABLE IF NOT EXISTS public.aicode_job_controls (
    tenant_id TEXT NOT NULL,
    task_id UUID NOT NULL,
    cancel_epoch BIGINT NOT NULL DEFAULT 0 CHECK (cancel_epoch >= 0),
    cancel_requested_at TIMESTAMPTZ,
    requested_by TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, task_id),
    FOREIGN KEY (tenant_id, task_id)
        REFERENCES public.aicode_jobs(tenant_id, task_id) ON DELETE CASCADE
);

INSERT INTO public.aicode_job_controls (tenant_id, task_id)
SELECT tenant_id, task_id
FROM public.aicode_jobs
ON CONFLICT (tenant_id, task_id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_aicode_job_controls_cancel_requested
    ON public.aicode_job_controls(updated_at)
    WHERE cancel_requested_at IS NOT NULL;

ALTER TABLE public.aicode_job_controls ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aicode_job_controls FORCE ROW LEVEL SECURITY;

REVOKE ALL ON public.aicode_job_controls
    FROM PUBLIC, rusa_runtime, rusa_api_runtime, rusa_worker_runtime;

GRANT USAGE ON SCHEMA public TO rusa_api_runtime, rusa_worker_runtime;
GRANT SELECT, INSERT, UPDATE ON public.aicode_job_controls TO rusa_api_runtime;
GRANT SELECT ON public.aicode_job_controls TO rusa_worker_runtime;

DROP POLICY IF EXISTS aicode_job_controls_api_tenant
    ON public.aicode_job_controls;
CREATE POLICY aicode_job_controls_api_tenant
    ON public.aicode_job_controls
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

DROP POLICY IF EXISTS aicode_job_controls_worker_read
    ON public.aicode_job_controls;
CREATE POLICY aicode_job_controls_worker_read
    ON public.aicode_job_controls
    FOR SELECT TO rusa_worker_runtime
    USING (true);

COMMIT;
