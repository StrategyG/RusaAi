# RUSA AI Control Center — Integration and Capability Boundaries

## Purpose

The Control Center is a Next.js 15 / React 19 application in `web/`. It consumes the FastAPI service through same-origin Next.js route handlers. Browser code does not connect directly to PostgreSQL, workers, a broker, the model provider, or internal tool execution.

The interface is designed around source-backed capabilities. A feature is labelled as live only when a corresponding API or runtime contract exists. Architecture-only capabilities remain visible as specifications, not simulated controls.

## Product architecture

The RUSA AI architecture document defines four pillars:

1. AI Autonomous Software Engineering & DevOps
2. RUSA Intelligence
3. RUSA Business
4. RUSA Ecosystem

Current web integration is partial for the first two pillars: AICode task execution and inference/model discovery. Memory/knowledge and evaluation APIs are not exposed. RUSA Business and RUSA Ecosystem are shown as architecture-defined, not implemented backend modules.

The Agentic DevOps lifecycle document also describes RUSA Repository as an evidence/artifact layer and RUSA Social as an optional publishing layer. These document-specific integration names are not silently substituted for the four product pillars above.

## UI workspace map

| Workspace | Responsibility | Source of truth |
|---|---|---|
| Command Center | Tenant task totals, loaded-page status sample, health/readiness, product pillars | Task list and health APIs |
| Orchestrator | Actual AICode execution sequence; separate 10-step Supreme Orchestrator workflow; proposed communication topology | `rusa_agents/orchestrator.py`, All Agents document, ADR-COMM-001 |
| AICode Tasks | Create, tenant-scoped list, pagination, status filter, detail, failed-task retry, cancellation | AICode task API |
| Approval Center | List `awaiting_approval` records and open task details with the result payload currently exposed by the API | AICode task API |
| Agent Workspace | Seven source-defined runtime components and ten architecture role contracts | Runtime source and All Agents document |
| Agentic DevOps | Eight lifecycle agent contracts, inputs, outputs, exit criteria, authority boundaries | Agentic DevOps lifecycle document |
| RUSA AI Chat | Model discovery, inference, stream request with non-stream fallback | `/v1/models`, `/v1/chat/completions` |
| Memory & Knowledge | Working, Episodic, Semantic, Procedural, and Project memory taxonomy | Memory & Knowledge Agent specification |
| Repository & Artifacts | Artifact references and evidence keys returned in task result payloads | Task result payload |
| Evaluation & Reports | Task, agent, infrastructure, and AI KPI definitions | Evaluation & Reporting Agent specification |
| Governance | Authenticated principal, backend role permissions, source-defined autonomy policy boundaries | FastAPI auth/RBAC and `rusa_agents/policy.py` |
| Observability | Liveness, readiness, operator metrics, polling/ADR delivery status | Health and metrics endpoints |
| Product Pillars | Four RUSA AI product pillars and evidence-backed integration state | `RUSA_AI_Architecture.md` |
| Developer Tools | Model list and A2A envelope validation | `/v1/models`, `/v1/validate-task` |

## Implemented API integration

| UI capability | Backend contract | Notes |
|---|---|---|
| Tenant sign-in | `GET /v1/auth/session` | Uses `X-API-Key` server-side |
| Task creation | `POST /v1/aicode/tasks` | BFF adds `Idempotency-Key` |
| Task history | `GET /v1/aicode/tasks` | Tenant-scoped; limit, offset, optional status |
| Task detail | `GET /v1/aicode/tasks/{task_id}` | Tenant-scoped |
| Failed-task retry | `POST /v1/aicode/tasks/{task_id}/retry` | UI exposes only for `failed` records |
| Task cancellation | `DELETE /v1/aicode/tasks/{task_id}` | Admin roles only in UI; backend remains authoritative |
| Chat inference | `POST /v1/chat/completions` | Supports streaming when provider does; HTTP 501 falls back to non-streaming |
| Model discovery | `GET /v1/models` | Added same-origin BFF route |
| A2A envelope validation | `POST /v1/validate-task` | Validates `schemas/a2a/task-envelope.schema.json`, not the AICode create-task body or the separate proposed ADR envelope |
| Health | `GET /health`, `GET /health/ready` | Liveness and readiness are separate |
| Operator metrics | `GET /metrics` | Same-origin BFF route; platform_admin only; metrics key stays server-side |

The API key is held in an encrypted JWE session cookie with HttpOnly and SameSite attributes. Browser code receives only subject, tenant ID, and roles. Mutating BFF requests require a same-origin Origin header checked against request Host and protocol. The reverse proxy must preserve Host and set X-Forwarded-Proto. Local Compose disables Secure cookies for loopback HTTP; production enables them.

