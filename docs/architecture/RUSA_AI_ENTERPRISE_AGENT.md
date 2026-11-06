# RUSA-AI Enterprise Hierarchical Agent Architecture

**Architecture decision:** hierarchical multi-agent, full autonomy inside an explicit allowlist, enterprise deployment profile.

## 1. System boundary and operating model

RUSA-AI is split into two independently scalable planes:

- **Inference plane:** the existing FastAPI/Transformers service serves model completions.
- **Agent control plane:** task API, PostgreSQL queue, master orchestrator, specialist workers, GitHub tool gateway, memory, and audit.

The agent control plane calls an OpenAI-compatible model endpoint. Model output is always parsed as a typed proposal. It is never executed as source code or shell input.

Full autonomy means the system can inspect a repository, create a work branch, modify allowlisted files, open a draft pull request, observe CI, and perform bounded repair cycles without a human approving each routine step. It does **not** mean unrestricted host access. Merge, deployment, release, credential access, permission changes, workflow changes, and destructive operations are not exposed as agent tools. Those operations remain under GitHub branch protection and human release approval.

## 2. Logical architecture

~~~mermaid
flowchart TB
    Client[Developer / API Client] --> API[Agent Task API]
    API --> Q[(PostgreSQL Durable Queue)]
    Q --> W[Queue Worker Pool]
    W --> M[Master Orchestrator]
    M --> P[Policy Engine]
    M --> R[Hierarchical Router]
    R --> A[Analyst]
    R --> AR[Architect]
    R --> D[Developer]
    R --> DB[Debugger]
    R --> T[Tester]
    R --> S[Security]
    A --> M
    AR --> M
    D --> M
    DB --> M
    T --> M
    S --> M
    M --> G[GitHub Tool Gateway]
    G --> GH[GitHub Repository / Draft PR / CI]
    M <--> MEM[(Tenant-scoped PostgreSQL Memory)]
    M --> AUD[HMAC Audit Log]
    M <--> LLM[OpenAI-compatible Model Provider]
    GH -->|Checks and CI evidence| T
    T -->|Pass / Fail / Pending| M
    M -->|Checkpoint / retry / lease| Q
~~~

### Runtime sequence

~~~mermaid
sequenceDiagram
    participant C as Client
    participant API as Task API
    participant Q as PostgreSQL Queue
    participant M as Master
    participant W as Specialist Worker
    participant G as GitHub Gateway
    participant CI as GitHub Actions
    participant MEM as Tenant Memory
    C->>API: POST task + tenant + idempotency key
    API->>Q: Enqueue task
    Q-->>API: task_id
    API-->>C: 202 Accepted
    M->>Q: Claim with lease / SKIP LOCKED
    M->>MEM: Retrieve tenant-scoped context
    loop Analyst -> Architect -> Developer
        M->>W: Typed task + bounded prior results
        W-->>M: Validated JSON proposal
        M->>M: Role authorization + risk policy
        M->>G: Allowlisted tool call
        G-->>M: Result + evidence
    end
    M->>G: Create or reuse draft pull request
    G->>CI: Branch push triggers CI
    loop CI pending or bounded repair
        M->>G: Read check-runs
        G-->>M: Pending / failed / passed
        opt Failed and repair budget remains
            M->>W: Debugger diagnosis
            W-->>M: Proposed repair
            M->>G: Write allowlisted file on work branch
        end
    end
    M->>W: Security review
    W-->>M: Risk assessment + blockers
    M->>Q: Persist terminal state / checkpoint
    M->>MEM: Store outcome summary
    M->>M: Append signed audit event
~~~

## 3. Agent hierarchy and responsibilities

| Agent | Responsibility | Allowed tools |
|---|---|---|
| Master Orchestrator | Task lifecycle, stage routing, policy enforcement, checkpoints, retries, final decision | Queue, policy, audit, memory, provider |
| Analyst | Inspect repository, identify current state, dependencies, constraints, acceptance coverage | read_file, list_files |
| Architect | Propose minimal design, affected files, interfaces, migration and test plan | read_file, list_files |
| Developer | Create isolated branch, implement changes, open/reuse draft PR | create_branch, read_file, list_files, write_file, create_pull_request |
| Debugger | Interpret CI evidence and propose bounded corrective changes | get_ci_status, read_file, list_files, write_file |
| Tester | Verify required GitHub Actions checks and acceptance evidence | get_ci_status |
| Security | Review changes for secrets, unsafe paths, permission escalation, injection, and policy violations | read_file, list_files, get_ci_status |

