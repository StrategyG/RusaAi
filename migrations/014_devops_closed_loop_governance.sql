-- Phase 5 durable incident correlation, lifecycle, and execution kill switches.
-- Apply after 013_devops_task_leases_and_cancellation.sql on the platform database.
BEGIN;

CREATE TABLE IF NOT EXISTS public.rusa_incidents (
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    incident_id UUID NOT NULL,
    fingerprint TEXT NOT NULL CHECK (fingerprint ~ '^[a-f0-9]{64}$'),
    service_id TEXT NOT NULL CHECK (length(service_id) BETWEEN 1 AND 128),
    environment TEXT NOT NULL CHECK (environment IN ('development', 'eval', 'staging', 'production')),
    rule_id TEXT NOT NULL CHECK (length(rule_id) BETWEEN 1 AND 128),
    severity TEXT NOT NULL CHECK (severity IN ('info', 'low', 'medium', 'high', 'critical')),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'acknowledged', 'investigating', 'mitigating', 'resolved', 'closed')),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 1000),
    summary TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 4000),
    correlation_id TEXT NOT NULL CHECK (length(correlation_id) BETWEEN 1 AND 128),
    workflow_id TEXT CHECK (workflow_id IS NULL OR length(workflow_id) BETWEEN 1 AND 128),
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK (occurrence_count > 0),
    latest_evidence_ref TEXT NOT NULL CHECK (
        latest_evidence_ref ~ '^(artifact|evidence)://sha256/[a-f0-9]{64}$'
    ),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    state_version BIGINT NOT NULL DEFAULT 1 CHECK (state_version > 0),
    acknowledged_by TEXT,
    resolved_by TEXT,
    resolved_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, incident_id),
    UNIQUE (tenant_id, fingerprint),
    CHECK (last_seen_at >= first_seen_at),
    CHECK ((resolved_by IS NULL) = (resolved_at IS NULL)),
    CHECK ((status IN ('resolved', 'closed')) = (resolved_at IS NOT NULL)),
    CHECK ((status = 'closed') = (closed_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_rusa_incidents_tenant_status_recent
    ON public.rusa_incidents(tenant_id, status, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_rusa_incidents_service_status
    ON public.rusa_incidents(tenant_id, service_id, environment, status);

CREATE TABLE IF NOT EXISTS public.rusa_closed_loop_inbox (
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    consumer_name TEXT NOT NULL CHECK (length(consumer_name) BETWEEN 1 AND 128),
    message_id TEXT NOT NULL CHECK (length(message_id) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL CHECK (length(fingerprint) BETWEEN 1 AND 128),
    subject_id TEXT,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, consumer_name, message_id)
);
CREATE INDEX IF NOT EXISTS idx_rusa_closed_loop_inbox_processed
    ON public.rusa_closed_loop_inbox(tenant_id, processed_at DESC);

CREATE TABLE IF NOT EXISTS public.rusa_incident_events (
    tenant_id TEXT NOT NULL,
    event_id UUID NOT NULL,
    incident_id UUID NOT NULL,
    message_id TEXT NOT NULL CHECK (length(message_id) BETWEEN 1 AND 128),
    correlation_id TEXT NOT NULL CHECK (length(correlation_id) BETWEEN 1 AND 128),
    event_type TEXT NOT NULL CHECK (length(event_type) BETWEEN 1 AND 128),
    actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 128),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, event_id),
    UNIQUE (tenant_id, message_id),
    FOREIGN KEY (tenant_id, incident_id)
        REFERENCES public.rusa_incidents(tenant_id, incident_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_rusa_incident_events_timeline
    ON public.rusa_incident_events(tenant_id, incident_id, occurred_at, event_id);

CREATE TABLE IF NOT EXISTS public.rusa_kill_switches (
    tenant_id TEXT NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    scope TEXT NOT NULL CHECK (
        scope IN ('all', 'workflow', 'devops', 'plan', 'code', 'build', 'test',
                  'release', 'deploy', 'operate', 'monitor', 'model', 'tool')
    ),
    enabled BOOLEAN NOT NULL,
    reason_code TEXT NOT NULL CHECK (length(reason_code) BETWEEN 1 AND 128),
    updated_by TEXT NOT NULL CHECK (length(updated_by) BETWEEN 1 AND 128),
    version BIGINT NOT NULL CHECK (version > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, scope)
);
CREATE INDEX IF NOT EXISTS idx_rusa_kill_switches_enabled
    ON public.rusa_kill_switches(tenant_id, scope)
    WHERE enabled=true;

CREATE TABLE IF NOT EXISTS public.rusa_kill_switch_events (
    tenant_id TEXT NOT NULL,
    event_id UUID NOT NULL,
    message_id TEXT NOT NULL CHECK (length(message_id) BETWEEN 1 AND 128),
    scope TEXT NOT NULL,
    enabled BOOLEAN NOT NULL,
    reason_code TEXT NOT NULL,
    actor TEXT NOT NULL,
    version BIGINT NOT NULL CHECK (version > 0),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, event_id),
    UNIQUE (tenant_id, message_id),
    FOREIGN KEY (tenant_id, scope)
        REFERENCES public.rusa_kill_switches(tenant_id, scope) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_rusa_kill_switch_events_timeline
    ON public.rusa_kill_switch_events(tenant_id, scope, occurred_at DESC);

DO $rusa$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'rusa_incidents', 'rusa_closed_loop_inbox', 'rusa_incident_events',
        'rusa_kill_switches', 'rusa_kill_switch_events'
    ] LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format(
            'REVOKE ALL ON public.%I FROM PUBLIC, rusa_runtime, rusa_api_runtime, rusa_worker_runtime',
            table_name
        );
    END LOOP;
END;
$rusa$;

GRANT SELECT, INSERT, UPDATE ON public.rusa_incidents TO rusa_api_runtime, rusa_worker_runtime;
GRANT SELECT, INSERT, UPDATE ON public.rusa_closed_loop_inbox TO rusa_api_runtime, rusa_worker_runtime;
GRANT SELECT, INSERT ON public.rusa_incident_events TO rusa_api_runtime, rusa_worker_runtime;
GRANT SELECT, INSERT, UPDATE ON public.rusa_kill_switches TO rusa_api_runtime;
GRANT SELECT ON public.rusa_kill_switches TO rusa_worker_runtime;
GRANT SELECT, INSERT ON public.rusa_kill_switch_events TO rusa_api_runtime, rusa_worker_runtime;

REVOKE DELETE, TRUNCATE ON TABLE
    public.rusa_incident_events,
    public.rusa_kill_switch_events
FROM rusa_api_runtime, rusa_worker_runtime;

DROP POLICY IF EXISTS rusa_incidents_api_tenant ON public.rusa_incidents;
CREATE POLICY rusa_incidents_api_tenant ON public.rusa_incidents
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
DROP POLICY IF EXISTS rusa_incidents_worker_tenant ON public.rusa_incidents;
CREATE POLICY rusa_incidents_worker_tenant ON public.rusa_incidents
    FOR ALL TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

DROP POLICY IF EXISTS rusa_closed_loop_inbox_api_tenant ON public.rusa_closed_loop_inbox;
CREATE POLICY rusa_closed_loop_inbox_api_tenant ON public.rusa_closed_loop_inbox
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
DROP POLICY IF EXISTS rusa_closed_loop_inbox_worker_tenant ON public.rusa_closed_loop_inbox;
CREATE POLICY rusa_closed_loop_inbox_worker_tenant ON public.rusa_closed_loop_inbox
    FOR ALL TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

DROP POLICY IF EXISTS rusa_incident_events_api_tenant ON public.rusa_incident_events;
CREATE POLICY rusa_incident_events_api_tenant ON public.rusa_incident_events
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
DROP POLICY IF EXISTS rusa_incident_events_worker_tenant ON public.rusa_incident_events;
CREATE POLICY rusa_incident_events_worker_tenant ON public.rusa_incident_events
    FOR ALL TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

DROP POLICY IF EXISTS rusa_kill_switches_api_read ON public.rusa_kill_switches;
CREATE POLICY rusa_kill_switches_api_read ON public.rusa_kill_switches
    FOR SELECT TO rusa_api_runtime
    USING (
        tenant_id = NULLIF(current_setting('rusa.tenant_id', true), '')
        OR tenant_id = 'platform'
    );
DROP POLICY IF EXISTS rusa_kill_switches_api_write ON public.rusa_kill_switches;
CREATE POLICY rusa_kill_switches_api_write ON public.rusa_kill_switches
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
DROP POLICY IF EXISTS rusa_kill_switches_worker_read ON public.rusa_kill_switches;
CREATE POLICY rusa_kill_switches_worker_read ON public.rusa_kill_switches
    FOR SELECT TO rusa_worker_runtime
    USING (
        tenant_id = NULLIF(current_setting('rusa.tenant_id', true), '')
        OR tenant_id = 'platform'
    );

DROP POLICY IF EXISTS rusa_kill_switch_events_api_tenant ON public.rusa_kill_switch_events;
CREATE POLICY rusa_kill_switch_events_api_tenant ON public.rusa_kill_switch_events
    FOR ALL TO rusa_api_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));
DROP POLICY IF EXISTS rusa_kill_switch_events_worker_tenant ON public.rusa_kill_switch_events;
CREATE POLICY rusa_kill_switch_events_worker_tenant ON public.rusa_kill_switch_events
    FOR ALL TO rusa_worker_runtime
    USING (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('rusa.tenant_id', true), ''));

COMMIT;
