# Agent–Worker Runtime hardening — implementation notes

- **Repository:** `StrategyG/RusaAi`
- **Baseline default branch:** `main` at `1adac8d340e5870570910168b6626fe285dd2cf2`
- **Implementation branch:** `feat/agent-worker-runtime-hardening-20261115`
- **Status:** PARTIAL pending exact-head CI and review; production NOT CERTIFIED.
- **Scope:** harden the existing mainline AICode queue/worker path. This does not import or duplicate the unmerged DAG runtime in PR #116.

## Mainline audit summary

The baseline already contains:

- PostgreSQL-backed `aicode_jobs` with durable status, attempts, lease owner/token/expiry, result and idempotency key.
- `FOR UPDATE SKIP LOCKED` claims and lease-token-fenced heartbeat, completion, approval pause, failure, cancellation, and repository publication.
- Tenant-scoped API routes, task-policy events, RLS migrations 004–005, separate API/worker runtime role groups, and an HMAC-chained audit ledger.
- Role-based Tool Gateway allowlists, protected path checks, bounded file sizes, Docker sandbox profiles, read-only sandbox snapshots, and no arbitrary shell tool.
- Tenant/project-scoped knowledge retrieval through the RAG service.
- Task timeout, worker heartbeat, and cancellation of the orchestrator when the lease is lost.

The baseline did not bound pending tasks per tenant or active queue claims across replicas. Legacy task-memory writes and Tool Gateway workspace writes did not hold the current queue lease row lock. Actual tool invocations were not written to the HMAC audit ledger. Sandbox subprocesses did not receive the lease-revocation event. Generic retry also accepted `awaiting_approval`.

PR #116 (`feature/agent-runtime-v1`) is an open, unmerged Agent Runtime/DAG candidate. Its exact head `faf686ac85875a930221ed92a357a9520a5947e5` has successful checks for Test/lint/security, RAG/MCP integration, and production image build/scan. That result applies to PR #116's head only; it is not evidence that the DAG runtime is in `main`.

## Changes on this branch

| Area | Change | Compatibility / safety |
|---|---|---|
| Tenant queue admission | Adds `RUSA_MAX_PENDING_TASKS_PER_TENANT` with a tenant advisory lock and counts queued/running/approval-paused tasks. | No schema change. Saturation is HTTP 429 with `Retry-After`. |
| Idempotency | Resolves identical tenant/key/payload replay before capacity checks. | Existing key and payload semantics retained. |
| Global active claims | Adds `RUSA_MAX_ACTIVE_TASKS` and a global transaction advisory lock around active-lease count + claim. | No schema change; one conservative active task by default. |
| Lease fencing | Adds `with_lease_owned` for short local/database operations. | No model inference or network calls are allowed under the queue row lock. |
| Legacy memory | Adds `LeaseBoundMemory` and uses it in the claimed worker path. | Existing `PostgresMemory` API remains unchanged. |
| Tool Gateway | Adds lease pre-checks, row-lock fencing for developer file writes, and HMAC `tool.intent`/`tool.outcome` records. | Audit metadata only; no raw arguments, file contents, tool output, or secrets. |
| Cancellation | Adds configurable heartbeat interval and propagates lease loss to DockerSandbox, which removes the named container. | Cooperative cancellation; other I/O still relies on bounded operation timeouts. |
| Retry | Adds explicit `retryable` classification and bounded delay; generic retry accepts only failed tasks. | Approval-pending tasks require a separate approval workflow; mainline currently lacks a plan-fingerprint-bound resume API. |
| API / deployment | Maps queue saturation to HTTP 429; documents and passes new settings through production Compose. | No database migration or credential/permission changes. |

## Required configuration

- `RUSA_MAX_PENDING_TASKS_PER_TENANT=1000` — set identically on every API replica.
- `RUSA_MAX_ACTIVE_TASKS=1` — set identically on every worker replica.
- `RUSA_LEASE_HEARTBEAT_INTERVAL_SECONDS=10` — set to 1–60 seconds.
- `RUSA_MAX_PARALLEL_WORKERS` remains the in-task specialist concurrency setting; it is not the global active-task cap.

## Verification commands

The repository CI workflow is the authoritative execution environment for this branch. It runs the following commands on the exact pull-request head:

```bash
python -m compileall -q rusa_agents rusa_knowledge rusa_mcp llm-project/serve tests
ruff check rusa_agents rusa_knowledge rusa_mcp tests llm-project/serve
bandit -q -r rusa_agents rusa_knowledge rusa_mcp --severity-level medium --confidence-level medium
python llm-project/train/validate_dataset.py \
  --train-file llm-project/data/curated/train.jsonl \
  --eval-file llm-project/data/curated/eval.jsonl
python -m pytest -vv -o faulthandler_timeout=60
```

The RAG/MCP CI job additionally compiles/lints those packages and runs the knowledge and MCP test modules. The image job builds API, worker, sandbox, and RAG/MCP images, runs the full test suite in the isolated sandbox, and scans images with Trivy. No result is claimed for this branch until the exact head has completed those jobs.

## Known gaps

1. The current mainline legacy AICode path has no plan-fingerprint-bound approval-resume API. Generic retry is now failed-only; approval-paused tasks need operator handling.
2. Lease checks on read-only tools are point-in-time checks. Workspace writes and memory operations are row-lock fenced; long-running read/check operations rely on cancellation and their own timeouts.
3. Cancellation of the Docker sandbox is implemented, but not every external I/O primitive is forcibly interruptible. Verify the full cancellation path in CI and a live worker drill.
4. The database role/RLS contract has repository migrations and tests, but production credentials/roles have not been adversarially verified against the target deployment.
5. There is no production load/soak result, backup/restore drill, rollback drill, external immutable audit-retention proof, or operator sign-off in this run.
6. The Agent Runtime/DAG code in PR #116 remains separate and unmerged. Do not report it as deployed or production-ready.

## Deployment rule

No migration is required. Keep task intake disabled while rolling out the API and worker images to every replica. Ensure all replicas use consistent limits, run the PostgreSQL race tests and sandbox cancellation smoke tests, and require operator approval before re-enabling intake. Rollback is an image rollback with intake disabled; do not modify or delete audit history.
