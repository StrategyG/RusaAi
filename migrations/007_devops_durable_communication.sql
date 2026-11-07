-- Durable DevOps workflow state and transactional message delivery.
-- Apply after 006_aicode_idempotency.sql. Keep AICode's existing task queue intact.
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rusa_relay_runtime') THEN
        CREATE ROLE rusa_relay_runtime NOLOGIN NOBYPASSRLS;
    END IF;
END;
$$;
ALTER ROLE rusa_relay_runtime NOLOGIN NOSUPERUSER NOBYPASSRLS;

CREATE TABLE IF NOT EXISTS public.devops_workflows (
    run_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 256),
    request_sha256 TEXT NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'needs_approval', 'completed',
                          'failed', 'blocked', 'cancelled')),
    state_version BIGINT NOT NULL DEFAULT 1 CHECK (state_version > 0),
    request JSONB NOT NULL,
    result_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_devops_workflows_tenant_created
    ON public.devops_workflows(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.devops_tasks (
    task_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES public.devops_workflows(run_id),
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    agent_id TEXT NOT NULL CHECK (length(agent_id) BETWEEN 1 AND 128),
    stage TEXT NOT NULL CHECK (length(stage) BETWEEN 1 AND 64),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'accepted', 'running', 'validating',
                          'needs_approval', 'completed', 'failed', 'blocked', 'cancelled')),
    state_version BIGINT NOT NULL DEFAULT 1 CHECK (state_version > 0),
    lease_epoch BIGINT NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
    deadline_at TIMESTAMPTZ,
    task JSONB NOT NULL,
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, task_id),
    UNIQUE (tenant_id, run_id, task_id, attempt)
);
CREATE INDEX IF NOT EXISTS idx_devops_tasks_run
    ON public.devops_tasks(tenant_id, run_id, created_at);