Workers do not call other workers directly. Every worker returns to the master. This avoids circular delegation, unbounded agent spawning, and untraceable side effects.

## 4. Communication protocol

Worker requests and responses are JSON contracts. Each task has:

- tenant_id: mandatory isolation boundary.
- task_id: stable identity.
- idempotency_key: prevents duplicate submissions per tenant.
- repository: owner/name plus base and isolated work branch.
- acceptance_criteria: 1–20 measurable criteria.
- stage_index and repair_cycles: durable orchestration checkpoint.
- request ID: API correlation header and audit task correlation.

Worker response contract:

- summary: concise evidence-based summary.
- tool_calls: zero or more allowlisted calls with JSON arguments.
- blockers: explicit conditions that stop the workflow.
- risk: low, medium, high, or critical.
- criteria_results: boolean result for every exact acceptance criterion.

A worker response with unknown keys, invalid JSON, unsupported tools, excessive calls, incomplete acceptance criteria, or invalid field types is rejected. Repository content, retrieved memory, and tool results are untrusted data and cannot override system policy.

## 5. Worker orchestration and state machine

Normal path:

1. RECEIVED -> OBSERVING (Analyst).
2. OBSERVING -> PLANNING (Architect).
3. PLANNING -> EXECUTING (Developer).
4. EXECUTING -> VERIFYING (Tester).
5. If CI is pending: persist WAITING_EXTERNAL, release the worker lease, and re-check after a delay.
6. If CI fails: Debugger -> Developer repair -> Tester. Maximum two repair cycles by default.
7. If CI passes: Tester must mark every acceptance criterion true with evidence; then Security independently reviews the criteria and risk.
8. SUCCEEDED only when all required CI checks pass, all acceptance criteria are verified by Tester and Security, and Security has no blockers.

Terminal states: SUCCEEDED, BLOCKED, FAILED, CANCELLED, and AWAITING_APPROVAL. An approval-required task does not silently continue. Protected files and high/critical risk are not auto-approved.

The PostgreSQL queue uses tenant-scoped idempotency, FOR UPDATE SKIP LOCKED, bounded leases, attempt limits, delayed rechecks, and checkpoint payloads. A worker crash allows another worker to reclaim an expired lease. Tool operations are designed to be idempotent where possible: branch creation reuses an existing branch, file writes avoid no-op commits, and pull request creation reuses an existing open PR. Terminal task records are purged after 30 days by default.

## 6. Memory architecture

PostgreSQL is the source of truth for task and episodic memory. Queue and memory tables enable and force PostgreSQL row-level security. The API connects with a restricted non-BYPASSRLS role and sets a transaction-local tenant context only after validating that tenant's API key. Workers use a separate BYPASSRLS database role to claim tasks across tenants; this credential is isolated from the API container.

| Memory type | Purpose | Lifecycle |
|---|---|---|
| Working memory | Current task, stage, tool evidence, worker results | Stored in task checkpoint; terminal records default to 30-day retention |
| Episodic memory | Short outcome summaries from completed tasks | Tenant-scoped; default TTL 90 days |
| Semantic/project memory | Curated repository conventions and stable project facts | Namespace-scoped; reviewed and versioned |
| Procedural memory | Validated workflows and runbooks | Stored as reviewed documentation, not arbitrary model-generated commands |

Every retrieval is filtered by tenant_id and namespace. Memory stores outcome summaries, not secrets or raw credentials. Expired records can be purged; tenant deletion is supported. Production deployments must enable encrypted database volumes, backups, access logging, and a documented deletion/retention policy.

The initial implementation uses PostgreSQL full-text search. Vector retrieval/pgvector is an optional future adapter and must pass tenant-isolation and retrieval-quality tests before activation.

## 7. Tools and permissions

| Tool | Capability | Boundary |
|---|---|---|
| GitHub read | Read/list regular text files | One repository and approved base/work refs |
| GitHub write | Commit text changes | Isolated non-protected work branch only |
| GitHub PR | Open or reuse draft PR | Never merge |
| GitHub CI | Read branch check-runs | Evidence only; never mark checks successful |
| Model provider | JSON completion | HTTPS endpoint, bounded timeout, no redirects |
| PostgreSQL queue | Enqueue/claim/ack/retry | Tenant-scoped API access; worker lease and idempotency |
| Memory | Search/store summaries | Tenant-scoped, TTL-limited, RLS-enabled |
| Audit | HMAC-chained event record | No raw secrets or file contents |

