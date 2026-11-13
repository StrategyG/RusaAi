-- Atomic version fencing for Agent Runtime DAG checkpoints.
-- Apply as a database/schema owner. Existing task-memory rows start at version 0;
-- every compare-and-set checkpoint write advances the version exactly once.

ALTER TABLE public.aicode_memory
    ADD COLUMN IF NOT EXISTS checkpoint_version BIGINT NOT NULL DEFAULT 0
    CHECK (checkpoint_version >= 0);

COMMENT ON COLUMN public.aicode_memory.checkpoint_version IS
    'Monotonic compare-and-set version for tenant-scoped task memory and Agent Runtime checkpoints.';
