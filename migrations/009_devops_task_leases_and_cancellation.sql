-- Task lease ownership and idempotent workflow cancellation for Phase 3D.
-- This extends the durable communication foundation without changing AICode queues.
BEGIN;

ALTER TABLE public.devops_workflows
    DROP CONSTRAINT IF EXISTS devops_workflows_status_check;
ALTER TABLE public.devops_workflows
    ADD CONSTRAINT devops_workflows_status_check
    CHECK (status IN ('queued', 'running', 'needs_approval', 'cancel_requested',
                      'completed', 'failed', 'blocked', 'cancelled'));

ALTER TABLE public.devops_workflows
    ADD COLUMN IF NOT EXISTS cancel_epoch BIGINT NOT NULL DEFAULT 0
        CHECK (cancel_epoch >= 0),
    ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cancel_reason_code TEXT,
    ADD COLUMN IF NOT EXISTS cancel_idempotency_key TEXT,
    ADD COLUMN IF NOT EXISTS cancel_request_sha256 TEXT;

ALTER TABLE public.devops_workflows
    ADD CONSTRAINT devops_workflows_cancel_request_consistency
    CHECK (
        (cancel_epoch = 0 AND cancel_requested_at IS NULL
            AND cancel_reason_code IS NULL AND cancel_idempotency_key IS NULL
            AND cancel_request_sha256 IS NULL)
        OR
        (cancel_epoch > 0 AND cancel_requested_at IS NOT NULL
            AND cancel_reason_code IS NOT NULL AND cancel_idempotency_key IS NOT NULL
            AND cancel_request_sha256 ~ '^[a-f0-9]{64}$')
    );

ALTER TABLE public.devops_tasks
    DROP CONSTRAINT IF EXISTS devops_tasks_status_check;
ALTER TABLE public.devops_tasks
    ADD CONSTRAINT devops_tasks_status_check
    CHECK (status IN ('queued', 'accepted', 'running', 'validating',
                      'needs_approval', 'cancel_requested', 'completed',
                      'failed', 'blocked', 'cancelled', 'skipped'));

ALTER TABLE public.devops_tasks
    ADD COLUMN IF NOT EXISTS lease_owner TEXT,
    ADD COLUMN IF NOT EXISTS lease_claim_id TEXT,
    ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;

ALTER TABLE public.devops_tasks
    ADD COLUMN IF NOT EXISTS side_effect TEXT
        CHECK (side_effect IS NULL OR side_effect IN ('NONE', 'APPLIED', 'UNKNOWN'));

ALTER TABLE public.devops_tasks
    ADD CONSTRAINT devops_tasks_lease_consistency
    CHECK (
        (lease_owner IS NULL AND lease_claim_id IS NULL AND lease_until IS NULL)
        OR
        (lease_owner IS NOT NULL AND length(lease_owner) BETWEEN 1 AND 128
            AND lease_claim_id IS NOT NULL
            AND length(lease_claim_id) BETWEEN 1 AND 128
            AND lease_until IS NOT NULL)
    );

CREATE INDEX IF NOT EXISTS idx_devops_tasks_claimable
    ON public.devops_tasks(tenant_id, run_id, status, lease_until, deadline_at);

CREATE TABLE IF NOT EXISTS public.devops_run_cancellations (
    cancellation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    run_id TEXT NOT NULL CHECK (length(run_id) BETWEEN 1 AND 128),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 256),
    request_sha256 TEXT NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    cancel_epoch BIGINT NOT NULL CHECK (cancel_epoch > 0),
    requested_by TEXT NOT NULL CHECK (length(requested_by) BETWEEN 1 AND 128),
    reason_code TEXT NOT NULL CHECK (length(reason_code) BETWEEN 1 AND 128),
    status TEXT NOT NULL DEFAULT 'requested'
        CHECK (status IN ('requested', 'completed', 'reconciliation_required')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    UNIQUE (tenant_id, idempotency_key),
    UNIQUE (tenant_id, run_id),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES public.devops_workflows(tenant_id, run_id),
    CHECK ((status = 'completed') = (completed_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_devops_cancellations_tenant_created
    ON public.devops_run_cancellations(tenant_id, created_at DESC);

GRANT SELECT, INSERT, UPDATE ON public.devops_run_cancellations TO rusa_api_runtime;
GRANT USAGE, SELECT ON SEQUENCE public.devops_run_cancellations_cancellation_id_seq
    TO rusa_api_runtime;
GRANT SELECT ON public.devops_run_cancellations TO rusa_worker_runtime;
GRANT UPDATE (status, completed_at) ON public.devops_run_cancellations
    TO rusa_worker_runtime;

ALTER TABLE public.devops_run_cancellations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.devops_run_cancellations FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS devops_cancellations_api_tenant
    ON public.devops_run_cancellations;
CREATE POLICY devops_cancellations_api_tenant
    ON public.devops_run_cancellations
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

DROP POLICY IF EXISTS devops_cancellations_worker_tenant
    ON public.devops_run_cancellations;
CREATE POLICY devops_cancellations_worker_tenant
    ON public.devops_run_cancellations
    FOR SELECT TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

DROP POLICY IF EXISTS devops_cancellations_worker_update
    ON public.devops_run_cancellations;
CREATE POLICY devops_cancellations_worker_update
    ON public.devops_run_cancellations
    FOR UPDATE TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));


-- RPC message receipts make progress/result retries safe across lost responses.
CREATE TABLE IF NOT EXISTS public.devops_task_rpc_receipts (
    receipt_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    task_id TEXT NOT NULL CHECK (length(task_id) BETWEEN 1 AND 128),
    method TEXT NOT NULL CHECK (method IN ('ReportTaskProgress', 'ReportTaskResult')),
    message_id TEXT NOT NULL CHECK (length(message_id) BETWEEN 1 AND 128),
    request_sha256 TEXT NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}),
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, task_id, method, message_id),
    FOREIGN KEY (tenant_id, task_id)
        REFERENCES public.devops_tasks(tenant_id, task_id)
);

CREATE INDEX IF NOT EXISTS idx_devops_task_rpc_receipts_created
    ON public.devops_task_rpc_receipts(tenant_id, created_at DESC);

GRANT SELECT, INSERT ON public.devops_task_rpc_receipts TO rusa_worker_runtime;
GRANT USAGE, SELECT ON SEQUENCE public.devops_task_rpc_receipts_receipt_id_seq
    TO rusa_worker_runtime;

ALTER TABLE public.devops_task_rpc_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.devops_task_rpc_receipts FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS devops_task_rpc_receipts_worker_tenant
    ON public.devops_task_rpc_receipts;
CREATE POLICY devops_task_rpc_receipts_worker_tenant
    ON public.devops_task_rpc_receipts
    FOR ALL TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));


COMMIT;