CREATE TABLE IF NOT EXISTS public.devops_event_outbox (
    outbox_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    run_id UUID NOT NULL,
    task_id UUID,
    attempt INTEGER CHECK (attempt IS NULL OR attempt > 0),
    message_id TEXT NOT NULL CHECK (length(message_id) BETWEEN 1 AND 128),
    destination TEXT NOT NULL CHECK (destination IN ('commands', 'events')),
    event_type TEXT NOT NULL CHECK (length(event_type) BETWEEN 1 AND 128),
    aggregate_version BIGINT NOT NULL CHECK (aggregate_version > 0),
    envelope JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'publishing', 'published', 'dead_letter')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    lease_token UUID,
    lease_until TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, message_id),
    CHECK (
        (status = 'publishing' AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
        OR (status <> 'publishing' AND lease_token IS NULL AND lease_until IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_devops_outbox_pending
    ON public.devops_event_outbox(destination, available_at, outbox_id)
    WHERE status IN ('pending', 'publishing');

CREATE TABLE IF NOT EXISTS public.devops_event_inbox (
    inbox_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    consumer_id TEXT NOT NULL CHECK (length(consumer_id) BETWEEN 1 AND 128),
    message_id TEXT NOT NULL CHECK (length(message_id) BETWEEN 1 AND 128),
    envelope_sha256 TEXT NOT NULL CHECK (envelope_sha256 ~ '^[a-f0-9]{64}$'),
    processed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, consumer_id, message_id)
);

CREATE TABLE IF NOT EXISTS public.devops_event_log (
    event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    run_id UUID NOT NULL,
    message_id TEXT NOT NULL CHECK (length(message_id) BETWEEN 1 AND 128),
    event_type TEXT NOT NULL CHECK (length(event_type) BETWEEN 1 AND 128),
    aggregate_version BIGINT NOT NULL CHECK (aggregate_version > 0),
    envelope JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, message_id),
    UNIQUE (tenant_id, run_id, aggregate_version)
);
CREATE INDEX IF NOT EXISTS idx_devops_event_log_run
    ON public.devops_event_log(tenant_id, run_id, aggregate_version);

-- Separate NOLOGIN role groups. Login identities are provisioned by deployment.
GRANT USAGE ON SCHEMA public TO rusa_api_runtime, rusa_worker_runtime, rusa_relay_runtime;
GRANT SELECT, INSERT, UPDATE ON public.devops_workflows TO rusa_api_runtime, rusa_worker_runtime;
GRANT SELECT, INSERT, UPDATE ON public.devops_tasks TO rusa_api_runtime, rusa_worker_runtime;
GRANT SELECT, INSERT ON public.devops_event_outbox TO rusa_api_runtime, rusa_worker_runtime;
GRANT SELECT, UPDATE ON public.devops_event_outbox TO rusa_relay_runtime;
GRANT USAGE, SELECT ON SEQUENCE public.devops_event_outbox_outbox_id_seq TO rusa_api_runtime, rusa_worker_runtime, rusa_relay_runtime;
GRANT SELECT, INSERT ON public.devops_event_inbox TO rusa_worker_runtime;
GRANT USAGE, SELECT ON SEQUENCE public.devops_event_inbox_inbox_id_seq TO rusa_worker_runtime;
GRANT SELECT, INSERT ON public.devops_event_log TO rusa_worker_runtime;
GRANT USAGE, SELECT ON SEQUENCE public.devops_event_log_event_id_seq TO rusa_worker_runtime;

ALTER TABLE public.devops_workflows ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.devops_workflows FORCE ROW LEVEL SECURITY;
ALTER TABLE public.devops_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.devops_tasks FORCE ROW LEVEL SECURITY;
ALTER TABLE public.devops_event_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.devops_event_outbox FORCE ROW LEVEL SECURITY;
ALTER TABLE public.devops_event_inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.devops_event_inbox FORCE ROW LEVEL SECURITY;
ALTER TABLE public.devops_event_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.devops_event_log FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS devops_workflows_tenant ON public.devops_workflows;
CREATE POLICY devops_workflows_tenant ON public.devops_workflows
    FOR ALL TO rusa_api_runtime, rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

DROP POLICY IF EXISTS devops_tasks_tenant ON public.devops_tasks;
CREATE POLICY devops_tasks_tenant ON public.devops_tasks
    FOR ALL TO rusa_api_runtime, rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

DROP POLICY IF EXISTS devops_outbox_tenant ON public.devops_event_outbox;
CREATE POLICY devops_outbox_tenant ON public.devops_event_outbox
    FOR INSERT TO rusa_api_runtime, rusa_worker_runtime
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

DROP POLICY IF EXISTS devops_outbox_relay ON public.devops_event_outbox;
CREATE POLICY devops_outbox_relay_select ON public.devops_event_outbox
    FOR SELECT TO rusa_relay_runtime USING (true);
CREATE POLICY devops_outbox_relay_update ON public.devops_event_outbox
    FOR UPDATE TO rusa_relay_runtime USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS devops_inbox_worker_tenant ON public.devops_event_inbox;
CREATE POLICY devops_inbox_worker_tenant ON public.devops_event_inbox
    FOR INSERT TO rusa_worker_runtime
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

DROP POLICY IF EXISTS devops_event_log_worker_tenant ON public.devops_event_log;
CREATE POLICY devops_event_log_worker_select ON public.devops_event_log
    FOR SELECT TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
CREATE POLICY devops_event_log_worker_insert ON public.devops_event_log
    FOR INSERT TO rusa_worker_runtime
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

-- Runtime roles may append but never mutate or delete canonical event history.
REVOKE UPDATE, DELETE, TRUNCATE ON public.devops_event_log FROM PUBLIC, rusa_api_runtime, rusa_worker_runtime;
REVOKE UPDATE, DELETE, TRUNCATE ON public.devops_event_inbox FROM PUBLIC, rusa_api_runtime, rusa_worker_runtime;

CREATE OR REPLACE FUNCTION public.rusa_devops_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'DevOps event history is append-only';
END;
$$;
REVOKE ALL ON FUNCTION public.rusa_devops_append_only() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_devops_event_log_immutable ON public.devops_event_log;
CREATE TRIGGER trg_devops_event_log_immutable
BEFORE UPDATE OR DELETE ON public.devops_event_log
FOR EACH ROW EXECUTE FUNCTION public.rusa_devops_append_only();
DROP TRIGGER IF EXISTS trg_devops_event_log_no_truncate ON public.devops_event_log;
CREATE TRIGGER trg_devops_event_log_no_truncate
BEFORE TRUNCATE ON public.devops_event_log
FOR EACH STATEMENT EXECUTE FUNCTION public.rusa_devops_append_only();

COMMIT;
