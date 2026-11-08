# RUSA AI UI/UX Precision Matrix

**Scope:** web Control Center  
**Frontend:** Next.js 15, React 19, TypeScript  
**Reference set:** RUSA AI Architecture, All Agents, Agentic DevOps Lifecycle, ADR-COMM-001

## Design principles

1. Every displayed state is backed by an API response or explicitly labelled as an architecture contract.
2. Every mutation is constrained by the authenticated role and re-authorized by the backend.
3. No UI affordance implies an API capability that does not exist.
4. Agent identity and lifecycle stage names are not aliased without an architecture decision.
5. Counts disclose whether they represent tenant totals, a loaded page, or a capped result.
6. Approval, policy, evidence, and audit boundaries remain visible in the workflow.

## Page-to-contract matrix

| Page | User task | Data source | Current implementation boundary |
|---|---|---|---|
| Command Center | Understand task volume and service readiness | GET /v1/aicode/tasks, /health, /health/ready | Active/failed counts are loaded-page samples; approval queue capped at 100 |
| Orchestrator | Understand AICode execution and proposed communication | AICodeOrchestrator source, All Agents, ADR-COMM-001 | Current pipeline and proposed ADR topology shown separately |
| AICode Tasks | Create, find, inspect, retry failed, cancel | POST/GET /v1/aicode/tasks and task detail/retry/delete | Retry button only for failed tasks; backend tenant RBAC remains authoritative |
| Approval Center | Inspect awaiting_approval tasks | GET /v1/aicode/tasks?status=awaiting_approval | No approve/reject controls; no approval record API exists |
| Agent Workspace | Inspect runtime inventory and agent contracts | WorkerRole, AgentManager, AICodeOrchestrator, All Agents | 7 source-defined runtime components; 10 architecture roles not shown as online |
| Agentic DevOps | Inspect Plan–Monitor lifecycle contract | Agentic DevOps Lifecycle document | Eight stage contracts; no live lifecycle registry |
| RUSA AI Chat | Run inference | GET /v1/models, POST /v1/chat/completions | Stream request with HTTP 501 fallback to non-stream |
| Memory & Knowledge | Understand memory categories | All Agents memory specification | Working, Episodic, Semantic, Procedural, Project; no memory CRUD/retrieval API |
| Repository & Artifacts | Inspect task evidence references | Task result payload | No file browser, download, provenance, or digest verification API |
| Evaluation & Reports | Understand evaluation KPI taxonomy | Evaluation & Reporting specification | KPI definitions only; no evaluation job/scorecard API |
| Governance | Inspect principal, role permission, policy boundaries | Auth session, backend role mapping, AutonomyPolicy | Policy values are source defaults, not runtime policy introspection |
| Observability | Inspect health and operator metrics | /health, /health/ready, /metrics | Metrics require platform_admin and server-side RUSA_METRICS_API_KEY; task progress polls |
| Product Pillars | Inspect four product pillars | RUSA_AI_Architecture.md | Business and Ecosystem remain architecture-defined |
| Developer Tools | Discover model and validate A2A envelope | /v1/models, /v1/validate-task | Envelope validator is not AICode create-task validation |

## Agent identity matrix

### Runtime source inventory

| Component | Runtime role/manager | UI label |
|---|---|---|
| AICodeOrchestrator | Root supervisor | Code-defined |
| Analyst | Repository manager | Code-defined |
| Architect | Repository manager | Code-defined |
| Developer | Engineering manager | Code-defined |
| Debugger | Engineering manager | Code-defined |
| Tester | Quality manager | Code-defined |
| Security | Quality manager | Code-defined |

The repository does not expose live registry, heartbeat, instance health, or dynamic agent availability. Code-defined is not equivalent to online.

### Ten architecture roles

Supreme Orchestrator; System Architect; Planner; Task Decomposer; Software Engineer; DevOps; QA & Testing; Security & Policy; Memory & Knowledge; Evaluation & Reporting.

Each role detail panel presents its objective, responsibilities, inputs, outputs, and authority restrictions from the All Agents specification. It does not imply an invocation endpoint.

## Eight-stage lifecycle matrix

| Stage ID | Stage | Principal output | Boundary |
|---|---|---|---|
| rusa_plan_agent | Plan | plan, requirements, backlog, acceptance criteria, risk assessment | Product scope changes require owner approval |
| rusa_code_agent | Code | source branch, tests, docs, commit, PR, change summary | No force-push main, self-merge, branch-protection or production-secret changes |
| rusa_build_agent | Build | binary/package/image, logs, checksum, manifest, SBOM when supported | No silent dependency/base-image substitution |
| rusa_test_agent | Test | test, coverage, regression, security reports, evidence, PASS/FAIL/BLOCKED | No PASS without complete evidence for the tested commit |
| rusa_release_agent | Release | candidate, changelog, manifest, deployment/rollback notes | Official publication follows repository approval |
| rusa_deploy_agent | Deploy | deployment record, active version, health/smoke result, rollback record | Production approval required; never display raw secrets |
| rusa_operate_agent | Operate | operational report, incident timeline, recovery/capacity report | Allowlisted actions only; destructive/high-impact changes require approval |
| rusa_monitor_agent | Monitor | telemetry dashboard data, alerts, anomaly/SLO/capacity report | Read-only by default |

## Backend contracts surfaced

- Session: GET /v1/auth/session
- Task intake: POST /v1/aicode/tasks
- Task list: GET /v1/aicode/tasks with status/limit/offset
- Task detail: GET /v1/aicode/tasks/{task_id}
- Retry: POST /v1/aicode/tasks/{task_id}/retry
- Cancel: DELETE /v1/aicode/tasks/{task_id}
- Chat: POST /v1/chat/completions
- Models: GET /v1/models
- A2A envelope validation: POST /v1/validate-task
- Health: GET /health and GET /health/ready
- Operator metrics: GET /metrics

## Explicitly not implemented as interactive capabilities

- Approve/reject and durable approval records
- Live agent registry and heartbeat
- /runs workflow API and WebSocket cursor/reconnect
- Memory CRUD, indexing, retrieval, retention
- Artifact browser, download, immutable provenance/digest lookup
- Evaluation jobs, scorecards, historical KPI and cost attribution
- RUSA Business automation and RUSA Ecosystem marketplace/integration modules
- Runtime registration for all eight lifecycle agents

ADR-COMM-001 is marked Diusulkan. gRPC, RabbitMQ, Kafka/event log, outbox, /runs, and WebSocket are presented as proposed contracts, not current deployment facts.

## Interaction and security rules

- API keys remain in encrypted HttpOnly JWE session cookies and server-side BFF calls.
- Browser receives identity claims, never the raw API key or metrics key.
- Tenant filtering and permission enforcement stay in FastAPI.
- Platform metrics are server-proxied and restricted to platform_admin.
- Failed-task retry and task cancellation require admin roles in the UI; backend checks remain authoritative.
- awaiting_approval is never treated as approved. The UI does not expose retry from the approval queue.
- No mock metrics, fabricated agent status, synthetic event stream, or pretend artifact download.
- Task list polls every 10 seconds; health polls every 30 seconds.
- Dashboard counts explicitly disclose their sampling/cap semantics.

## Responsive behavior

The desktop layout uses grouped navigation, a persistent workspace sidebar, dense task tables, and split-pane detail views. On narrower screens, navigation becomes a slide-in drawer, grids collapse to one or two columns, and forms/modals remain scrollable.

## Validation

The PR CI workflow runs TypeScript typecheck and Next.js production build, then builds the web Docker image. Compose validation remains part of the repository's existing CI checks.
