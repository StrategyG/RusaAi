-- Platform Core 2.0 durable workflow state, task DAG, event log, outbox, and inbox.
-- Apply after 009_knowledge_embedding_384.sql on pgvector-enabled deployments.
-- Plain PostgreSQL test databases may skip optional pgvector migrations 007 and 009.
-- Existing aicode_jobs remains unchanged.
BEGIN;

CREATE TABLE IF NOT EXISTS public.rusa_workflow_runs (
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    workflow_id TEXT NOT NULL CHECK (length(workflow_id) BETWEEN 1 AND 128),
    run_id TEXT NOT NULL CHECK (length(run_id) BETWEEN 1 AND 128),
    domain TEXT NOT NULL CHECK (length(domain) BETWEEN 1 AND 64),
    objective TEXT NOT NULL CHECK (length(objective) BETWEEN 1 AND 8000),
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN (
            'queued', 'planning', 'running', 'validating', 'awaiting_approval',
            'reconciling', 'succeeded', 'failed', 'blocked', 'cancel_requested',
            'cancelled', 'expired'
        )),
    state_version BIGINT NOT NULL DEFAULT 1 CHECK (state_version > 0),
    deadline_at TIMESTAMPTZ NOT NULL,
    budget JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(budget) = 'object'),
    budget_consumed JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(budget_consumed) = 'object'),
    final_report_ref TEXT,
    final_artifact_digest TEXT,
    cancel_epoch BIGINT NOT NULL DEFAULT 0 CHECK (cancel_epoch >= 0),
    idempotency_key TEXT,
    created_by TEXT NOT NULL CHECK (length(created_by) BETWEEN 1 AND 128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, run_id),
    UNIQUE (tenant_id, workflow_id, run_id),
    CHECK (deadline_at > created_at)
);

ALTER TABLE public.rusa_workflow_runs
    ADD COLUMN IF NOT EXISTS final_artifact_digest TEXT;
DO $rusa$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='public.rusa_workflow_runs'::regclass
          AND conname='rusa_workflow_runs_final_artifact_digest_check'
    ) THEN
        ALTER TABLE public.rusa_workflow_runs
            ADD CONSTRAINT rusa_workflow_runs_final_artifact_digest_check
            CHECK (
                final_artifact_digest IS NULL
                OR final_artifact_digest ~ '^(sha256:)?[a-f0-9]{64}$'
            );
    END IF;
END;
$rusa$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_rusa_workflow_runs_idempotency
    ON public.rusa_workflow_runs(tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_rusa_workflow_runs_tenant_recent
    ON public.rusa_workflow_runs(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rusa_workflow_runs_state_deadline
    ON public.rusa_workflow_runs(state, deadline_at);

CREATE TABLE IF NOT EXISTS public.rusa_workflow_tasks (
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    workflow_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL CHECK (length(task_id) BETWEEN 1 AND 128),
    parent_task_id TEXT,
    domain TEXT NOT NULL CHECK (length(domain) BETWEEN 1 AND 64),
    agent_id TEXT NOT NULL CHECK (length(agent_id) BETWEEN 1 AND 128),
    agent_version TEXT NOT NULL CHECK (length(agent_version) BETWEEN 1 AND 64),
    capability TEXT NOT NULL CHECK (length(capability) BETWEEN 1 AND 128),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN (
            'queued', 'running', 'awaiting_approval', 'succeeded', 'failed',
            'blocked', 'cancelled', 'skipped', 'expired'
        )),
    phase TEXT NOT NULL DEFAULT 'received'
        CHECK (phase IN (
            'received', 'observing', 'planning', 'executing', 'verifying',
            'awaiting_approval', 'succeeded', 'failed', 'blocked', 'cancelled'
        )),
    state_version BIGINT NOT NULL DEFAULT 1 CHECK (state_version > 0),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 100),
    priority SMALLINT NOT NULL DEFAULT 5 CHECK (priority BETWEEN 0 AND 9),
    available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    deadline_at TIMESTAMPTZ NOT NULL,
    budget JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(budget) = 'object'),
    input JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(input) = 'object'),
    input_refs JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(input_refs) = 'array'),
    result JSONB,
    result_ref TEXT,
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(evidence_refs) = 'array'),
    side_effect_status TEXT NOT NULL DEFAULT 'NONE',
    lease_owner TEXT,
    lease_epoch BIGINT NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
    lease_until TIMESTAMPTZ,
    idempotency_key TEXT,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, task_id),
    UNIQUE (tenant_id, workflow_id, run_id, task_id),
    FOREIGN KEY (tenant_id, workflow_id, run_id)
        REFERENCES public.rusa_workflow_runs(tenant_id, workflow_id, run_id) ON DELETE RESTRICT,
    CHECK (deadline_at > created_at),
    CHECK (
        (status = 'running' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
        OR (status <> 'running' AND lease_owner IS NULL AND lease_until IS NULL)
    )
);

