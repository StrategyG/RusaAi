# RUSA-AI

RUSA-AI is an AI model and agent platform under active development. This repository provides a FastAPI serving foundation, a durable hierarchical agent runtime, an A2A task-envelope contract, a local Transformers adapter, and a responsive role-aware web workspace for users and administrators.

## Verified scope and status

The complete RUSA-AI product is NOT production-ready yet. The repository now contains an enterprise AICode runtime foundation (hierarchical workers, GitHub tool gateway, PostgreSQL task queue, tenant-scoped task memory, audit trail, and authenticated task intake). It has not yet passed complete end-to-end production validation. The repository also does not contain trained RUSA-AI model weights, a verified training pipeline, or production model benchmark evidence. The model artifact must be supplied and evaluated separately.

## Serving foundation

- FastAPI serving API with service API-key authentication and database-backed bearer sessions for human users.
- Local-only Hugging Face Transformers loading. Model files must already exist on disk; remote code execution and implicit downloads are disabled.
- OpenAI-style non-streaming chat completion response with token usage.
- A2A task-envelope validation using the schema-declared JSON Schema draft and format checks.
- Separate liveness and readiness endpoints.
- Request IDs, response security headers, request content-length guard, non-root container, and hardened Compose example.
- CI for lint, compile, tests, dependency audit, container build, and liveness smoke test.

Mock model responses are intentionally not supported. Inference returns HTTP 503 until a real local model is loaded. Streaming returns HTTP 501 until implemented.

## Repository layout

- llm-project/model/: model architecture blueprint; not trained weights.
- llm-project/serve/: API, user authentication, workspace/admin endpoints, and local inference adapter.
- apps/web/: React/TypeScript user workspace, admin control center, and same-origin Nginx gateway.
- schemas/a2a/: A2A task envelope JSON Schema.
- tests/: API, authentication, readiness, and schema tests.
- rusa_agents/: hierarchical coding-agent runtime, workers, GitHub gateway, queue, memory, and audit.
- AI_CODE.md: normative AICode operating and security contract.
- docs/: architecture, integration, and production-readiness documentation.

## Requirements

- Python 3.11 for the supported container and CI baseline.
- Node.js 22 and npm for the web workspace.
- A compatible local causal language model directory with a tokenizer chat template.
- Adequate RAM/VRAM for the selected model.
- A random API key of at least 32 characters.
- Docker Compose is optional.

## Local setup

1. Create and activate a Python virtual environment.
2. Install production dependencies with: python -m pip install -r requirements.txt
3. Install development dependencies with: python -m pip install -r requirements-dev.txt
4. Copy .env.example to .env, generate a real secret, and set RUSA_MODEL_PATH to a local model directory.
5. Run: uvicorn app:app --app-dir llm-project/serve --host 127.0.0.1 --port 8000

The API deliberately reports not-ready and returns HTTP 503 for inference until a valid local model is loaded. It never returns fabricated model output.

## Docker Compose

Set RUSA_API_KEY to a random secret and RUSA_MODEL_DIR to the directory containing model files. Then run docker compose up --build -d. The model directory is mounted read-only at /models.

The Compose file passes explicit per-service environment variables instead of a shared env_file: the API receives its API key, while the worker receives provider/GitHub credentials. The container runs as UID/GID 10001, drops Linux capabilities, uses a read-only root filesystem under Compose, and exposes port 8000. Put it behind a TLS-terminating reverse proxy or gateway. Configure distributed rate limiting, request timeouts, and connection limits at that gateway; an in-process limiter is not sufficient across replicas.

## Health endpoints

- GET /health/live: process liveness; does not imply that a model is loaded.
- GET /health: backward-compatible liveness endpoint.
- GET /health/ready: returns HTTP 200 only when a model, API-key configuration, and task schema are available.

## API endpoints

- POST /v1/chat/completions: requires a valid service API key or active user bearer session; accepts non-streaming chat requests.
- POST /v1/validate-task: requires X-API-Key; validates an A2A envelope. Missing or invalid schema configuration fails closed with HTTP 503.
- POST /v1/agent/tasks: requires a valid service API key or active user bearer session plus Idempotency-Key; validates and enqueues a task in PostgreSQL. Browser-created tasks are attributed to the authenticated user. Requires RUSA_AGENT_ENABLED=true and DATABASE_URL.
- GET /v1/agent/tasks/{task_id}: requires a valid service API key or active user bearer session; returns task status within the tenant and enforces task ownership for regular users.
- POST /v1/agent/tasks/{task_id}/cancel: requires X-API-Key; cancels queued/running work for the authenticated tenant.
- GET /v1/agent/queue/stats: returns tenant-scoped queue counts and oldest queued-task age.
- GET /v1/agent/dead-letters: lists up to 50 dead-letter tasks for the authenticated tenant.
- POST /v1/agent/tasks/{task_id}/retry: requeues a dead-letter task after operator review.


## User and admin web workspace

