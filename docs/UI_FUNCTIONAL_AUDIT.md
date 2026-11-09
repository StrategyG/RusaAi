# RUSA AI Control Center — Functional Audit

**Audited code revision:** c1553e6d04e22fdb31e20f7051da8fbd746a1041  
**Application:** Next.js 15 / React 19  
**Audit type:** source/API contract review, BFF smoke tests, and Chromium browser smoke tests  
**Environment:** CI standalone web server connected to a deterministic mock RUSA API

## Status vocabulary

- **API-integrated:** the UI calls an existing backend endpoint; this is not a claim that the production deployment was exercised.
- **Browser-smoke verified:** a Chromium test exercised the visible interaction against the CI mock API.
- **Architecture view:** the page presents source-defined or document-defined contracts; it does not invoke a live runtime for that contract.
- **Not implemented:** the required backend contract does not exist in the inspected API surface.

## Fourteen-workspace audit matrix

| Workspace | Actual behavior | Verification | Boundary / remaining work |
|---|---|---|---|
| Command Center | Tenant task totals, loaded-page task sample, liveness/readiness, product-pillar overview | Browser navigation; BFF task and health routes | Active/failed are loaded-page samples. Production health was not exercised. |
| Orchestrator | Displays current AICode sequence, separate ten-step architecture workflow, and proposed ADR communication design | Browser navigation | Architecture view only. No /runs workflow API, event log, broker, or WebSocket runtime is exposed. |
| AICode Tasks | Create, tenant-scoped list, pagination/filter, detail, failed-task retry, and cancellation | BFF create/list/detail/retry/cancel; browser create/detail/retry; queue policy unit test | Explicit retry is backend-enforced for failed tasks only; awaiting_approval receives HTTP 409. Real execution still depends on the deployed API, durable queue, worker, AICode flag, and tenant repository allowlist. |
| Approval Center | Lists awaiting_approval records and opens task detail | BFF approval-list route; browser verifies no fabricated approve/reject or retry controls on approval-paused task detail; BFF retry returns HTTP 409 | No approval decision endpoint, durable approval record, or approver identity contract. |
| Agent Workspace | Displays seven code-defined AICode runtime components and ten architecture-role contracts | Browser navigation and role-detail modal | No live agent registry, heartbeat, instance health, or agent invocation API. |
| Agentic DevOps | Displays eight stage contracts, inputs, outputs, exit criteria, and authority boundaries | Browser navigation and stage-selection interaction | Lifecycle stages are architecture contracts, not eight registered runtime agents. |
| RUSA AI Chat | Discovers models and sends streaming or non-streaming inference requests | BFF model and chat SSE/JSON routes; browser streaming interaction | CI uses a mock provider. Actual model-provider availability, latency, and quality require staging verification. |
| Memory & Knowledge | Displays five memory categories and describes optional RAG/MCP components | Browser navigation | No generic web memory CRUD, knowledge browser, indexing, retrieval, or retention API. RAG/MCP remains a separate optional surface. |
| Repository & Artifacts | Extracts safe PR, commit, and branch references from loaded task results | Browser navigation | No repository file browser, artifact download, immutable digest lookup, or provenance verification endpoint. |
| Evaluation & Reports | Displays KPI definitions and distinguishes offline RAG evaluation from live API/worker telemetry | Browser navigation | No web evaluation jobs, agent scorecards, historical KPI reports, or cost-attribution API. |
| Governance & Policy | Displays authenticated identity, role map, and source-default autonomy policy | Browser navigation; BFF role-denial tests | Policy defaults are not a live policy-introspection API. Backend RBAC remains authoritative. |
| Observability | Displays liveness/readiness and platform-admin-only API metrics | BFF health/metrics routes; browser metrics interaction | Worker metrics are a separate internal listener and are not aggregated into the web API metrics response. |
| RUSA AI Product Pillars | Displays four architecture-defined product pillars and integration status | Browser navigation | RUSA Business and RUSA Ecosystem remain architecture-defined in this repository scope. |
| Developer Tools | Discovers model IDs and validates the current repository A2A envelope schema | BFF models/validator routes; browser validator interaction | Validator schema is distinct from AICode task intake and the proposed ADR-COMM-001 envelope. |

## Automated verification added

Run from web/ after installing dependencies and building the standalone Next.js output:

    npm install
    npm run build
    npx playwright install --with-deps chromium
    npm run smoke

The smoke command starts the built standalone web server and a deterministic mock API. It checks:

- Encrypted HttpOnly session cookie, identity revalidation, and no raw API-key return to browser code.
- Missing-session rejection, same-origin mutation protection, platform_admin/developer/tenant_admin/read_only role boundaries, and platform-metrics denial for tenant_admin/read_only.
- Tenant-scoped task list and detail behavior across two tenant identities, including cross-tenant detail hiding (HTTP 404) and a browser tenant-switch assertion.
- Approval list, task creation, failed-task retry, approval-paused retry denial (HTTP 409), and cancellation proxy routes.
- Model discovery, liveness/readiness, platform-admin metrics authorization and server-side metrics key, chat SSE and JSON responses, and A2A validation.
- Chromium login/logout for platform_admin, developer, tenant_admin, and read_only roles; all fourteen workspace navigation destinations; task creation/detail/retry; developer-versus-admin and tenant-admin task controls; read-only inference denial; tenant switching without prior-tenant task leakage; architecture-role detail; DevOps stage selection; approval-page safety and no-retry detail state; chat streaming; A2A validation; platform-admin metrics display; and mobile navigation drawer.

The browser and BFF tests use a mock API that follows the web adapter contracts. They verify the web application and its server-side BFF behavior; they do **not** prove that the production API, database, worker, model provider, or optional RAG/MCP deployment is reachable.

The separate RUSA-AI CI workflow continues to test backend compilation, lint, security analysis, Python tests, RAG/MCP integration, isolated worker/sandbox images, vulnerability gates, and production Compose validation.

## Governance note: awaiting approval

The current API has an awaiting_approval state but no structured approve/reject endpoint or durable approval-decision record. The queue retry method is now restricted to failed tasks only; awaiting_approval returns HTTP 409. A task-retry audit event is not a substitute for a dedicated approval record. The Control Center intentionally does not expose retry from the Approval Center and never labels a task approved. A dedicated approval contract is still required before the UI can offer an approval decision.

## Production readiness boundary

This audit does not certify production readiness. Before declaring all workspaces operational against a deployment, verify in staging with the real RUSA API:

1. Session cookie security behind the actual TLS reverse proxy and tenant-specific API keys.
2. PostgreSQL durability, tenant RLS, queue migrations, AICode worker availability, kill switch, and repository allowlist.
3. The configured inference provider and model permissions.
4. Real task lifecycle, evidence/result payloads, retry/cancel races, and audit records.
5. Optional RAG/MCP service, its restricted database role, and tenant-scoped retrieval if enabled.
6. Metrics credentials, API/worker metrics collection, and internal-only scrape access.
7. Browser flows against the real backend, followed by operator/product-owner sign-off.

**Conclusion:** the Control Center's currently implemented UI/BFF interactions now have automated browser and route-level smoke coverage. The architecture-only and not-implemented capabilities remain explicitly out of scope until their backend contracts are delivered.
