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
registry remains `draft_proposed_not_activated`. The 34 `RUSA-AW-*` values
are registry-local capability IDs, not corpus chunk IDs. A role-level
crosswalk references 24 existing `RUSA-KB-*` candidates from Batch 015 and
Batch 033; it creates zero new chunk content. The crosswalk status remains
`candidate_crosswalk_pending_review`.

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
match from draft text and always returns an empty `tool_grants` list. Before activation, the role-level crosswalk must pass technical/source-fidelity,
rights, exact/near-dedup and human review, then be marked `approved_for_routing`.
Batch 033 index-only overlaps KB-859, KB-860 and KB-862 are excluded from the
crosswalk. The 24 references are existing candidate IDs, not 24 newly created
or approved corpus units. Do not count the 34 routing IDs or the crosswalk
references as new corpus chunks. The Policy Engine and Tool Gateway remain the only authority for tool execution.

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

1. Requires a trusted tenant context, an approved source crosswalk, an active verified registry, an active delegating agent, and a handler bound to the same tenant for every selected role.
2. Calls an injected Policy Engine authorizer before each worker.
3. Stops for human approval on high/critical risk and production targets; proceeds only when the policy adapter confirms that the required approval is satisfied.
4. Enforces node deadlines around policy authorization, worker execution and independent verification, and runs one worker at a time on the CPU-first profile.
5. Calls an injected independent verifier before marking a node complete.
6. Persists a fingerprinted checkpoint after each node and supports resume.
7. Blocks dependants when an upstream node fails or is not completed.

Model calls made by handlers must continue through the Rusaai Model Access
Gateway. The Gateway, not this executor, enforces the single active inference
generation. Tools must continue through the Tool Gateway and sandbox. The
executor does not expose shell, network, filesystem, or secret APIs.

Use `TaskMemoryCheckpointStore` with the existing `SQLiteMemory` or
`PostgresMemory` implementation for checkpoint persistence. The in-memory store
is only for tests/development. For a queue-backed worker, wrap the durable store
with `LeaseBoundCheckpointStore`, passing the current queue job ID,
queue task ID, tenant ID, worker ID, and lease token. The adapter rejects a
checkpoint task or tenant that does not match the claimed job. Its
`PostgresTaskQueue.with_lease_owned()` callback holds the task row lock while the
bounded checkpoint read/write runs, so
`SKIP LOCKED` claimers cannot take over the job during that operation. This
guard is for short checkpoint I/O only, never model inference, Git operations,
or other network work.

Checkpoint persistence is versioned with the monotonic
`aicode_memory.checkpoint_version` column (migration
`014_aicode_memory_checkpoint_cas.sql`). Every DAG checkpoint write uses an
atomic compare-and-set against the version read at execution start or after the
previous successful write. A stale worker receives
`CheckpointConflictError` and cannot overwrite a newer checkpoint. The
PostgreSQL row-lock lease fence and checkpoint CAS are complementary: the
lease prevents takeover during the bounded I/O operation, while CAS prevents
lost updates across independent reads/writes and recovery attempts.

`WorkerExecutionEngine.execute()` accepts an optional cancellation event.
When it is set, the executor cancels an in-flight policy/worker/verifier
awaitable, marks the active and not-yet-started nodes cancelled, and does not
attempt a checkpoint write after queue cancellation or lease revocation. The
queue's durable cancelled/lease state remains authoritative. If the Python
execution coroutine itself is cancelled while the lease is still valid, the
executor attempts a final cancelled checkpoint; if the lease has already been
lost, that write is rejected and ignored rather than bypassing the fence.

Generic DAG plan intake/dispatch is still not connected to
`AICodeRuntime.run_once()`. Production Policy Engine/verifier adapters,
role/source-crosswalk approvals, handler-side idempotency for external side
effects, and a live PostgreSQL queue-to-DAG end-to-end acceptance test remain
mandatory before activating new Agent/Worker roles.

## API and worker-process integration

The existing API exposes two authenticated, read-only control-plane endpoints.
Both require the existing `task:read` permission and emit policy-audit events:

- `GET /v1/agent-runtime/status` returns registry status, role counts,
  dispatchable count, source-crosswalk status, and the execution-enabled flag.
- `POST /v1/agent-runtime/route-preview` accepts a scope plus explicit
  capability IDs or a role ID and returns candidate roles. It never dispatches
  a worker and always returns an empty `tool_grants` list.

- `POST /v1/agent-runtime/plan-preview` validates a proposed task DAG,
  including topological order, dependency edges, role/scope ownership, deadlines,
  DevOps change metadata, and approval-required nodes. Tenant identity is taken
  from the authenticated API principal; the request schema rejects a caller-
  supplied tenant. It never enqueues or executes the plan.

The production `AICodeRuntime.initialize()` loads the same packaged registry
before starting its queue and sandbox services. It validates that the existing
six-role AICode pipeline (`analyst`, `architect`, `developer`, `debugger`,
`tester`, `security`) is mapped to six registry entries of type
`aicode_worker`. This is a compatibility check only: the legacy AICode
orchestrator remains responsible for execution and its evidence/repair gates.
The registry's draft state does not block existing AICode tasks, and it does
not authorize any new role dispatch.

After role approval and adapter review, `AICodeRuntime.build_agent_execution_engine()`
composes the generic DAG engine with the existing `PostgresMemory` checkpoint
store and the active `Job` lease. It refuses a job owned by another worker and
uses `LeaseBoundCheckpointStore` so checkpoint I/O is fenced by that job's
lease. Policy authorization and independent result verification remain
explicit injected adapters; this method does not make the draft registry
dispatchable or add a queue task dispatcher.

The API route preview and worker startup checks share the same JSON package
data and JSON Schema validator. A missing or invalid registry causes the
corresponding control-plane operation to fail closed.

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
