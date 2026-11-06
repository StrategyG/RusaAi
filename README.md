# RUSA-AI

RUSA-AI is an AI model and agent platform under active development. The repository provides an authenticated local-model inference API, A2A task validation, and an enterprise hierarchical coding-agent runtime.

## Verified scope and status

The complete RUSA-AI product is not production-certified until a real model artifact, provider, GitHub credential, PostgreSQL deployment, target hardware benchmarks, security review, and operational release gates are supplied and verified. The repository does not claim trained model weights or measured production SLOs.

## Enterprise AICode Agent

The agent control plane is a separate service from model inference. It uses a hierarchical master orchestrator and six specialist workers: Analyst, Architect, Developer, Debugger, Tester, and Security.

Capabilities within the configured access policy:
- Read and list regular text files in an explicitly allowlisted repository assigned to the authenticated tenant.
- Create or reuse a deterministic tenant/task-scoped work branch.
- Write approved repository files on that isolated branch.
- Open or reuse a draft pull request.
- Read complete, bounded GitHub Actions check-runs and perform at most two repair cycles by default.
- Review a complete, bounded GitHub compare diff and re-check CI against the same commit SHA.
- Run a second, read-only Security review after the diff and CI evidence have been retrieved.
- Persist tenant-scoped task state, fenced worker leases, episodic memory, and audit evidence.

The agent does not expose arbitrary shell execution, unrestricted network access, secret retrieval, force push, merge, deployment, release, permission changes, or workflow changes. GitHub Actions provides the isolated test/build execution environment. Human review and protected-branch controls govern merge and release.

See [Enterprise Agent Architecture](docs/architecture/RUSA_AI_ENTERPRISE_AGENT.md) and [Security and Operations](docs/architecture/SECURITY_AND_OPERATIONS.md).

## Repository layout

- llm-project/model/: model architecture blueprint; not trained weights.
- llm-project/serve/: inference API and local Transformers adapter.
- rusa_agents/: contracts, policy, provider, GitHub tools, queue, memory, audit, orchestrator, task API, and worker process.
- schemas/a2a/: A2A task-envelope JSON Schema.
- docs/architecture/: agent architecture, worker contract, and security/operations model.
- tests/: API, contract, policy, orchestration, HTTP-boundary, PostgreSQL, and security regression tests.

## Requirements

- Python 3.11 for the supported container and CI baseline.
- A compatible local causal language model directory for the inference API.
- A structured-output-capable OpenAI-compatible model provider for the agent workers.
- PostgreSQL 16 or compatible for task queue and memory.
- A repository-scoped GitHub App or fine-grained token with minimum contents, pull-request, checks, and metadata permissions.
- Docker Compose is optional for local orchestration.

## Local setup

1. Create and activate a Python virtual environment.
2. Install dependencies: python -m pip install -r requirements-dev.txt.
3. Copy .env.example to .env and generate unique secrets. Do not commit .env.
4. Configure RUSA_MODEL_PATH and RUSA_MODEL_DIR for the inference API.
5. Configure separate PostgreSQL owner/worker/API passwords, per-tenant API keys, provider credentials, GitHub credentials, audit HMAC key, and the global repository allowlist.
6. Configure RUSA_AGENT_TENANT_REPOSITORIES as a JSON map assigning each tenant only its permitted repositories. Every tenant in this map must have a unique API key and every assigned repository must also appear in the global allowlist.
7. Restrict the GitHub App or fine-grained token to the same global repository set. Do not grant administration, Actions write, secrets, environments, or deployment permissions.
8. Start the services with docker compose up --build -d. Compose runs the one-shot `rusa-agent-migrate` service before starting the agent API and worker; it creates the queue/memory schema and grants only the required task-table and lifecycle-function privileges.

The inference API reports not-ready and returns HTTP 503 until a real local model is loaded. The inference API binds to 127.0.0.1 by default; put it behind a TLS reverse proxy and change RUSA_BIND_ADDRESS only when binding to a deliberate private interface. All agent task endpoints require X-API-Key and X-Tenant-ID; the API validates the key against RUSA_AGENT_TENANT_KEYS and enforces each tenant's repository assignment.