ALTER TABLE public.rusa_workflow_tasks
    ADD COLUMN IF NOT EXISTS side_effect_status TEXT NOT NULL DEFAULT 'NONE';
DO $rusa$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='public.rusa_workflow_tasks'::regclass
          AND conname='rusa_workflow_tasks_side_effect_status_check'
    ) THEN
        ALTER TABLE public.rusa_workflow_tasks
            ADD CONSTRAINT rusa_workflow_tasks_side_effect_status_check
            CHECK (side_effect_status IN ('NONE', 'APPLIED', 'UNKNOWN', 'RECONCILED'));
    END IF;
END;
$rusa$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_rusa_workflow_tasks_idempotency
    ON public.rusa_workflow_tasks(tenant_id, run_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_rusa_workflow_tasks_claim
    ON public.rusa_workflow_tasks(status, available_at, priority DESC, created_at)
    WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS idx_rusa_workflow_tasks_run
    ON public.rusa_workflow_tasks(tenant_id, run_id, status);
CREATE INDEX IF NOT EXISTS idx_rusa_workflow_tasks_agent
    ON public.rusa_workflow_tasks(tenant_id, agent_id, agent_version, capability);

CREATE TABLE IF NOT EXISTS public.rusa_workflow_dependencies (
    tenant_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    depends_on_task_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, run_id, task_id, depends_on_task_id),
    FOREIGN KEY (tenant_id, workflow_id, run_id, task_id)
        REFERENCES public.rusa_workflow_tasks(tenant_id, workflow_id, run_id, task_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, workflow_id, run_id, depends_on_task_id)
        REFERENCES public.rusa_workflow_tasks(tenant_id, workflow_id, run_id, task_id)
        ON DELETE RESTRICT,
    CHECK (task_id <> depends_on_task_id)
);
CREATE INDEX IF NOT EXISTS idx_rusa_workflow_dependencies_upstream
    ON public.rusa_workflow_dependencies(tenant_id, run_id, depends_on_task_id);

