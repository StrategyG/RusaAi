# RUSA-AI AICode — Production Deployment Runbook

This runbook deploys AICode as a production candidate. Complete every preflight and acceptance gate before enabling production tenants.

## 1. Required infrastructure

- PostgreSQL 15+ with automated encrypted backups, point-in-time recovery, TLS, and a dedicated runtime role.
- Dedicated Linux worker host running a rootless Docker daemon.
- Persistent Git mirror and workspace volumes mounted at the same absolute paths inside the worker container and visible to the rootless Docker daemon.
- Registry containing immutable API, worker, and AICode sandbox images.
- Self-hosted or approved OpenAI-compatible model endpoint.
- Fine-grained GitHub token restricted to the repository allowlist.
- TLS-terminating reverse proxy/ingress, Prometheus-compatible scraper, and alerting.

Do not expose PostgreSQL, worker metrics, or the Docker socket publicly.

## 2. Database migration and runtime role

Apply the migration as a schema owner/admin, not as the application runtime user:

```bash
psql "$RUSA_MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_aicode_core.sql
psql "$RUSA_MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/002_aicode_lease_fencing.sql
psql "$RUSA_MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/003_aicode_task_results.sql
psql "$RUSA_MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/004_aicode_tenant_rls.sql
psql "$RUSA_MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/005_aicode_task_policy_audit.sql
psql "$RUSA_MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/006_aicode_idempotency.sql
psql "$RUSA_MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/007_rag_knowledge_pgvector.sql
psql "$RUSA_MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/008_aicode_task_list_audit.sql
```

The migrations create the durable task queue, tenant memory, rate limits, immutable audit ledger, lease fencing, task-result storage, forced row-level security (RLS), append-only task policy events, idempotency records, and RAG knowledge storage. Apply migrations 001–008 in numeric order. Migration 007 requires PostgreSQL pgvector support. The API and worker fail closed if expected columns, RLS flags, policies, audit grants, or append-only triggers are missing.

Create two independent login roles using a secret manager for passwords:

```sql
CREATE ROLE rusa_api_login LOGIN PASSWORD 'api-managed-secret' IN ROLE rusa_api_runtime;
CREATE ROLE rusa_worker_login LOGIN PASSWORD 'worker-managed-secret' IN ROLE rusa_worker_runtime;
GRANT CONNECT ON DATABASE rusa TO rusa_api_login, rusa_worker_login;
```

Use `rusa_api_login` only in `RUSA_API_DATABASE_URL` and `rusa_worker_login` only in `RUSA_WORKER_DATABASE_URL`, both with `sslmode=verify-full`. The API container must not receive the worker DSN or worker-only secrets; the worker container must not receive the API DSN or tenant API keys. Never use the schema owner or a PostgreSQL superuser in application containers.

Runtime permissions:
- API role: tenant-scoped SELECT, INSERT, UPDATE on jobs; tenant-scoped rate-limit maintenance.
- Worker role: cross-tenant SELECT/UPDATE on the durable queue; tenant-scoped memory; append to the immutable audit ledger and task policy event table.
- Neither role can perform DDL, grant privileges, or mutate existing audit records.
- The API role cannot read memory or audit tables.

The database policies use transaction-local `rusa.tenant_id` context. The worker may claim jobs across tenants, but its memory operations remain tenant-scoped. Verify these boundaries with `tests/test_postgres_backends.py`; application-level WHERE clauses alone are not sufficient.

## 3. GitHub token permissions

Create a fine-grained token scoped only to each authorized repository:
- Metadata: Read-only
- Contents: Read and write
- Pull requests: Read and write
- Checks: Read-only

Do not grant Actions administration, repository administration, secrets, environments, workflow write, or organization permissions. Branch protection must prevent direct writes to the default branch and require human review before merge.

The repository origin must be exactly `https://github.com/{owner}/{repo}.git`. Embedded credentials, SSH remotes, alternate hosts, and redirects are rejected.

## 4. Model endpoint requirements

Configure an OpenAI-compatible endpoint:
- `GET /models` must succeed for readiness.
- `POST /chat/completions` must accept standard chat requests.
- The coding endpoint must support JSON-object response format and return the AICode action envelope.

The coding endpoint receives repository files and task context. Confirm data residency, retention, access controls, and processor terms before sending proprietary code. Prefer a self-hosted endpoint when required by policy.

## 5. Build and publish immutable images

