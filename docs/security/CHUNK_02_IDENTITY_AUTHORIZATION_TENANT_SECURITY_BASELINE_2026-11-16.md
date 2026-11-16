# CHUNK 02 — Identity, Authorization and Tenant Security Baseline

**Snapshot date:** 2026-11-16  
**Status:** DISCOVER / BASELINE complete; DESIGN and IMPLEMENT are in progress. This is a source-and-CI evidence baseline, not a production security certification.  
**Roadmap dependency:** CHUNK 01 is merged and its cross-repository documentation is aligned.  
**Production posture:** NO-GO / NOT_VERIFIED. No live production database, identity provider, secret manager, Supabase project, Dropbox account, or deployed service was accessed.

## 1. Source snapshot and evidence boundary

| Repository | Main SHA at discovery | Relevant implementation |
|---|---|---|
| RusaAi | `cb7e12a44406cba1ff3e55aa699b389bd2fcc773` | API-key principal registry, tenant-scoped task API/RLS, worker queue and lease fencing, `rusa_knowledge` |
| RusaModels | `b9671c1581edc6c3ee265fa97dff949a070e1999` | Hashed API principal registry, role-based inference authorization, request-correlated audit events |
| RAG_Cerdas | `7b62582824d5fda33f5c47d1ac9e67378d3c019d` | IM19 worker/approver database roles, reviewer identity registry, append-only revocation and review triggers |

