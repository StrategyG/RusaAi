-- Durable, append-only task authorization events. Apply after 004_aicode_tenant_rls.sql.
BEGIN;

CREATE TABLE IF NOT EXISTS public.aicode_task_policy_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    tenant_id TEXT,
    task_id TEXT,
    actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 128),
    action TEXT NOT NULL CHECK (
        action IN (
            'task.authenticate', 'task.create', 'task.read',
            'task.retry', 'task.cancel', 'task.execute'
        )
    ),
    decision TEXT NOT NULL CHECK (decision IN ('allow', 'deny')),
    CHECK (tenant_id IS NOT NULL OR (
        actor = 'unauthenticated'
        AND action = 'task.authenticate'
        AND decision = 'deny'
    )),
    CHECK (tenant_id IS NULL OR length(tenant_id) BETWEEN 1 AND 128),
    CHECK (task_id IS NULL OR length(task_id) BETWEEN 1 AND 128)
);

CREATE INDEX IF NOT EXISTS idx_aicode_task_policy_events_tenant_time
    ON public.aicode_task_policy_events(tenant_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_aicode_task_policy_events_task_time
    ON public.aicode_task_policy_events(task_id, occurred_at DESC);

ALTER TABLE public.aicode_task_policy_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aicode_task_policy_events FORCE ROW LEVEL SECURITY;

REVOKE ALL ON public.aicode_task_policy_events
    FROM PUBLIC, rusa_runtime, rusa_api_runtime, rusa_worker_runtime;
REVOKE ALL ON SEQUENCE public.aicode_task_policy_events_id_seq
    FROM PUBLIC, rusa_runtime, rusa_api_runtime, rusa_worker_runtime;

GRANT USAGE ON SCHEMA public TO rusa_api_runtime, rusa_worker_runtime;
GRANT INSERT ON public.aicode_task_policy_events
    TO rusa_api_runtime, rusa_worker_runtime;
GRANT USAGE, SELECT ON SEQUENCE public.aicode_task_policy_events_id_seq
    TO rusa_api_runtime, rusa_worker_runtime;

DROP POLICY IF EXISTS aicode_task_policy_events_api_insert
    ON public.aicode_task_policy_events;
CREATE POLICY aicode_task_policy_events_api_insert
    ON public.aicode_task_policy_events
    FOR INSERT TO rusa_api_runtime
    WITH CHECK (
        tenant_id = NULLIF(current_setting('rusa.tenant_id', true), '')
        OR (
            tenant_id IS NULL
            AND actor = 'unauthenticated'
            AND action = 'task.authenticate'
            AND decision = 'deny'
        )
    );

DROP POLICY IF EXISTS aicode_task_policy_events_worker_insert
    ON public.aicode_task_policy_events;
CREATE POLICY aicode_task_policy_events_worker_insert
    ON public.aicode_task_policy_events
    FOR INSERT TO rusa_worker_runtime
    WITH CHECK (
        tenant_id = NULLIF(current_setting('rusa.tenant_id', true), '')
    );

CREATE OR REPLACE FUNCTION public.rusa_task_policy_event_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'aicode_task_policy_events is append-only';
END;
$$;

REVOKE ALL ON FUNCTION public.rusa_task_policy_event_immutable() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_aicode_task_policy_events_immutable
    ON public.aicode_task_policy_events;
CREATE TRIGGER trg_aicode_task_policy_events_immutable
    BEFORE UPDATE OR DELETE ON public.aicode_task_policy_events
    FOR EACH ROW EXECUTE FUNCTION public.rusa_task_policy_event_immutable();

DROP TRIGGER IF EXISTS trg_aicode_task_policy_events_no_truncate
    ON public.aicode_task_policy_events;
CREATE TRIGGER trg_aicode_task_policy_events_no_truncate
    BEFORE TRUNCATE ON public.aicode_task_policy_events
    FOR EACH STATEMENT EXECUTE FUNCTION public.rusa_task_policy_event_immutable();

COMMIT;