Build all three images from the reviewed commit:

```bash
docker build -t ghcr.io/ORG/rusa-ai:RELEASE .
docker build -f Dockerfile.worker -t ghcr.io/ORG/rusa-aicode-worker:RELEASE .
docker build -f Dockerfile.agent-sandbox -t ghcr.io/ORG/rusa-aicode-sandbox:RELEASE .
docker push ghcr.io/ORG/rusa-ai:RELEASE
docker push ghcr.io/ORG/rusa-aicode-worker:RELEASE
docker push ghcr.io/ORG/rusa-aicode-sandbox:RELEASE
```

Resolve the sandbox digest from the registry and configure `RUSA_SANDBOX_IMAGE` as an immutable reference:

```text
ghcr.io/ORG/rusa-aicode-sandbox@sha256:REGISTRY_DIGEST
```

The worker refuses a mutable sandbox tag in production. Pull the exact digest into the dedicated rootless Docker daemon. Do not assume an image in the rootful Docker daemon is available to the rootless daemon.

The current CI builds images but does not publish them to a registry automatically. Pin base images and third-party build actions to reviewed immutable digests in the release pipeline.

## 6. Prepare Git mirrors and storage

Create persistent directories on the worker host:

```bash
sudo install -d -o 10001 -g 10001 -m 0750 /var/lib/rusa/workspaces
sudo install -d -o 10001 -g 10001 -m 0750 /var/lib/rusa/mirrors
```

Create one bare mirror per authorized repository:

```bash
sudo -u rusa-worker git clone --bare \
  https://github.com/StrategyG/RusaAi.git \
  /var/lib/rusa/mirrors/StrategyG/RusaAi.git
```

The mirror must retain the exact credential-free HTTPS origin. AICode refreshes only the requested base branch before creating a task worktree. Fetch/push credentials are supplied through a short-lived askpass helper and are never embedded in the remote URL.

Mount the workspace root at the same absolute host path inside the worker container and make it visible to the rootless Docker daemon. This is required for sandbox bind mounts.

## 7. Production environment

Copy `.env.example` to a secret-managed environment file. Do not commit the production file.

API settings:

```dotenv
ENVIRONMENT=production
RUSA_AICODE_ENABLED=false  # fail-closed; set true only after all release gates pass
RUSA_API_DATABASE_URL=postgresql://rusa_api_login:REDACTED@DB_HOST:5432/rusa?sslmode=verify-full
RUSA_API_KEYS_JSON='{"<64-character-sha256-digest>":{"subject":"service-acme","tenant_id":"tenant-acme","roles":["tenant_admin"]}}'
RUSA_METRICS_API_KEY=LONG_RANDOM_OPERATOR_ONLY_KEY_AT_LEAST_32_BYTES
RUSA_TENANT_REPOSITORIES={"tenant-acme":["StrategyG/RusaAi"]}
RUSA_MODEL_BASE_URL=https://MODEL_HOST/v1
RUSA_MODEL_NAME=rusa-ai
RUSA_RATE_LIMIT_CHAT_PER_MINUTE=60
RUSA_RATE_LIMIT_TASKS_PER_MINUTE=10
RUSA_RATE_LIMIT_DEFAULT_PER_MINUTE=120
RUSA_MAX_PENDING_TASKS_PER_TENANT=1000
```

Worker settings:

```dotenv
RUSA_WORKER_DATABASE_URL=postgresql://rusa_worker_login:REDACTED@DB_HOST:5432/rusa?sslmode=verify-full
RUSA_AUDIT_HMAC_KEY=AT_LEAST_32_RANDOM_UTF8_BYTES
RUSA_WORKSPACE_ROOT=/var/lib/rusa/workspaces
RUSA_GIT_MIRROR_ROOT=/var/lib/rusa/mirrors
RUSA_GITHUB_TOKEN=FINE_GRAINED_REPOSITORY_TOKEN
RUSA_SANDBOX_IMAGE=ghcr.io/ORG/rusa-aicode-sandbox@sha256:REGISTRY_DIGEST
RUSA_TASK_TIMEOUT_SECONDS=3600
RUSA_MAX_PENDING_TASKS_PER_TENANT=1000
RUSA_MAX_ACTIVE_TASKS=1
RUSA_LEASE_HEARTBEAT_INTERVAL_SECONDS=10
RUSA_MAX_PARALLEL_WORKERS=1
RUSA_CI_TIMEOUT_SECONDS=900
RUSA_CI_POLL_SECONDS=15
RUSA_METRICS_PORT=9100
RUSA_METRICS_BIND=0.0.0.0
RUSA_DOCKER_SOCKET=/run/user/1001/docker.sock
RUSA_DOCKER_GID=1001
```