Relevant CI evidence:
- RusaAi PR #117 exact-head workflow [43733130737](https://github.com/StrategyG/RusaAi/actions/runs/43733130737): 3/3 checks passed; core suite 337 passed / 6 skipped. Its RAG/MCP integration job also ran the disposable PostgreSQL/pgvector knowledge-store tests: 65 passed.
- RusaModels PR #24 exact-head workflow [43704012560](https://github.com/StrategyG/RusaModels/actions/runs/43704012560): 2/2 checks passed; 156 passed / 1 skipped.
- RAG_Cerdas PR #28 exact-head workflow [43680080103](https://github.com/StrategyG/RAG_Cerdas/actions/runs/43680080103): 1/1 check passed; disposable PostgreSQL/pgvector-backed suite reported 240 passed, including reviewer-principal and role-membership tests.
- Post-merge main workflows were still running for RusaAi [43801535417](https://github.com/StrategyG/RusaAi/actions/runs/43801535417) and RusaModels [43801252283](https://github.com/StrategyG/RusaModels/actions/runs/43801252283) at the time of this snapshot. RAG_Cerdas main workflow [43800691402](https://github.com/StrategyG/RAG_Cerdas/actions/runs/43800691402) passed.

The CI databases are disposable test environments. Passing tests do not prove that production grants, secrets, RLS policies, external identity, or deployment configuration match the repository.

## 2. Identity and authorization matrix

| Domain | Current principal / source of identity | Authorization and scope | Revocation / lifecycle | Assessment |
|---|---|---|---|---|
| RusaAi API caller | API key digest in `RUSA_API_KEYS_JSON`; principal carries subject, tenant ID and roles | Roles map to explicit permissions. Tenant is derived from the authenticated principal, not trusted from a request body or caller-supplied tenant header. Production rejects the legacy raw-key registry. | Remove the digest from the injected registry and roll the API deployment. No key ID, expiry, or automated rotation workflow is present. | Tenant binding is implemented and tested. This is a service-principal model, not human SSO. |
| RusaAi browser session | Encrypted, HttpOnly session cookie containing the API-key-backed session; Secure in production, SameSite=Lax, bounded lifetime | UI session is a wrapper around the API principal; backend authorization still validates the API credential. | Logout clears the cookie; rotating the session secret invalidates existing sessions. Underlying API-key revocation still requires registry update and deployment. | No OIDC/SAML login, MFA, or individual human identity provider is implemented. |
| RusaAi Worker | Dedicated `rusa_worker_login` DB login / `rusa_worker_runtime` group role, worker ID and per-job lease token | Worker claims from the shared queue. Tenant ID is read from the database job row; task payload is not the authority for tenant identity. Completion and state transitions are fenced by worker/lease ownership. Queue claim is intentionally cross-tenant; memory and knowledge operations are tenant-scoped. | Rotate the worker DB credential and tenant-scoped inference keys through deployment. Lease expiry/reassignment fences stale workers. | Stronger than a caller-supplied tenant model; explicit payload-tampering regression coverage remains a CHUNK 02 item. |
| RusaAi knowledge access | `rusa_knowledge_worker` DB role, set transaction-locally by the knowledge store | Tenant RLS is enforced in PostgreSQL; agent/project filters are also applied by the knowledge query layer. The inspected RLS policies are tenant-scoped, not independent project-level database policies. | Remove role membership / DB secret and redeploy. Knowledge-store production activation remains separately gated. | Disposable PostgreSQL/pgvector tests passed (65); live DB privileges are NOT_VERIFIED. |
| RusaModels API caller | Bearer token digest in `RUSAMODELS_API_PRINCIPALS`; principal carries subject and role | `viewer`, `inference`, `manager`, and `administrator` roles map to API permissions. The inference role is limited to inference invocation. Model Plane is platform-wide; tenant identity is expected to remain authoritative in RusaAi. | Remove the digest from the registry and restart/redeploy the service. No token expiry or automated rotation workflow is present. | Service-level RBAC is implemented; there is no tenant-scoped Model Plane principal in the inspected contract. |
| RusaModels internal service endpoint | One hashed `RUSAMODELS_SERVICE_TOKEN_SHA256` credential in `X-Rusa-Service-Token` | Authenticates a generic service identity for the internal platform-dependencies endpoint. It is not a per-caller subject/role registry. | Replace the hash and client secret together, then restart/redeploy. | Read-only use is narrow, but the credential is global and has no token ID or expiry. |
| IM19 worker | PostgreSQL login belonging to `im19_worker` (group role is NOLOGIN) | Worker role is distinct from reviewer role. The private IM19 schema uses forced RLS, but current policies are single-pilot role policies, not tenant predicates. | Rotate DB login credentials and remove role membership. | Valid for the isolated pilot only; not a multi-tenant identity model. |
| IM19 reviewer | Dedicated PostgreSQL LOGIN role mapped by role OID to a stable `reviewer_id` in `im19_security.reviewer_identities` | Approval triggers derive identity from `session_user`, require reviewer registration and enforce membership in `im19_approver` without worker/admin/bypass membership. Revocation is append-only and checked on each decision. | Append a revocation record; disable the old login and revoke role membership as part of operational revocation. | Database-principal binding is implemented and tested. No SSO/MFA, external identity proofing, or tenant/project authorization exists. |
| IM19 public search Edge Function | Static `RAG_CHATGPT_API_KEY` plus Supabase service-role credential | Disabled by default. The function only enters its legacy mode when explicitly enabled and `RAG_DEPLOYMENT_SCOPE=single-tenant`; it has no tenant-scoped end-user authorization contract. | Rotate provider key and injected function secrets; keep the function disabled for multi-tenant use. | Single-tenant pilot path only; never treat it as a multi-tenant public API. |

## 3. Tenant and worker trust boundaries

### RusaAi

- API key registry is the authority for caller subject, tenant and roles. The task API tests reject a caller attempting to substitute another tenant through the request body; caller-supplied tenant headers do not override the principal.
- `rusa_api_runtime` and `rusa_worker_runtime` are NOLOGIN / NOBYPASSRLS group roles. Dedicated login roles are used for API and Worker connections.
- Worker queue access is intentionally cross-tenant for claiming work. A claimed row supplies its tenant ID; worker ID and lease token fence heartbeat, completion and state transitions. This global queue-read capability must not be confused with permission to read arbitrary tenant memory or knowledge.
- `rusa_knowledge` uses transaction-local role and tenant context. The query layer applies agent/project filters. Project scope is not independently enforced by the inspected database RLS predicates, so every read/list/search/history/export path must continue to be covered by negative tests.
- The optional Worker `KnowledgeService` path can construct an embedding client from `RUSA_EMBEDDING_BASE_URL` / `RUSA_EMBEDDING_API_KEY` and call RusaModels `/openai/v1/embeddings` directly. This bypasses the RusaAi chat gateway and is not an approved production Worker path.

**CHUNK 02 mitigation in this branch:** production Worker validation now rejects `RUSA_RAG_ENABLED=true`, ambiguous RAG flag values, and Worker injection of `RUSA_KNOWLEDGE_DATABASE_URL`, `RUSA_EMBEDDING_BASE_URL`, or `RUSA_EMBEDDING_API_KEY`. The production Compose profile pins RAG off and clears those credentials. This mitigation is not mainline until this PR passes CI and is merged. It does not authorize the separate MCP process to use direct Model Server credentials.

### RusaModels

- RusaModels authenticates service principals and enforces API role permissions. It is a shared Model Plane for model registry/artifact/inference operations, not the tenant data boundary.
- The inspected migrations do not establish tenant RLS or tenant-specific database roles. Do not move tenant-owned documents, task memory, or user content into the Model Plane.
- RusaAi must remain the tenant authorization boundary. If a future cross-service contract carries tenant context for audit correlation, the receiving service must accept it only from an authenticated, specifically authorized gateway principal.

### RAG_Cerdas / IM19

- IM19 is a single institutional pilot. Its private schema and role grants are not a tenant/project isolation design.
- `im19_worker` cannot write human approvals or source-rights decisions. `im19_approver` is a separate NOLOGIN group role; reviewer logins are registered by PostgreSQL role OID and may belong only to the approver role.
- The reviewer registry and revocation ledger are in a private security schema. Triggers bind approval writes to the database login identity; the Python worker rejects superuser, BYPASSRLS and approver connections.
- The legacy Edge Function uses a static key and Supabase service role and has no tenant claim. Keep it disabled unless operating the explicitly isolated single-tenant pilot.

## 4. Database privilege matrix

| Repository / role | Login status | Main privileges / boundary | Tenant isolation |
|---|---|---|---|
| RusaAi `rusa_api_runtime` | NOLOGIN group; used through `rusa_api_login` | Task/job and rate-limit operations; read-only access to selected knowledge rows; no Worker memory/audit write capability | Tenant RLS on task/job and knowledge surfaces |
| RusaAi `rusa_worker_runtime` | NOLOGIN group; used through `rusa_worker_login` | Cross-tenant queue claim/update by design; tenant-scoped memory operations; append-only audit/policy-event writes; no general API enqueue authority | Tenant RLS on memory and scoped data; queue transitions require lease fencing |
| RusaAi `rusa_knowledge_worker` | NOLOGIN role, available only through controlled role switching | Knowledge read/write operations required by the optional knowledge service; version history is append-only and delete is restricted by migration policy | Tenant RLS; agent/project scope is enforced by query predicates and tests, not a separate project RLS predicate |
| RusaModels application DB role | Configured through `DATABASE_URL` | Shared model registry, artifact metadata and job state; API authorization is enforced by service-principal roles | No tenant RLS/tenant role model was found in the inspected migration set; this is acceptable only while the database remains platform-wide model-plane data |
| IM19 `im19_worker` | NOLOGIN group role | Worker read/write operations on pilot records; cannot create reviewer decisions or modify reviewer registry | Forced RLS, but policies are single-pilot role policies rather than tenant predicates |
| IM19 `im19_approver` | NOLOGIN group role | Narrow approval and source-rights review writes; no worker-role membership | Forced RLS, single-pilot scope |
| IM19 reviewer login | LOGIN principal, direct membership in `im19_approver` only | Can submit registered human decisions; cannot inherit worker, admin, superuser or BYPASSRLS privileges | Identity is per reviewer login; data scope remains single-pilot, not tenant-isolated |
| IM19 `im19_security` registry | Private schema | Reviewer identity and revocation administration; not readable by worker/approver roles | Administrative boundary, not a tenant data plane |

Production grants and live database role memberships have not been inspected. The matrix is derived from repository migrations, runtime checks and disposable CI tests.

## 5. Audit and correlation matrix

| System | Persisted audit fields observed | Gap |
|---|---|---|
| RusaAi | Task/audit records include tenant, task, actor, event/action, decision and JSON details; hash-chain records are append-only | No standardized request ID, resource and outcome fields are present on every task-policy event. Request IDs exist at the HTTP layer but are not consistently persisted with policy decisions. |
| RusaModels | Audit records include request ID, actor subject/role, action, resource, outcome and metadata | Tenant ID is not a first-class audit field; correlation depends on a trusted gateway request context. |
| RAG_Cerdas | IM19 audit events include document, event type and JSON details; human approval/source-rights records include reviewer identity | Generic audit events do not expose a standardized actor/request ID field. Reviewer identity is bound on human decision tables, not every worker event. |

CHUNK 02 must define one cross-service audit envelope: `request_id`, `tenant_id` (when applicable), `actor_subject`, `actor_type`, `actor_role`, `action`, `resource_type`, `resource_id`, `outcome`, `timestamp`, and safe metadata. Untrusted clients must not be able to assert an authoritative tenant or actor.

## 6. Threat model and current disposition

| Threat | Current control | Residual gap / disposition |
|---|---|---|
| Caller spoofs another tenant | Tenant comes from authenticated RusaAi API-key principal; request-body override is rejected; core PostgreSQL RLS tests pass | Add explicit negative tests for all list/search/history/export surfaces and retain exact CI evidence |
| Worker payload changes tenant | Tenant is read from the claimed DB row; lease owner/token fence state changes | Add a regression test that tampers with payload tenant metadata and proves the DB row remains authoritative |
| Cross-tenant task/memory read | Forced RLS and dedicated API/Worker roles; PostgreSQL integration tests | Live production grants remain NOT_VERIFIED |
| Cross-project knowledge leakage within one tenant | Agent/project filters in knowledge query layer; disposable pgvector tests passed | Project scope is not independently enforced by DB RLS; preserve exhaustive query-path negative tests or add DB-level project policy |
| Worker bypasses tenant gateway for embeddings | This branch rejects direct Worker RAG and direct knowledge/embedding credentials in production; Compose profile clears them | MCP is a separate process and still needs its own brokered credential/egress design; change is not mainline until merged |
| Model Server token is over-privileged | Hashed principal registry and role-based API permissions; inference role is narrower than manager/admin | No expiry/key ID/automated rotation; one global service token remains for internal dependencies |
| IM19 reviewer impersonation or role escalation | PostgreSQL login-to-reviewer binding, role-membership guard, revocation ledger and trigger checks | No external IdP, SSO, MFA, identity proofing or automated account lifecycle |
| IM19 data leaks across tenants | Private schema, separate worker/approver roles, forced RLS; Edge Function disabled by default | IM19 has no tenant/project data model. It must remain isolated and single-pilot. |
| Credential compromise persists | Hashed registries and append-only reviewer revocation | RusaAi/RusaModels require config update and restart; rotation/revocation automation and expiry are missing |
| Audit evidence cannot be correlated | RusaModels has request IDs; RusaAi has tenant/task/actor; IM19 records reviewer on human decisions | Standard request/resource/outcome fields are not consistent across systems |

## 7. Credential rotation and revocation runbook

### RusaAi API and Worker inference credentials

1. Generate a new high-entropy API credential (at least 32 random bytes); never place the raw value in Git or a ticket.
2. Compute its SHA-256 digest and add the digest with the intended subject, tenant ID and least-privilege roles to `RUSA_API_KEYS_JSON`.
3. Add the new tenant-scoped Worker inference key to `RUSA_WORKER_INFERENCE_API_KEYS` in the secret manager. Do not give the Worker a RusaModels Model Server key.
4. Deploy/restart the API and Worker with the new secret set. Verify a positive request and a negative cross-tenant request.
5. Remove the old digest/key from the secret manager and registry, deploy/restart again, then verify that the old key receives an authorization failure.
6. Rotate `RUSA_WEB_SESSION_SECRET` as a separate planned action; existing encrypted browser sessions will be invalidated.

### RusaModels service principals

1. Create a new random bearer token and calculate its SHA-256 digest.
2. Add the new digest with the intended subject and narrowest role to `RUSAMODELS_API_PRINCIPALS`; update the authenticated RusaAi gateway client secret.
3. Restart/redeploy RusaModels and the gateway; verify the new token and expected role denial.
4. Remove the old digest after successful overlap, restart, and verify the old token is rejected.
5. Rotate `RUSAMODELS_SERVICE_TOKEN_SHA256` and the internal client secret together. This is a shared service credential; do not reuse it as an inference principal.

### IM19 reviewer and source-provider credentials

1. Provision a dedicated PostgreSQL login role; grant only `im19_approver`; verify it has no worker, admin, superuser or BYPASSRLS membership.
2. Register the role OID against a stable reviewer ID and test one allowed decision plus unregistered/unauthorized negative cases.
3. To revoke, append the reviewer revocation record, set the old role NOLOGIN, revoke `im19_approver` membership and rotate/remove its password. Never delete or rewrite the revocation ledger.
4. Rotate Dropbox OAuth refresh tokens through the provider and secret manager; restart the worker to clear in-memory access tokens. Keep read-only scopes.
5. Rotate the Supabase service-role key through the provider's supported process and update function secrets. Keep the public search Edge Function disabled for multi-tenant use.

All rotations require a recorded owner, change window, validation evidence, rollback plan and post-rotation negative test. No secret value belongs in this document.

## 8. CHUNK 02 implementation backlog

| Priority | Work item | Initial disposition |
|---|---|---|
| P0 | Deny direct Worker RAG / embedding credentials in production | Implemented in this branch; pending exact-head CI and merge |
| P0 | Add Worker payload-tenant spoofing and lease-fencing negative tests | Required |
| P0 | Preserve cross-tenant negative tests for task, memory, knowledge search/list/history/export | Core and knowledge suites pass in disposable CI; expand where coverage is not explicit |
| P1 | Define human identity/IdP, MFA, tenant membership and role lifecycle for RusaAi UI/API | Not implemented; design decision required |
| P1 | Standardize audit envelope and propagate request IDs across RusaAi → RusaModels → IM19 | Partial; schema/application changes required |
| P1 | Add key expiry/IDs, rotation overlap, revocation tests and deployment runbooks | Partial; manual registry rotation only |
| P1 | Keep IM19 single-pilot; design tenant/project authorization before any multi-tenant API | Not implemented; Edge Function remains disabled by default |
| P1 | Review RusaModels global service token and trusted gateway tenant correlation | Partial; read-only internal service token is global |
| P1 | Verify production DB grants, TLS, secret mounts and egress against this matrix | NOT_VERIFIED; requires owner-approved environment evidence |

## 9. CHUNK 02 acceptance gate

CHUNK 02 cannot be marked PASS until all of the following are evidenced on exact main/PR SHAs:

- Trusted identity and tenant source are explicit for each caller/service/worker/reviewer.
- Client-supplied tenant values cannot override authenticated tenant context.
- User A cannot read, change, list, search, export or infer User B's tenant data through any API, Worker, knowledge or MCP path.
- Worker tenant identity comes from the claimed database row; payload tampering cannot alter it; stale leases cannot commit.
- Service credentials are least-privilege, revocable and rotated with tested overlap/revocation.
- Database roles are NOLOGIN/NOBYPASSRLS where intended; connection/transaction context cannot leak between tenants.
- Audit records consistently correlate actor, tenant, request, resource, action and outcome.
- IM19 remains isolated and the legacy Edge Function is not exposed as a multi-tenant service.
- Production configuration and live grants are independently verified before any production readiness claim.

Any cross-tenant negative-test failure is a hard FAIL. Passing disposable CI is necessary but does not authorize production activation.

## 10. Non-authorization

This baseline and the Worker fail-closed change do not authorize production RAG activation, IM19 public exposure, SSO/provider integration, production DDL, bulk ingestion, credential sharing, cross-system synchronization, model release, or deployment. The overall production-readiness score remains **45/100** until separate evidence-backed scorecard criteria are met.