The repository includes a responsive React/TypeScript web application with separate role-aware experiences:

- **User Workspace** (/ , /chat, /tasks): workspace overview, AI chat, personal agent task submission, and task tracking.
- **Admin Control Center** (/admin, /admin/users, /admin/tasks): tenant-wide overview, account provisioning, role/active-state control, and agent task operations.
- FastAPI-backed identity with Argon2id password hashes, short-lived signed access tokens, rotating HttpOnly refresh sessions, same-origin cookie protection, login throttling, and tenant-scoped authorization.
- Nginx serves the frontend and reverse-proxies API/health requests over the private Compose network. Browser clients never receive database, provider, GitHub, or service API secrets.

For a fresh deployment, configure RUSA_AUTH_SECRET, RUSA_BOOTSTRAP_ADMIN_EMAIL, RUSA_BOOTSTRAP_ADMIN_PASSWORD, RUSA_TENANT_ID, and RUSA_MODEL_DIR in .env. Bootstrap admin tenant must match RUSA_TENANT_ID. The Compose deployment serves the web application on 127.0.0.1:3000 by default; put a TLS-terminating reverse proxy in front of it for remote access.

Start the web stack with:

~~~bash
docker compose up -d --build postgres rusa-api rusa-web
~~~

Start the optional agent worker separately after configuring its provider and GitHub credentials:

~~~bash
docker compose --profile agent up -d --build rusa-agent-worker
~~~

For local frontend development, install dependencies in apps/web and run npm run dev. The Vite development server proxies /v1 and /health to the FastAPI service. See docs/ui-ux/user-admin-workspace.md for routes, security boundaries, and setup details.

## Staging deployment

A GitHub Actions workflow can publish immutable API/model, agent-worker, and web images to GHCR and deploy them to a protected staging environment. It supports a protected `staging` branch push (with environment approval) and manual dispatch after the workflow is available on the default branch. Configure branch protection, required environment reviewers, host/SSH secrets, a read-only GHCR pull token, and a host-managed `.env` before deployment. Start with `build-only` to publish and inspect images without touching a server. See [the operations runbook](docs/OPERATIONS_RUNBOOK.md#4-staging-image-publication-and-deployment).

## Agent worker

Set RUSA_AGENT_ENABLED=true, DATABASE_URL, RUSA_PROVIDER_BASE_URL, RUSA_PROVIDER_ALLOWED_HOSTS, RUSA_PROVIDER_MODEL, RUSA_PROVIDER_API_KEY (when required), and RUSA_GITHUB_TOKEN in the secret-backed .env file. For a single-tenant deployment, configure RUSA_GITHUB_ALLOWED_REPOSITORIES. For a multi-tenant deployment, configure RUSA_API_KEY_TENANTS_JSON with SHA-256 API-key hashes and RUSA_GITHUB_TENANT_REPOSITORIES_JSON with a separate repository allowlist for every authenticated tenant. Remote model provider hostnames must appear in RUSA_PROVIDER_ALLOWED_HOSTS. Do not use a shared global repository allowlist for multiple tenants. Start the API and PostgreSQL with docker compose up --build -d, then start the optional worker with docker compose --profile agent up -d rusa-agent-worker. Use a GitHub App or fine-grained token restricted to the target repository. The worker does not merge or deploy.

See AI_CODE.md for the agent contract and docs/PRODUCTION_READINESS.md for release gates.

## Quality gates

Run ruff check rusa_agents llm-project/serve tests, python -m compileall -q rusa_agents llm-project tests, pytest -q, pip-audit -r requirements.txt, and pip-audit -r requirements-model.txt. PostgreSQL integration tests require DATABASE_URL.

A green CI run validates the serving foundation. It does not prove model quality, model capacity, multi-agent correctness, or production SLOs.

## Production acceptance checklist

- [ ] Licensed model artifact, revision, checksum, tokenizer, and chat template are recorded.
- [ ] Model correctness, p50/p95 latency, throughput, RAM/VRAM, and context limits are benchmarked on target hardware.
- [ ] A suitable high-throughput serving strategy is selected. The current Transformers adapter serializes generation in one process and is a baseline adapter.
- [ ] Streaming is implemented and tested if required.
- [ ] Distributed rate limiting, TLS, ingress body limits, timeouts, and network policy are configured at the gateway.
- [ ] Secret rotation, access audit, backup/restore, incident response, and rollback are tested.
- [ ] Multi-agent orchestration, queueing, idempotency, retries, cancellation, and worker isolation are implemented and tested.
- [ ] Persistent memory/RAG includes tenant isolation, data provenance, retention, and deletion controls.
- [ ] Evaluation, safety, prompt-injection, tool authorization, and adversarial tests meet documented thresholds.
- [ ] Staging soak test, canary rollout, monitoring, alerting, and operational runbook are approved.

See docs/PRODUCTION_READINESS.md for the evidence ledger, docs/OPERATIONS_RUNBOOK.md for deployment and task operations, and AI_CODE.md for the agent contract.