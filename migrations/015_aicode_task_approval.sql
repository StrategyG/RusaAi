-- Permit audited tenant/platform administrator decisions to approve gated DAG nodes.
-- Apply after migrations/005_aicode_task_policy_audit.sql.
BEGIN;

ALTER TABLE public.aicode_task_policy_events
    DROP CONSTRAINT IF EXISTS aicode_task_policy_events_action_check;

ALTER TABLE public.aicode_task_policy_events
    ADD CONSTRAINT aicode_task_policy_events_action_check
    CHECK (
        action IN (
            'task.authenticate', 'task.create', 'task.read',
            'task.retry', 'task.cancel', 'task.approve', 'task.execute'
        )
    );

COMMIT;