No arbitrary shell, unrestricted network, local filesystem access, secret retrieval, force push, merge, deployment, release, or permission-management tool is registered. GitHub Actions is the isolated execution environment for tests and build validation. The agent does not execute repository code on the worker host.

## 8. Evaluation and release gates

### Per-task hard gates

- Contract validation and role/tool allowlist pass.
- Policy allows every side effect.
- Every acceptance criterion is marked true by both Tester and Security.
- Required CI jobs complete successfully on the exact work-branch commit.
- Security worker reports no blockers and no high/critical risk.
- Audit chain verifies.
- Task result and evidence are persisted.

### Quality score

Use a 0–100 weighted score for reporting, never as a substitute for hard gates:

- Acceptance coverage: 30%.
- Test and CI evidence: 25%.
- Security review: 20%.
- Maintainability and architecture: 15%.
- Operational evidence and documentation: 10%.

Release target: score >= 90/100 and every hard gate passes. A failed hard gate always blocks release regardless of score.

### Operational metrics

Track task success rate, blocked/approval rate, retry and repair rate, queue wait time, task duration p50/p95, provider latency/error rate, token cost, GitHub API error rate, policy denials, CI failure categories, memory retrieval hit rate, and tenant-isolation test results.

### Evaluation suite

- Contract/schema tests and state-transition tests.
- Tool authorization and protected-path tests.
- Prompt-injection and malicious repository-content tests.
- Cross-tenant memory and task access tests using the restricted API database role.
- Idempotency, duplicate delivery, lease expiry, and worker crash tests.
- CI failure/repair/pending/resume tests.
- Load and soak tests with multiple worker replicas.
- Model task benchmark with a versioned dataset and human-reviewed golden outcomes.

## 9. Deployment topology

- rusa-api: existing inference API.
- rusa-agent-api: task submission/status API.
- rusa-agent-worker: horizontally scalable worker process.
- rusa-agent-db: PostgreSQL for task queue and memory.
- GitHub Actions: isolated build/test runner.
- Model provider: configured OpenAI-compatible endpoint.

Use a TLS reverse proxy/API gateway in production. Enforce distributed rate limits, request size limits, provider/GitHub egress allowlists, secret rotation, PostgreSQL backups, point-in-time recovery, monitoring, alerting, and a tested rollback procedure.

## 10. Production status boundary

This implementation is an enterprise-oriented runtime foundation, not a claim that the full RUSA-AI product is production-certified. A real provider, GitHub token with minimum repository permissions, PostgreSQL instance, model endpoint, model evaluation dataset, and target deployment environment must be configured and tested. A green CI pipeline proves code checks for the commit; it does not prove model quality, capacity, security certification, or production SLOs.


## 11. Runtime persistence, isolation and release evidence

The following controls are implemented in the production-hardening branch and must be validated against the deployed PostgreSQL roles and ASGI stack:

- **Tenant-bound repository scope:** API allowlists are per tenant. The API derives work-branch names from a tenant digest and idempotency identity; GitHub operations reject a task whose branch does not match that namespace.
- **Idempotent task submission:** reusing an idempotency key is allowed only when the normalized task payload matches the original request. A changed objective, repository, branch, criteria or context is rejected.
- **Lease fencing:** every claim receives a cryptographically random token. Lease renewal, checkpoint persistence, retry and acknowledgement require the current token, preventing a stale worker from committing after reassignment.
- **Durable orchestration checkpoints:** worker results and stage transitions are persisted while the lease is held. Checkpoint failure is propagated and cannot be acknowledged as a successful task.
- **Bounded request intake:** actual streamed body bytes are counted before JSON parsing, with a 256 KiB default limit and a 15-second body timeout.
- **Fail-closed readiness:** the queue is ready only when forced row-level security, the tenant policy and the lease-token column are present.

### Release verification still required

Run the PostgreSQL integration suite with the least-privilege API and worker roles; verify crash/restart recovery, stale-token rejection, concurrent claims, and tenant isolation. Test request limits through the real reverse proxy. Independently validate trusted CI producer identity, immutable artifact-to-commit mapping, backups/restores, load/soak SLOs and model quality. Unit tests and a green CI run alone do not certify the complete RUSA-AI product for production.
