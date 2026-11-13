# RusaAi Agent Runtime v1.0

## Scope

This change adds a staged, reusable Control Plane runtime layer for the 10 core
agents, 6 AICode worker roles, and 8 DevOps lifecycle profiles. It reuses the
existing RusaAi queue, task memory, AICode worker contracts, Tool Gateway,
Policy Engine, sandbox, and audit boundary. It does not create another model
runtime or replace the existing AICode orchestrator.

## Components

| Component | Responsibility |
|---|---|
| `capability_registry.py` | Load and validate the versioned role manifest and enforce activation state |
| `capability_router.py` | Match explicit registry-local capability IDs to candidate roles |
| `task_decomposer.py` | Validate task contracts, dependencies, parent hierarchy, and DAG ordering |
| `worker_execution.py` | Execute one worker at a time, enforce policy/approval gates, verify results, checkpoint, and resume |
| `runtime_v1.py` | Compose the registry, router, decomposer, and executor |

## Registry status

The checked-in registry is a compact runtime projection of the reviewed
registry proposal. It contains 10 core agents, 6 AICode workers, and 8 DevOps
lifecycle profiles. Every role remains `not_verified` / disabled and the
registry remains `draft_proposed_not_activated`.

Therefore:

- Registry validation and route preview are available.
- A preview may return `suggested_role_id`, but never a dispatchable
  `selected_role_id`.
- The executor rejects the draft registry.
- No role receives tools or permissions from registry membership.
- No chunk is approved, embedded, indexed, or published by this change.

Do not change the registry to `active` until role owners approve the contracts,
runtime handlers exist, policy and verification adapters are reviewed, and
release authorization is recorded.

## Routing

Routing is deliberately deterministic in v1.0. Callers provide explicit
registry-local capability IDs (`RUSA-AW-*`) or an explicit role ID. These
identifiers are routing keys, not canonical knowledge chunk IDs. At this
revision, 34 provisional capability IDs exist but **zero canonical RUSA-KB
source chunk references are linked**. The router does not infer a semantic
match from draft text and always returns an empty `tool_grants` list. Before
activation, each capability must be reconciled to approved canonical source
chunk IDs and exact source locators. Do not count these routing IDs as corpus
chunks. The Policy Engine and Tool Gateway remain the only authority for tool execution.

Example:

```python
from rusa_agents.runtime_v1 import AgentRuntimeV1
from rusa_agents.capability_router import RouteRequest

runtime = AgentRuntimeV1()
decision = runtime.preview_route(
    RouteRequest(
        scope="worker",
        required_capability_ids=("RUSA-AW-WORKER-001",),
        identity_type="aicode_worker",
    )
)
print(decision.status, decision.suggested_role_id)
# PREVIEW_ONLY analyst
```

## Task contract and DAG

Each node carries a stable task ID, selected role, scope, delegating agent,
objective, acceptance criteria, dependencies, workspace reference, optional
deadline, bounded retry count, requested tools/permissions, risk class, and
context references.

DevOps lifecycle nodes additionally require change ID, target environment,
release/change version, lifecycle gate, and rollback/stop condition. Their
parent remains the `devops` core agent.

The decomposer checks role/scope consistency, parent ownership, dependency
references, cycles, deadlines, tool identifier syntax, retry bounds, and
approval-required risk. A model may propose a plan, but it cannot bypass this
deterministic validation.

## Execution and recovery

The executor:

1. Requires a trusted tenant context, an active verified registry, an active delegating agent, and a handler bound to the same tenant for every selected role.
2. Calls an injected Policy Engine authorizer before each worker.
3. Stops for human approval on high/critical risk and production targets; proceeds only when the policy adapter confirms that the required approval is satisfied.
4. Runs one worker at a time on the CPU-first profile.
5. Calls an injected independent verifier before marking a node complete.
6. Persists a fingerprinted checkpoint after each node and supports resume.
7. Blocks dependants when an upstream node fails or is not completed.

Model calls made by handlers must continue through the Rusaai Model Access
Gateway. The Gateway, not this executor, enforces the single active inference
generation. Tools must continue through the Tool Gateway and sandbox. The
executor does not expose shell, network, filesystem, or secret APIs.

Use `TaskMemoryCheckpointStore` with the existing `SQLiteMemory` or
`PostgresMemory` implementation for checkpoint persistence. The in-memory store
is only for tests/development. This executor is not yet wired to the durable
queue's claim/lease fencing, idempotency, cancellation and atomic checkpoint
compare-and-set. Those controls remain mandatory before activation; task-memory
persistence alone is not a worker lease.

## Existing AICode integration

The current `AICodeOrchestrator` remains the specialized six-worker coding
pipeline with its evidence gates and repair cycles. `AICodeWorkerAdapter`
allows an existing AICode worker to implement the generic DAG handler
contract after the role registry is approved. This change does not replace
the current worker process or activate the new registry.

## Validation

Run:

```bash
python -m rusa_agents.runtime_v1
python -m pytest tests/test_agent_runtime_v1.py -q
ruff check rusa_agents tests
```

The runtime registry is a development-stage contract. Production activation
still requires human approval, adapter review, security review, hardware
benchmarking, and an explicit release decision.
