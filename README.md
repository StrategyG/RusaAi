# RUSA-AI

RUSA-AI is an AI model and agent platform under active development. The repository provides an authenticated local-model inference API, A2A task validation, and an enterprise hierarchical coding-agent runtime.

## Verified scope and status

The complete RUSA-AI product is not production-certified until a real model artifact, provider, GitHub credential, PostgreSQL deployment, target hardware benchmarks, security review, and operational release gates are supplied and verified. The repository does not claim trained model weights or measured production SLOs.

## Enterprise AICode Agent

The agent control plane is a separate service from model inference. It uses a hierarchical master orchestrator and six specialist workers: Analyst, Architect, Developer, Debugger, Tester, and Security.

Capabilities within the allowlist:
- Read and list regular text files in the task repository.
- Create or reuse an isolated work branch.
- Write approved repository files on that branch.
- Open or reuse a draft pull request.
- Read GitHub Actions checks and perform bounded repair cycles.
- Persist tenant-scoped task state, episodic memory, and audit evidence.

The agent does not expose arbitrary shell execution, unrestricted network access, secret retrieval, force push, merge, deployment, release, permission changes, or workflow changes. GitHub Actions provides the isolated test/build execution environment. Human review and protected-branch controls govern merge and release.

See [Enterprise Agent Architecture](docs/architecture/RUSA_AI_ENTERPRISE_AGENT.md) and [Security and Operations](docs/architecture/SECURITY_AND_OPERATIONS.md).

## Repository layout

- llm-project/model/: model architecture blueprint; not trained weights.
- llm-project/serve/: inference API and local Transformers adapter.
- rusa_agents/: contracts, policy, provider, GitHub tools, queue, memory, audit, orchestrator, task API, and worker process.
- schemas/a2a/: A2A task-envelope JSON Schema.
- docs/architecture/: agent architecture, worker contract, and security/operations model.
- tests/: API, contract, policy, orchestration, and security regression tests.

## Requirements

- Python 3.11 for the supported container and CI baseline.
- A compatible local causal language model directory for the inference API.
- A structured-output-capable OpenAI-compatible model provider for the agent workers.
- PostgreSQL 16 or compatible for task queue and memory.
- A repository-scoped GitHub App or fine-grained token with minimum contents, pull-request, checks, and metadata permissions.
- Docker Compose is optional for local orchestration.

## Local setup

1. Create and activate a Python virtual environment.
2. Install dependencies: python -m pip install -r requirements-dev.txt
3. Copy .env.example to .env and generate unique secrets. Do not commit .env.
4. Configure RUSA_MODEL_PATH and RUSA_MODEL_DIR for the inference API.
5. Configure the agent database password, per-tenant API key map, provider, GitHub token, and audit HMAC key.
6. Start the services with docker compose up --build -d.

The inference API reports not-ready and returns HTTP 503 until a real local model is loaded. The agent API accepts tasks at POST /v1/agent/tasks and returns task status at GET /v1/agent/tasks/{task_id}. Both require X-API-Key; the agent API also requires X-Tenant-ID and validates the key against RUSA_AGENT_TENANT_KEYS. Set Idempotency-Key on task submission to prevent duplicate tasks.

## Quality gates

Run:
- ruff check llm-project/serve rusa_agents tests
- python -m compileall -q llm-project rusa_agents tests
- pytest -q
- pip-audit -r requirements.txt -r requirements-agent.txt

CI also builds both the inference API image and the agent runtime image, then smoke-tests the inference API container.

## Production acceptance

A green CI run validates code checks for that commit. It does not prove model quality, model capacity, multi-tenant isolation under production load, security certification, or production SLOs. Before release, configure TLS and distributed rate limiting at an API gateway, egress allowlists, secret rotation, PostgreSQL encryption/backups/PITR, immutable remote audit retention, tenant isolation tests, prompt-injection red-team tests, staging soak tests, monitoring, alerting, rollback, and human approval for merge/deployment.

See docs/architecture/RUSA_AI_ENTERPRISE_AGENT.md for the complete architecture and acceptance model.