The worker also requires `DOCKER_HOST=unix:///run/docker.sock`, supplied by Compose when the rootless socket is mounted. Set the socket path and group ID to actual host values.

Keep `RUSA_AICODE_ENABLED=false` during installation, migration, smoke testing, security review, and restore drills. The API rejects task intake/retry and workers do not claim jobs while disabled. Set it to `true` only after the final release gate is signed off. To pause AICode, set it to `false` and restart the API and worker services: the API then rejects new task submissions/retries and workers stop claiming new jobs. Already-running work is not forcibly terminated; cancel it through the API or let it reach a safe checkpoint. Existing task status, cancellation, health, and metrics endpoints remain available.

Generate raw API keys with a cryptographically secure generator, give each raw key only to its client through a secret manager, and configure only its lowercase SHA-256 digest in `RUSA_API_KEYS_JSON`. Each registry claim must include a unique subject, tenant_id, and one or more supported roles (`platform_admin`, `tenant_admin`, `developer`, `read_only`). Production rejects legacy raw `RUSA_API_KEYS` and `RUSA_API_KEY`. Keep the audit HMAC key at least 32 UTF-8 bytes. Never put credentials in task context, repository files, Docker build arguments, or Git remotes.

## 8. Compose deployment

The included Compose file is a development/single-host example. It uses a local PostgreSQL service and is not a complete HA production topology. For production:
1. Replace the Compose PostgreSQL service with managed PostgreSQL.
2. Use separate `rusa_api_login` and `rusa_worker_login` database users; never reuse the migration owner or a shared runtime login.
3. Mount persistent mirror/workspace volumes at the configured paths.
4. Mount only the dedicated rootless Docker socket into the worker.
5. Confirm the rootless daemon sees the same workspace host paths.
6. Restrict the API to a TLS-terminating reverse proxy.
7. Keep worker metrics on an internal network only.

For local development only, use the bundled PostgreSQL stack:

```bash
docker compose up -d
docker compose --profile worker up -d --build
```

For production, set the API and worker image references to immutable registry digests in the deployment environment, keep `RUSA_AICODE_ENABLED=false` for the initial rollout, then validate and deploy the external-database stack:

```bash
export RUSA_API_IMAGE=ghcr.io/ORG/rusa-ai@sha256:API_IMAGE_DIGEST
export RUSA_WORKER_IMAGE=ghcr.io/ORG/rusa-aicode-worker@sha256:WORKER_IMAGE_DIGEST
docker compose -f docker-compose.production.yml config
docker compose -f docker-compose.production.yml pull
docker compose -f docker-compose.production.yml up -d
docker compose -f docker-compose.production.yml ps
docker compose -f docker-compose.production.yml logs --tail=200 api worker
```

The production Compose file has no local PostgreSQL service. It requires separate managed TLS DSNs for API and worker, blanks the opposite service's DSN and privileged secrets, requires the dedicated rootless Docker socket, and fails configuration if image/socket/workspace settings are missing.

API and worker containers run non-root, read-only root filesystems, dropped capabilities, no-new-privileges, and resource limits. The worker temporary filesystem permits execution because Git's short-lived askpass helper must be executable; keep that container isolated.

## 9. Smoke tests

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/health/ready
curl -fsS -H "X-API-Key: $RUSA_METRICS_API_KEY" http://127.0.0.1:8000/metrics
```

Submit a bounded task:

```bash
curl -fsS -X POST http://127.0.0.1:8000/v1/aicode/tasks \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $RUSA_API_KEY" \
  -d '{
    "objective": "Add a small unit test for the health endpoint",
    "repository": {"full_name": "StrategyG/RusaAi", "base_branch": "main"},
    "acceptance_criteria": ["The new test passes", "Ruff and Bandit checks pass"]
  }'
