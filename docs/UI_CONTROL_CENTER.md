# RUSA AI Control Center — Integration Notes

## Purpose

The Control Center is a separate Next.js application in web/. It consumes the existing FastAPI application through same-origin Next.js route handlers. It does not connect a browser to PostgreSQL, the worker process, a broker, the model provider, or internal tool execution.

## RUSA AI product pillars

The architecture document defines four pillars: AI Autonomous Software Engineering & DevOps, RUSA Intelligence, RUSA Business, and RUSA Ecosystem. The current Control Center integrates AICode task execution and RUSA Intelligence inference. Business automation and Ecosystem marketplace/integration surfaces are presented as architecture pillars, not as implemented backend modules.

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

The API key is held in an encrypted JWE session cookie with HttpOnly and SameSite attributes. Browser code receives only subject, tenant ID, and roles. Every task endpoint continues to apply backend tenant scoping and task RBAC. Mutating BFF requests require a same-origin Origin header checked against the request Host; the reverse proxy must preserve Host and set X-Forwarded-Proto. The local Compose service disables Secure cookies for loopback HTTP, while production enables them.

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

The eight Plan, Code, Build, Test, Release, Deploy, Operate, and Monitor agents in the Agentic DevOps lifecycle document are displayed as architecture targets. Security Guard is represented as a cross-cutting design control; the current API's security worker role is not silently renamed to that lifecycle identity. They are not represented as live runtime agents until the corresponding worker registrations, APIs, and execution evidence exist. The source documents also use Supreme Orchestrator, Executive Orchestrator, and DevOps Orchestrator terminology; the UI deliberately does not alias these identities without an explicit architecture decision.

The current AICode API supports the awaiting_approval state and an explicit retry after human review, but it does not expose an approve/reject endpoint. The Platform Core 2.0 branch adds a backend-only PostgreSQL approval service with exact action/resource/artifact binding and one-time consumption; it is not yet wired into the API or Control Center. The UI therefore never marks a task approved. An authorized human must complete the review through the configured governance process before an operator uses retry.

The API has no external WebSocket status stream. The UI polls the tenant-scoped task list every ten seconds and refreshes details on demand. A future event-stream integration should follow the ADR cursor/reconnect contract.

## Run locally

From the repository root:

    cp .env.example .env
    # Set POSTGRES_PASSWORD, API keys, and RUSA_WEB_SESSION_SECRET.
    docker compose up --build

Open http://localhost:3000. Configure a tenant API key with the appropriate task permissions. AICode task creation additionally requires the existing AICode intake flag, database queue, worker, and repository allowlist to be configured.

## Production

Build and publish an immutable web image, set RUSA_WEB_IMAGE to its digest, and set RUSA_WEB_SESSION_SECRET using a secret manager. The production Compose web service binds to loopback and joins only the internal RUSA network. Place an authenticated TLS reverse proxy in front of it before exposing the UI to users. The repository README continues to state that RUSA-AI is not production-certified; adding this UI does not change that certification status.

## Capability status source

The maintained [Agent Capability Status Matrix](AGENT_CAPABILITY_MATRIX.md) is the source of truth for current runtime roles, partially represented shared agents, and planned DevOps/domain capabilities. UI labels must use that distinction. Only a versioned, validated, active registry entry with passing execution and health evidence may be labelled as an active platform agent. The current six AICode worker roles remain labelled as AICode runtime roles.

Canonical orchestrator names and communication choices are defined in [ADR-PLATFORM-001/002](ADR-PLATFORM-001-002.md). Supreme Orchestrator is a target global coordinator; AICodeOrchestrator remains the current coding implementation; DevOps Orchestrator is a target domain coordinator. PostgreSQL remains the current durable queue. RabbitMQ, Kafka, gRPC, and WebSocket remain deferred options.