CREATE TABLE IF NOT EXISTS public.rusa_workflow_events (
    tenant_id TEXT NOT NULL,
    event_id UUID NOT NULL,
    workflow_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT,
    schema_version TEXT NOT NULL CHECK (length(schema_version) BETWEEN 1 AND 16),
    event_type TEXT NOT NULL CHECK (length(event_type) BETWEEN 2 AND 128),
    producer_id TEXT NOT NULL CHECK (length(producer_id) BETWEEN 1 AND 128),
    producer_version TEXT NOT NULL CHECK (length(producer_version) BETWEEN 1 AND 64),
    message_id TEXT NOT NULL CHECK (length(message_id) BETWEEN 1 AND 128),
    correlation_id TEXT NOT NULL CHECK (length(correlation_id) BETWEEN 1 AND 128),
    causation_id TEXT,
    traceparent TEXT,
    sequence BIGINT CHECK (sequence IS NULL OR sequence > 0),
    occurred_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, event_id),
    UNIQUE (tenant_id, message_id),
    FOREIGN KEY (tenant_id, workflow_id, run_id)
        REFERENCES public.rusa_workflow_runs(tenant_id, workflow_id, run_id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rusa_workflow_events_run_sequence
    ON public.rusa_workflow_events(tenant_id, run_id, sequence)
    WHERE sequence IS NOT NULL AND task_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_rusa_workflow_events_task_sequence
    ON public.rusa_workflow_events(tenant_id, run_id, task_id, sequence)
    WHERE sequence IS NOT NULL AND task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_rusa_workflow_events_run_time
    ON public.rusa_workflow_events(tenant_id, run_id, occurred_at, event_id);
CREATE INDEX IF NOT EXISTS idx_rusa_workflow_events_type_time
    ON public.rusa_workflow_events(tenant_id, event_type, occurred_at DESC);

CREATE TABLE IF NOT EXISTS public.rusa_workflow_outbox (
    tenant_id TEXT NOT NULL,
    event_id UUID NOT NULL,
    destination TEXT NOT NULL DEFAULT 'workflow.events'
        CHECK (length(destination) BETWEEN 1 AND 128),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'publishing', 'published', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    locked_by TEXT,
    locked_until TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, event_id),
    FOREIGN KEY (tenant_id, event_id)
        REFERENCES public.rusa_workflow_events(tenant_id, event_id) ON DELETE RESTRICT,
    CHECK ((locked_by IS NULL) = (locked_until IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_rusa_workflow_outbox_pending
    ON public.rusa_workflow_outbox(available_at, created_at)
    WHERE status IN ('pending', 'publishing');

CREATE TABLE IF NOT EXISTS public.rusa_workflow_inbox (
    tenant_id TEXT NOT NULL,
    consumer_name TEXT NOT NULL CHECK (length(consumer_name) BETWEEN 1 AND 128),
    message_id TEXT NOT NULL CHECK (length(message_id) BETWEEN 1 AND 128),
    event_id UUID,
    event_type TEXT NOT NULL CHECK (length(event_type) BETWEEN 2 AND 128),
    processed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    result JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(result) = 'object'),
    PRIMARY KEY (tenant_id, consumer_name, message_id)
);
CREATE INDEX IF NOT EXISTS idx_rusa_workflow_inbox_event
    ON public.rusa_workflow_inbox(tenant_id, event_id)
    WHERE event_id IS NOT NULL;

-- API access is tenant-scoped. Trusted workers may claim tasks and relay outbox
-- records across tenants, but every mutation must still include tenant and lease fencing.
DO $rusa$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'rusa_workflow_runs', 'rusa_workflow_tasks',
        'rusa_workflow_dependencies', 'rusa_workflow_events',
        'rusa_workflow_outbox', 'rusa_workflow_inbox'
    ] LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC, rusa_runtime, rusa_api_runtime, rusa_worker_runtime', table_name);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE ON public.%I TO rusa_api_runtime', table_name);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE ON public.%I TO rusa_worker_runtime', table_name);
    END LOOP;
END;
$rusa$;

-- Immutable facts may be appended and read, but never rewritten by runtime roles.
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE
    public.rusa_workflow_dependencies,
    public.rusa_workflow_events,
    public.rusa_workflow_inbox
FROM rusa_api_runtime, rusa_worker_runtime;

DROP POLICY IF EXISTS rusa_workflow_runs_api_tenant ON public.rusa_workflow_runs;
CREATE POLICY rusa_workflow_runs_api_tenant ON public.rusa_workflow_runs
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
DROP POLICY IF EXISTS rusa_workflow_runs_worker_tenant ON public.rusa_workflow_runs;
DROP POLICY IF EXISTS rusa_workflow_runs_worker_queue ON public.rusa_workflow_runs;
CREATE POLICY rusa_workflow_runs_worker_queue ON public.rusa_workflow_runs
    FOR ALL TO rusa_worker_runtime
    USING (true)
    WITH CHECK (true);

DROP POLICY IF EXISTS rusa_workflow_tasks_api_tenant ON public.rusa_workflow_tasks;
CREATE POLICY rusa_workflow_tasks_api_tenant ON public.rusa_workflow_tasks
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
DROP POLICY IF EXISTS rusa_workflow_tasks_worker_queue ON public.rusa_workflow_tasks;
CREATE POLICY rusa_workflow_tasks_worker_queue ON public.rusa_workflow_tasks
    FOR ALL TO rusa_worker_runtime
    USING (true)
    WITH CHECK (true);

DROP POLICY IF EXISTS rusa_workflow_dependencies_api_tenant ON public.rusa_workflow_dependencies;
CREATE POLICY rusa_workflow_dependencies_api_tenant ON public.rusa_workflow_dependencies
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
DROP POLICY IF EXISTS rusa_workflow_dependencies_worker_tenant ON public.rusa_workflow_dependencies;
DROP POLICY IF EXISTS rusa_workflow_dependencies_worker_queue ON public.rusa_workflow_dependencies;
CREATE POLICY rusa_workflow_dependencies_worker_queue ON public.rusa_workflow_dependencies
    FOR ALL TO rusa_worker_runtime
    USING (true)
    WITH CHECK (true);

