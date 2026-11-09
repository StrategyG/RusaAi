-- Explicit human reconciliation of external side effects after cooperative cancellation.
BEGIN;

ALTER TABLE public.aicode_task_policy_events
    DROP CONSTRAINT IF EXISTS aicode_task_policy_events_action_check;

ALTER TABLE public.aicode_task_policy_events
    ADD CONSTRAINT aicode_task_policy_events_action_check
    CHECK (action IN (
        'task.authenticate',
        'task.create',
        'task.read',
        'task.retry',
        'task.cancel',
        'task.execute',
        'task.reconcile'
    ));

COMMIT;
