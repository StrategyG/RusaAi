-- Preserve all append-only task audit actions after cancellation reconciliation.
-- Version 012 avoids colliding with the independent RAG embedding migration 008.
BEGIN;

ALTER TABLE public.aicode_task_policy_events
    DROP CONSTRAINT IF EXISTS aicode_task_policy_events_action_check;

ALTER TABLE public.aicode_task_policy_events
    ADD CONSTRAINT aicode_task_policy_events_action_check
    CHECK (action IN (
        'task.authenticate',
        'task.create',
        'task.read',
        'task.list',
        'task.retry',
        'task.cancel',
        'task.execute',
        'task.reconcile'
    ));

COMMIT;