```

The API returns a task UUID and generated feature branch. Poll `GET /v1/aicode/tasks/{task_id}` with the same tenant key. The response includes the durable `result` object after completion (Pull Request URL, commit SHA, and actual lease-specific branch), or after human review is required. The initial submission returns `work_branch_prefix`; the actual branch is assigned per worker lease to prevent stale workers from colliding. Only terminal `failed` tasks can be requeued through generic `POST /v1/aicode/tasks/{task_id}/retry`. An `awaiting_approval` task is not a retry state and requires a separate, explicit operator approval path. The current mainline legacy AICode path does not yet expose a plan-fingerprint-bound approval-resume endpoint; treat automated resume of these tasks as BLOCKED until that path is reviewed and implemented. The retry endpoint does not approve PR merges or deployments.

A successful task creates a draft pull request and requires both GitHub Actions jobs to pass. If checks are missing, fail, or cannot be verified, the task remains `awaiting_approval`; it is never silently marked successful.

## 10. Monitoring and recovery

Scrape authenticated API `/metrics` and internal worker `worker:9100/metrics`. Metric families include API request count/duration, AICode task count/duration, worker steps, and tool executions.

Alert on API 5xx/p95 latency, rate-limit rejections, queue age/retries/awaiting-approval backlog, worker lease loss, sandbox timeouts, CI verification failures, audit integrity failures, and PostgreSQL saturation.

The repository exposes metrics and logs but does not provision Prometheus, dashboards, alert rules, or distributed tracing. Those remain deployment responsibilities.

Before production:
- Enable encrypted PostgreSQL backups and point-in-time recovery.
- Run a documented restore drill.
- Preserve audit ledger and HMAC key material.
- Keep the audit HMAC key stable; key rotation requires a versioned checkpoint procedure and is not automated here.
- Monitor workspace/mirror disk usage and define cleanup for failed/blocked tasks.
- Verify worker restart reclaims expired leases and retains memory/audit history.

Rollback:
1. Stop task intake and allow/cancel active leases.
2. Roll back API/worker images to the last reviewed digest.
3. Do not roll back or delete the audit ledger.
4. Apply only forward database migrations; do not drop tables.
5. Verify readiness, queue state, audit integrity, and GitHub branch/PR state before resuming.

## 11. Release gate

Do not declare production certification until:
- PR head CI passes, including PostgreSQL integration tests and both image builds.
- Migration and runtime role permissions are verified.
- Tenant isolation tests show zero cross-tenant access.
- Sandbox adversarial and resource exhaustion tests pass.
- GitHub token is least-privilege and branch protection is active.
- Model endpoint is reachable and passes task-quality evaluation.
- Backup/restore and worker-restart drills pass.
- Metrics are scraped and alerts are tested.
- Security review and operator sign-off are recorded.

## Runtime hardening rollout — 2026-11-06

### Lease fencing migration

The AICode queue now uses a per-claim UUID lease token. Every worker heartbeat and state transition must present the current token, and the database rejects updates from expired or superseded leases. Each attempt also receives a distinct feature branch and worktree to prevent stale retries from sharing a checkout.

Before deploying this version:

1. Stop and drain all AICode workers.
2. Back up the PostgreSQL database.
3. Apply `migrations/002_aicode_lease_fencing.sql` with the database migration owner.
4. Confirm `public.aicode_jobs.lease_token` exists.
5. Deploy the API and worker images built from the same commit.
6. Start one canary worker, verify claim/heartbeat/complete, then scale workers.
7. Confirm old worker processes are fully stopped before re-enabling normal task intake.

The migration requeues tasks that were marked running so they can be claimed with a fresh token. Do not run old and new worker versions concurrently during this migration.

### Request and model response limits

The API now defaults to a 1 MiB request-body limit and a 15-second request-body read timeout. Configure:

- `RUSA_MAX_REQUEST_BYTES`: integer from 1,024 to 50,000,000 bytes.
- `RUSA_REQUEST_BODY_TIMEOUT_SECONDS`: number from 0.1 to 60 seconds.
- `RUSA_MODEL_MAX_RESPONSE_BYTES`: integer from 1 to 50,000,000 bytes; default 2,000,000.

The ASGI middleware counts received bytes rather than trusting Content-Length, so chunked requests are also bounded. Both agent and inference provider adapters stream and cap response bodies before JSON parsing.


### Hierarchical orchestration and worker controls

The root orchestrator dispatches specialists through three manager groups:

- Repository manager: Analyst, then Architect.
- Engineering manager: Developer, then Debugger.
- Quality manager: Tester and Security run concurrently after the Developer dependency is satisfied.

The deterministic DAG is Analyst → Architect → Developer → Debugger, with Developer also enabling parallel Tester and Security branches. Failed quality evidence triggers a bounded Developer/Debugger repair cycle. Missing mandatory evidence moves the task to `awaiting_approval`; it never produces a success state.

Runtime controls:

- `RUSA_MAX_PARALLEL_WORKERS`: integer from 1 to 32; default 4.
- `RUSA_MAX_REPAIR_CYCLES`: integer from 0 to 5; default 2.

These controls limit in-process concurrency and repair attempts; they do not autoscale Kubernetes worker replicas.

### Remaining production certification blockers

The current code still requires independent verification before it can be certified production-ready:

- Database RLS and separate API/worker database identities are implemented through migrations 004–005 and fail-closed startup checks. Before certification, execute adversarial cross-tenant tests against the actual production login roles and verify grants, policies, audit immutability, and off-host retention on the target PostgreSQL instance.
- The current `docker-compose.yml` uses the bootstrap PostgreSQL account `rusa` for both API and worker connections. Treat this compose profile as development-only; do not use it as a production credential model.
- The runtime still uses the custom PostgreSQL lease queue rather than Temporal. Agent manager dispatch is currently in-process (no external A2A transport), and the workspace tool gateway is not yet exposed through an MCP server/client implementation.
- Production rollout still requires independent verification of the least-privilege role split, tenant RLS policies, TLS-enabled database URLs, and a migration/rollback plan. The Compose syntax check does not certify these controls.
- Semantic/vector memory (pgvector) and model-generated dynamic DAG planning remain unimplemented. The current runtime now has repository, engineering, and quality managers, a deterministic dependency DAG, parallel QA dispatch, and a bounded repair loop. It does not yet autoscale worker replicas.
- Run PostgreSQL integration tests, rootless Docker sandbox adversarial tests, load/soak tests, restore drills, and external security review against the target deployment.
- Keep merge, release, production deployment, secret rotation, permission changes, and database migration execution under explicit operator approval.


### Task authorization audit

Task endpoints require a verified credential in every environment. Production
uses `RUSA_API_KEYS_JSON`, keyed by the SHA-256 digest of each raw API key; each
claim binds a subject, tenant ID, and roles. Raw `RUSA_API_KEYS` mappings are a
development-only compatibility path and production startup rejects them. The
tenant ID is never accepted from request headers or task payloads. Task routes
enforce role permissions and tenant scope before accessing the durable queue.

Migration `005_aicode_task_policy_audit.sql` creates the append-only
`aicode_task_policy_events` table. Both API and worker runtime roles are
non-superuser and NOBYPASSRLS. FORCE RLS limits API inserts to the authenticated
tenant and worker inserts to the claimed task tenant; API cannot read audit
events, and runtime roles cannot update, delete, or truncate them. API actors
are prefixed `api:`; worker actors are prefixed `worker:` and may emit only
`task.execute` events. Unauthenticated attempts use a null tenant and the
restricted `task.authenticate/deny` shape. Events contain only tenant ID,
task ID, actor, action, decision, and database timestamp—never credentials,
lease tokens, or task payloads. The API and worker verify the audit table,
RLS, grants, and append-only triggers at production startup. Task create/read/
retry/cancel events are inserted in the same PostgreSQL transaction as their
queue operation; worker execution allow/deny events are committed in the same
transaction as the live-lease check. A failed audit insert therefore rolls back
the associated queue mutation or prevents workspace execution. The INFO logger
is a secondary operational stream; PostgreSQL is the durable audit record.

The worker queue role is intentionally a trusted cross-tenant consumer so it
can claim jobs. Before opening a workspace it validates the row's tenant
repository allowlist and current lease. Queue mutations remain fenced by
tenant, worker ID, lease token, running status, and lease expiry. The custom
transaction-local `rusa.tenant_id` setting is a defense against accidental
unscoped SQL, not a cryptographic boundary against arbitrary SQL execution
under a shared runtime login. Keep all SQL parameterized and do not expose
arbitrary SQL; deployments requiring isolation against a compromised service
process must move to per-tenant database identities or signed database
assertions. Export the append-only audit stream to a restricted, immutable
off-host retention system and test restore/retention independently.


## Runtime hardening — 2026-11-15

### Queue admission and concurrency bounds

The durable queue now enforces two independent PostgreSQL-backed bounds without changing the schema:

- `RUSA_MAX_PENDING_TASKS_PER_TENANT`: default 1000, valid range 1–1,000,000. Counts `queued`, `running`, and `awaiting_approval` rows for one tenant. API replicas serialize the count-and-insert operation with a transaction-scoped tenant advisory lock. Saturation returns HTTP 429 with a `Retry-After` header.
- `RUSA_MAX_ACTIVE_TASKS`: default 1, valid range 1–64. Worker replicas serialize active-lease counting and claiming with one transaction-scoped global advisory lock. Expired leases do not consume an active slot; stale workers remain fenced by the lease token.

An identical `Idempotency-Key` replay is resolved before capacity enforcement and returns the original task ID/state. A replay with a different payload remains a 409 conflict. All API replicas must use the same pending-task limit; all worker replicas must use the same active-task limit. Mixed old/new replicas can bypass these code-level bounds, so stop intake and drain workers before rolling out the new version to every replica.

`RUSA_MAX_PARALLEL_WORKERS` remains the in-task specialist concurrency limit. It is not the global queue-claim limit. Keep these settings separate and size both for the actual CPU/memory and inference-admission capacity.

### Lease fencing, tool audit, and cancellation

- Legacy AICode memory reads/writes and developer workspace writes are executed under the active queue-row lock through `with_lease_owned`. The callback is limited to short local/database operations; model inference, Git/network requests, and sandbox execution must not hold this lock.
- Read-only Tool Gateway invocations verify the current task lease before execution. The gateway writes a HMAC-chained `tool.intent` event before execution and `tool.outcome` after allow/deny/result. Events include actor, task, tenant, tool, repository/project scope, timestamp, decision, and result status; they exclude raw arguments, file contents, model/tool output, credentials, and lease tokens. Production fails closed if the required audit store cannot persist the intent.
- `RUSA_LEASE_HEARTBEAT_INTERVAL_SECONDS` defaults to 10 seconds and accepts 1–60 seconds. Lease revocation/cancellation is detected on the next heartbeat; sandbox execution receives the cancellation signal and force-removes its named Docker container. Other I/O remains bounded by its own timeout. This is cooperative cancellation, not a hard real-time guarantee.
- `fail(..., retryable=False)` terminates a task immediately. The worker retries only classified timeout/connection transport failures; deterministic policy/permission and blocked verification results are terminal. Attempts remain bounded by `max_attempts` (1–10), and retry delay is bounded to 0–3600 seconds.
- Generic retry now accepts only `failed` tasks. Approval-pending tasks must not be converted into retries. The legacy mainline path still lacks an explicit approval-resume endpoint, so such tasks require operator handling and are a known lifecycle gap.

### Rollout and rollback

No database migration is introduced by these changes. The existing `aicode_jobs`, lease token, idempotency, tenant RLS, and HMAC audit schema are reused.

1. Keep `RUSA_AICODE_ENABLED=false`; stop/drain every worker and wait for running leases to finish or be explicitly cancelled.
2. Take/verify a database backup and record the current API/worker image digests.
3. Deploy the reviewed API image to every API replica with the same `RUSA_MAX_PENDING_TASKS_PER_TENANT` value.
4. Deploy the reviewed worker image to every worker replica with the same `RUSA_MAX_ACTIVE_TASKS` and `RUSA_LEASE_HEARTBEAT_INTERVAL_SECONDS` values.
5. Run queue admission, idempotency replay, claim-capacity, lease-loss, audit-chain, and sandbox-cancellation smoke tests. Verify the production database roles and audit append-only controls.
6. Re-enable intake only after operator approval. Monitor 429s, pending age, active leases, heartbeat failures, sandbox cancellations/timeouts, and audit write failures.

Rollback does not require a schema rollback. Disable intake, stop/drain workers, deploy the last reviewed API and worker image digests together, and verify queue/lease state before resuming. The old images do not enforce the new code-level bounds; keep intake disabled until the rollback topology is stable. Never delete or rewrite audit records. Do not run destructive migrations.

### Implementation evidence and remaining gaps

See [Agent–Worker Runtime traceability](engineering/AGENT_WORKER_RUNTIME_TRACEABILITY_20261115.md) and [implementation notes](agents/AGENT_WORKER_RUNTIME_IMPLEMENTATION.md). The current branch is a code-level hardening candidate, not a production certification. Production still requires actual deployment-role adversarial tests, load/soak, backup/restore and rollback drills, off-host immutable audit retention, monitoring/alert verification, and operator sign-off.
