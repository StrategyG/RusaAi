# RUSA AI Control Center — Integration Notes

## Purpose

The Control Center is a separate Next.js application in web/. It consumes the existing FastAPI application through same-origin Next.js route handlers. It does not connect a browser to PostgreSQL, the worker process, a broker, the model provider, or internal tool execution.

## Implemented integration

| UI capability | Backend contract |
|---|---|
| Tenant sign-in | GET /v1/auth/session using X-API-Key |
| Task creation | POST /v1/aicode/tasks with Idempotency-Key |
| Task history | GET /v1/aicode/tasks with tenant-scoped pagination and optional status |
| Task detail | GET /v1/aicode/tasks/{task_id} |
| Retry | POST /v1/aicode/tasks/{task_id}/retry |
| Cancellation | DELETE /v1/aicode/tasks/{task_id} |
| Chat | POST /v1/chat/completions |
| Health | GET /health and GET /health/ready |

The API key is held in an encrypted JWE session cookie with HttpOnly and SameSite attributes. Browser code receives only subject, tenant ID, and roles. Every task endpoint continues to apply backend tenant scoping and task RBAC. Mutating BFF requests require a same-origin Origin header.

## Task list API

GET /v1/aicode/tasks

Query parameters:
- status: queued, running, awaiting_approval, succeeded, failed, or cancelled
- limit: 1–100 (default 50)
- offset: 0–100000 (default 0)

Response:
- items: task records containing task_id, tenant_id, payload, result, status, attempts, max_attempts, last_error, created_at, updated_at
- total, limit, offset

The query applies both explicit tenant filtering and the existing PostgreSQL tenant context. List access is written to the task policy audit stream as task.list.

## Accurate capability boundaries

The current repository implements an AICode orchestrator with analyst, architect, developer, debugger, tester, and security worker roles. The UI labels these as current runtime roles.

The All Agents catalogue names Supreme Orchestrator, System Architect, Planner, Task Decomposer, Software Engineer, DevOps, QA & Testing, Security & Policy, Memory & Knowledge, and Evaluation & Reporting. These remain a separate architecture catalogue until canonical runtime registrations are available.

The eight Plan, Code, Build, Test, Release, Deploy, Operate, and Monitor agents in the Agentic DevOps lifecycle document are displayed as architecture targets. They are not represented as live runtime agents until the corresponding worker registrations, APIs, and execution evidence exist. The source documents also use Supreme Orchestrator, Executive Orchestrator, and DevOps Orchestrator terminology; the UI deliberately does not alias these identities without an explicit architecture decision.

The API supports the awaiting_approval state and an explicit retry after human review, but it does not currently expose an approve/reject endpoint. The UI therefore never marks a task approved. An authorized human must complete the review through the configured governance process before an operator uses retry.

The API has no external WebSocket status stream. The UI polls the tenant-scoped task list every ten seconds and refreshes details on demand. A future event-stream integration should follow the ADR cursor/reconnect contract.

## Run locally

From the repository root:

    cp .env.example .env
    # Set POSTGRES_PASSWORD, API keys, and RUSA_WEB_SESSION_SECRET.
    docker compose up --build

Open http://localhost:3000. Configure a tenant API key with the appropriate task permissions. AICode task creation additionally requires the existing AICode intake flag, database queue, worker, and repository allowlist to be configured.

## Production

Build and publish an immutable web image, set RUSA_WEB_IMAGE to its digest, and set RUSA_WEB_SESSION_SECRET using a secret manager. The production Compose web service binds to loopback and joins only the internal RUSA network. Place an authenticated TLS reverse proxy in front of it before exposing the UI to users. The repository README continues to state that RUSA-AI is not production-certified; adding this UI does not change that certification status.