The metrics route requires `RUSA_METRICS_API_KEY` in the web server environment. Compose passes it to the web service server-side; it is never returned to browser JavaScript.

## AICode runtime and architecture roles

The current code defines one `AICodeOrchestrator` root supervisor and six worker roles:

- Analyst
- Architect
- Developer
- Debugger
- Tester
- Security

The orchestrator groups roles under Repository, Engineering, and Quality managers. Its observed execution sequence is Analyst → Architect → Developer → Debugger, followed by Tester and Security quality checks in parallel. The default orchestrator allows up to two repair cycles.

This inventory is source-defined. The current API does not expose live agent registration, heartbeat, per-agent health, or runtime instance state. The UI therefore uses the label “Code-defined”, not “Online”.

The All Agents document separately defines ten architecture roles: Supreme Orchestrator, System Architect, Planner, Task Decomposer, Software Engineer, DevOps, QA & Testing, Security & Policy, Memory & Knowledge, and Evaluation & Reporting. These remain architecture role contracts until canonical runtime registration and evidence are exposed.

The source documents use Supreme Orchestrator, Executive Orchestrator, and DevOps Orchestrator terminology. The UI does not alias these identities to `AICodeOrchestrator` without an explicit architecture decision.

## Agentic DevOps lifecycle

The eight lifecycle IDs are:

- `rusa_plan_agent`
- `rusa_code_agent`
- `rusa_build_agent`
- `rusa_test_agent`
- `rusa_release_agent`
- `rusa_deploy_agent`
- `rusa_operate_agent`
- `rusa_monitor_agent`

The UI shows each stage's purpose, inputs, outputs, exit criteria, and authority boundary from the lifecycle specification. They are architecture contracts, not live runtime registrations in the current AICode API.

## Approval and retry safety

The task API supports the `awaiting_approval` state and the retry endpoint. It does not expose an approve/reject endpoint, an approval record, or an approver identity contract.

The underlying queue retry method currently accepts both `failed` and `awaiting_approval`. The UI deliberately exposes retry only for `failed` records and does not treat retry as approval. Do not use the retry endpoint to bypass required human governance. A dedicated approval decision and audit contract is required before approve/reject controls can be added.

## Task list semantics

The task list API accepts:

- `status`: queued, running, awaiting_approval, succeeded, failed, or cancelled
- `limit`: 1–100
- `offset`: 0–100000

The UI loads 25 records per page. Server status filtering and pagination are applied by the backend. Search and the “Active” filter are client-side within the currently loaded page. The backend list status filter does not include `blocked`; task detail displays any status returned by the API.

Dashboard counts are labelled by their scope: tenant total is sourced from the API total; active/failed counts refer only to the loaded page; approval count is the first 100 `awaiting_approval` records.

## Communication and telemetry boundaries

The UI polls task state every ten seconds and health every thirty seconds. It does not claim WebSocket, cursor replay, or reconnect behavior.

ADR-COMM-001 is marked “Diusulkan”. gRPC/Protobuf, durable command queue, Kafka/event log, outbox, external `POST /runs`, `GET /runs/{id}`, cancel-run API, and WebSocket cursor delivery are target contracts only. They are not represented as deployed services or active endpoints.

## Explicitly unavailable in the current web/API surface

- Approve/reject API and durable approval decision record
- Live agent registry, heartbeat, and per-agent health
- Dedicated `/runs` workflow API and run state projection
- WebSocket event stream and cursor/reconnect
- Memory CRUD, indexing, semantic retrieval, and retention controls
- Artifact file browser, download, immutable-reference lookup, and digest verification
- Evaluation jobs, agent scorecards, historical KPI trends, and cost attribution
- RUSA Business automation UI/API
- RUSA Ecosystem marketplace and external integration UI/API
- Lifecycle-specific runtime registration for the eight DevOps stages

Unavailable features are intentionally described, not mocked.

## Local development

From the repository root:

    cp .env.example .env
    # Set POSTGRES_PASSWORD, API keys, RUSA_WEB_SESSION_SECRET, and RUSA_METRICS_API_KEY.
    docker compose up --build

Open http://localhost:3000. Configure a tenant API key with the required backend permissions. AICode task intake also requires the AICode flag, durable database queue, worker, and tenant repository allowlist.

## Production

Build and publish immutable web and API images. Set `RUSA_WEB_IMAGE` to its digest and inject `RUSA_WEB_SESSION_SECRET` and `RUSA_METRICS_API_KEY` through a secret manager. The web service remains loopback-bound and internal-network-only. Put an authenticated TLS reverse proxy in front of it before exposing the UI. The repository README's production certification status is unchanged by this frontend work.
