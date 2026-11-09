-- Add explicit, separately audited human approval and rejection decisions.
-- Apply after migrations/008_aicode_task_list_audit.sql.
BEGIN;

ALTER TABLE public.aicode_task_policy_events
    DROP CONSTRAINT IF EXISTS aicode_task_policy_events_action_check;

ALTER TABLE public.aicode_task_policy_events
    ADD CONSTRAINT aicode_task_policy_events_action_check
    CHECK (
        action IN (
            'task.authenticate', 'task.create', 'task.read', 'task.list',
            'task.retry', 'task.approve', 'task.reject', 'task.cancel',
            'task.execute'
        )
    );

COMMIT;