| Method | Endpoint | Purpose |
|---|---|---|
| POST | /v1/agent/tasks | Submit an idempotent coding task |
| GET | /v1/agent/tasks/{task_id} | Read task status and checkpoint summary within the tenant |
| POST | /v1/agent/tasks/{task_id}/retry | Explicitly requeue a failed task |
| DELETE | /v1/agent/tasks/{task_id} | Cancel a queued, waiting, running, or approval-pending task and revoke its worker lease |

Cancellation and retry use tenant-checked database functions; the API role has no direct UPDATE or DELETE privilege on task tables. Cancellation clears the active lease token. The worker rechecks lease ownership immediately before each repository tool action and fails closed if the check is false or unavailable. Cancellation is cooperative: an external API request already in flight may finish, but no subsequent tool action or successful acknowledgement is permitted. An explicit tenant retry gives an eligible failed task a fresh attempt budget and keeps usable checkpoints. Automatic recovery counts each claim, including CI polling and crash recovery, against the configured maximum (four by default). Repeating the same submission with the same tenant and `Idempotency-Key` returns the original task ID until the terminal row is purged (30 days by default). Reusing the key with different input returns a conflict.

Before a repository mutation, the worker durably checkpoints an intent. It clears the intent when it checkpoints the completed worker stage and tool results. If the worker crashes or loses the provider response in between, recovery blocks the task and keeps the intent for investigation. This may also block a request that never reached the provider. Compare the provider's branch, commit history, file contents, or pull requests with the task and audit records before authorizing a new task. Leave the task blocked if the outcome cannot be established. Cancellation retains unresolved intents for investigation. Lease fencing governs PostgreSQL results. An external request already in flight can still commit.

Drain old workers before rolling out the new checkpoint format. Review provider state before resuming older unfinished tasks with repository writes, because their checkpoints do not record pending write intents.

Set Idempotency-Key on task submission to prevent duplicate tasks. Reusing the key with a different task payload is rejected. The agent generates the work branch itself under rusa-agent/{tenant-hash}/{request-hash}; clients must not select or reuse arbitrary branches. RUSA_AGENT_ALLOWED_REPOSITORIES is a comma-separated global owner/name allowlist. RUSA_AGENT_TENANT_REPOSITORIES assigns a subset of those repositories to each tenant; the API and GitHub gateway both enforce the tenant assignment. RUSA_AGENT_REQUIRED_CI_CHECKS is a JSON array of exact GitHub check-run names required for success.

Potential secrets are redacted from repository reads, memory, audit details, and durable checkpoints; secret-bearing file writes are blocked before GitHub access. The Security worker can review at most 20 changed files per task, with a bounded patch size per file. Larger or incomplete diffs are blocked for human review rather than silently truncated and treated as complete.

## Quality gates

Run:
- ruff check llm-project/serve rusa_agents tests
- python -m compileall -q llm-project rusa_agents tests
- pytest -q
- pip-audit -r requirements.txt -r requirements-agent.txt

When RUSA_TEST_DATABASE_URL is configured, PostgreSQL integration tests exercise idempotency, lease fencing, checkpoint ownership, and row-level security. CI provisions a dedicated PostgreSQL 16 service and runs those tests. CI builds both the inference API image and the agent runtime image, then smoke-tests the inference API container. Pull-request checks run regardless of the target branch so stacked PRs receive checks.

## Production acceptance

A green CI run validates code checks for that commit. It does not prove model quality, model capacity, multi-tenant isolation under production load, security certification, or production SLOs. Before release, configure TLS and distributed rate limiting at an API gateway, outbound egress allowlists, secret rotation, PostgreSQL encryption/backups/PITR, immutable remote audit retention, prompt-injection red-team tests, staging soak tests, monitoring, alerting, rollback, and human approval for merge/deployment.

The runtime foundation is not a production certification. A real provider, GitHub App, PostgreSQL deployment, model endpoint, model evaluation dataset, and target deployment environment must be configured and tested.