DROP POLICY IF EXISTS rusa_workflow_events_api_tenant ON public.rusa_workflow_events;
CREATE POLICY rusa_workflow_events_api_tenant ON public.rusa_workflow_events
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
DROP POLICY IF EXISTS rusa_workflow_events_worker_tenant ON public.rusa_workflow_events;
CREATE POLICY rusa_workflow_events_worker_tenant ON public.rusa_workflow_events
    FOR ALL TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

DROP POLICY IF EXISTS rusa_workflow_outbox_api_tenant ON public.rusa_workflow_outbox;
CREATE POLICY rusa_workflow_outbox_api_tenant ON public.rusa_workflow_outbox
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
DROP POLICY IF EXISTS rusa_workflow_outbox_worker_relay ON public.rusa_workflow_outbox;
CREATE POLICY rusa_workflow_outbox_worker_relay ON public.rusa_workflow_outbox
    FOR ALL TO rusa_worker_runtime
    USING (true)
    WITH CHECK (true);

DROP POLICY IF EXISTS rusa_workflow_inbox_api_tenant ON public.rusa_workflow_inbox;
CREATE POLICY rusa_workflow_inbox_api_tenant ON public.rusa_workflow_inbox
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
DROP POLICY IF EXISTS rusa_workflow_inbox_worker_tenant ON public.rusa_workflow_inbox;
CREATE POLICY rusa_workflow_inbox_worker_tenant ON public.rusa_workflow_inbox
    FOR ALL TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

-- Events, dependency edges, and inbox receipts are immutable facts.
-- Delivery state alone is mutable in the outbox table.
CREATE OR REPLACE FUNCTION public.rusa_workflow_event_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;
REVOKE ALL ON FUNCTION public.rusa_workflow_event_immutable() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_rusa_workflow_dependencies_immutable ON public.rusa_workflow_dependencies;
CREATE TRIGGER trg_rusa_workflow_dependencies_immutable
    BEFORE UPDATE OR DELETE ON public.rusa_workflow_dependencies
    FOR EACH ROW EXECUTE FUNCTION public.rusa_workflow_event_immutable();
DROP TRIGGER IF EXISTS trg_rusa_workflow_dependencies_no_truncate ON public.rusa_workflow_dependencies;
CREATE TRIGGER trg_rusa_workflow_dependencies_no_truncate
    BEFORE TRUNCATE ON public.rusa_workflow_dependencies
    FOR EACH STATEMENT EXECUTE FUNCTION public.rusa_workflow_event_immutable();

DROP TRIGGER IF EXISTS trg_rusa_workflow_inbox_immutable ON public.rusa_workflow_inbox;
CREATE TRIGGER trg_rusa_workflow_inbox_immutable
    BEFORE UPDATE OR DELETE ON public.rusa_workflow_inbox
    FOR EACH ROW EXECUTE FUNCTION public.rusa_workflow_event_immutable();
DROP TRIGGER IF EXISTS trg_rusa_workflow_inbox_no_truncate ON public.rusa_workflow_inbox;
CREATE TRIGGER trg_rusa_workflow_inbox_no_truncate
    BEFORE TRUNCATE ON public.rusa_workflow_inbox
    FOR EACH STATEMENT EXECUTE FUNCTION public.rusa_workflow_event_immutable();

DROP TRIGGER IF EXISTS trg_rusa_workflow_events_immutable ON public.rusa_workflow_events;
CREATE TRIGGER trg_rusa_workflow_events_immutable
    BEFORE UPDATE OR DELETE ON public.rusa_workflow_events
    FOR EACH ROW EXECUTE FUNCTION public.rusa_workflow_event_immutable();
DROP TRIGGER IF EXISTS trg_rusa_workflow_events_no_truncate ON public.rusa_workflow_events;
CREATE TRIGGER trg_rusa_workflow_events_no_truncate
    BEFORE TRUNCATE ON public.rusa_workflow_events
    FOR EACH STATEMENT EXECUTE FUNCTION public.rusa_workflow_event_immutable();

COMMIT;
