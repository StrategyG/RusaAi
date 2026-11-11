# Agent contract

The current task-envelope contract remains in schemas/a2a/task-envelope.schema.json.
The AICode-specific typed lifecycle and worker result objects remain in
rusa_agents/contracts.py. This folder marks the target shared-contract
ownership boundary; it does not duplicate those existing definitions.

The ten-agent core runtime and eight DevOps lifecycle contracts are not yet
implemented. Add new versioned schemas here only when the corresponding
agent identity, state transitions, permissions, evidence, and compatibility
rules are defined and covered by tests.
