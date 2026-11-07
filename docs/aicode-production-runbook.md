# RUSA-AI AICode — Production Deployment Runbook

This runbook deploys AICode as a production candidate. Complete every preflight and acceptance gate before enabling production tenants.

## 1. Required infrastructure

- PostgreSQL 15+ with automated encrypted backups, point-in-time recovery, TLS, and a dedicated runtime role.
- Dedicated Linux worker service with no Docker socket or Docker daemon access.
- Separate sandbox-executor service on a dedicated rootless Docker daemon.
- Persistent Git mirror and workspace volumes; the worker mounts workspaces read/write, while the sandbox executor mounts them read-only.
- A separate writable snapshot volume shared at the same absolute host/container path with the rootless Docker daemon.
- Registry containing immutable API, worker, sandbox-executor, and AICode sandbox images.
- Self-hosted or approved OpenAI-compatible model endpoint.
- GitHub App installed only on the authorized repositories, with a worker-only private key and per-tenant installation mapping.
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
psql "$RUSA_MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/008_aicode_sandbox_lease_verifier.sql
```

The migrations create the durable task queue, tenant memory, rate limits, immutable audit ledger, lease fencing, task-result storage, forced row-level security (RLS), and append-only task policy events. Apply migrations 001–008 in numeric order. The API and worker fail closed if expected columns, RLS flags, policies, audit grants, or append-only triggers are missing.

Create three independent login roles using a secret manager for passwords:

```sql
CREATE ROLE rusa_api_login LOGIN PASSWORD 'api-managed-secret' IN ROLE rusa_api_runtime;
CREATE ROLE rusa_worker_login LOGIN PASSWORD 'worker-managed-secret' IN ROLE rusa_worker_runtime;
CREATE ROLE rusa_sandbox_login LOGIN PASSWORD 'sandbox-managed-secret' IN ROLE rusa_sandbox_runtime;
GRANT CONNECT ON DATABASE rusa TO rusa_api_login, rusa_worker_login, rusa_sandbox_login;
```

Use `rusa_api_login` only in `RUSA_API_DATABASE_URL`, `rusa_worker_login` only in `RUSA_WORKER_DATABASE_URL`, and `rusa_sandbox_login` only in `RUSA_SANDBOX_EXECUTOR_DATABASE_URL`, all with `sslmode=verify-full`. The API container must not receive worker or sandbox DSNs. The worker must not receive the API or sandbox DSN, tenant API keys, or executor private key. The sandbox executor receives only the read-only lease-verifier DSN and its database CA certificate; it must not receive the worker DSN, GitHub App private key or installation map, model key, or audit HMAC key. Never use the schema owner or a PostgreSQL superuser in application containers.

Runtime permissions:
- API role: tenant-scoped SELECT, INSERT, UPDATE on jobs; tenant-scoped rate-limit maintenance.
- Worker role: cross-tenant SELECT/UPDATE on the durable queue; tenant-scoped memory; append to the immutable audit ledger and task policy event table.
- Sandbox role: EXECUTE only on `rusa_verify_sandbox_lease`; no direct access to job rows, tenant data, or sequences. Its NOLOGIN BYPASSRLS guard can SELECT only the queue table and is not granted to application logins.
- No runtime role can perform DDL, grant privileges, or mutate existing audit records.
- The API role cannot read memory or audit tables.

The database policies use transaction-local `rusa.tenant_id` context. The worker may claim jobs across tenants, but its memory operations remain tenant-scoped. Verify these boundaries with `tests/test_postgres_backends.py`; application-level WHERE clauses alone are not sufficient.

## 3. GitHub App permissions and installation scoping

Create a dedicated GitHub App and install it only on the repositories that AICode is allowed to modify. Grant only:
- Metadata: Read-only
- Contents: Read and write
- Pull requests: Read and write
- Checks: Read-only

Do not grant Actions administration, repository administration, secrets, environments, workflow write, organization administration, or unrelated permissions. Branch protection must prevent direct writes to the default branch and require human review before merge.

Configure `RUSA_GITHUB_APP_ID`, mount the App private key as a read-only file at `RUSA_GITHUB_APP_PRIVATE_KEY_FILE`, and map each tenant to its own installation ID in `RUSA_TENANT_GITHUB_INSTALLATIONS_JSON`. The runtime rejects a missing tenant installation and rejects repositories outside that tenant's `RUSA_TENANT_REPOSITORIES` allowlist.

For each repository operation, the worker requests a short-lived GitHub App installation token restricted to that one repository and the minimum required permissions. The token is used for mirror fetch, feature-branch push, draft PR creation, and check-run verification; it is not written to Git remotes or logs. Production must not set `RUSA_GITHUB_TOKEN`; that compatibility token is development-only.

Mount the App private key into the worker only, owned by UID 10001 and mode 0400. Never mount it into the API, sandbox executor, or validation containers. Rotate the App private key through the secret manager and restart workers to reload it.

Important residual boundary: the current worker process holds the App private key and the installation-ID map for all configured tenants because it consumes the cross-tenant queue. Repository-scoped, short-lived installation tokens reduce credential lifetime and scope, but do not isolate tenants against a fully compromised worker process. Stronger compromise isolation still requires per-tenant worker identities or a separate credential broker that issues tenant-bound tokens without exposing the App private key to workers.

## 4. Model endpoint requirements

Configure an OpenAI-compatible endpoint:
- `GET /models` must succeed for readiness.
- `POST /chat/completions` must accept standard chat requests.
- The coding endpoint must support JSON-object response format and return the AICode action envelope.

The coding endpoint receives repository files and task context. Confirm data residency, retention, access controls, and processor terms before sending proprietary code. Prefer a self-hosted endpoint when required by policy.

## 5. Build and publish immutable images

Build all four images from the reviewed commit:

```bash
docker build -t ghcr.io/ORG/rusa-ai:RELEASE .
docker build -f Dockerfile.worker -t ghcr.io/ORG/rusa-aicode-worker:RELEASE .
docker build -f Dockerfile.agent-sandbox -t ghcr.io/ORG/rusa-aicode-sandbox:RELEASE .
docker build -f Dockerfile.sandbox-executor -t ghcr.io/ORG/rusa-aicode-sandbox-executor:RELEASE .
docker push ghcr.io/ORG/rusa-ai:RELEASE
docker push ghcr.io/ORG/rusa-aicode-worker:RELEASE
docker push ghcr.io/ORG/rusa-aicode-sandbox:RELEASE
docker push ghcr.io/ORG/rusa-aicode-sandbox-executor:RELEASE
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
sudo install -d -o 10001 -g "$RUSA_DOCKER_GID" -m 2770 /var/lib/rusa/sandbox-snapshots
```

Create one bare mirror per authorized repository:

```bash
sudo -u rusa-worker git clone --bare \
  https://github.com/StrategyG/RusaAi.git \
  /var/lib/rusa/mirrors/StrategyG/RusaAi.git
```

The mirror must retain the exact credential-free HTTPS origin. AICode refreshes only the requested base branch before creating a task worktree. Fetch/push credentials are supplied through a short-lived askpass helper and are never embedded in the remote URL.

Mount the workspace root at the same absolute path in both the worker and sandbox-executor containers. The worker receives read/write access; the executor receives read-only access. Mount `/var/lib/rusa/sandbox-snapshots` read/write into the executor at the identical absolute path visible to the rootless Docker daemon. The worker must not receive the Docker socket, Docker group membership, or `DOCKER_HOST`. The executor uses a short-lived, read-only snapshot in this separate directory for each fixed validation profile.

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
```

Worker settings:

```dotenv
RUSA_WORKER_DATABASE_URL=postgresql://rusa_worker_login:REDACTED@DB_HOST:5432/rusa?sslmode=verify-full
RUSA_AUDIT_HMAC_KEY=AT_LEAST_32_RANDOM_UTF8_BYTES
RUSA_WORKSPACE_ROOT=/var/lib/rusa/workspaces
RUSA_GIT_MIRROR_ROOT=/var/lib/rusa/mirrors
RUSA_GITHUB_APP_ID=123456
RUSA_GITHUB_APP_PRIVATE_KEY_FILE=/run/secrets/github-app.pem
RUSA_TENANT_GITHUB_INSTALLATIONS_JSON='{"tenant-acme":987654321}'
RUSA_SANDBOX_IMAGE=ghcr.io/ORG/rusa-aicode-sandbox@sha256:REGISTRY_DIGEST
RUSA_SANDBOX_EXECUTOR_URL=https://sandbox-executor:9101
RUSA_SANDBOX_EXECUTOR_TOKEN=AT_LEAST_32_RANDOM_UTF8_BYTES
RUSA_SANDBOX_EXECUTOR_CA_FILE=/run/secrets/sandbox-executor-ca.crt
RUSA_TASK_TIMEOUT_SECONDS=3600
RUSA_CI_TIMEOUT_SECONDS=900
RUSA_CI_POLL_SECONDS=15
RUSA_METRICS_PORT=9100
RUSA_METRICS_BIND=0.0.0.0
```

The worker calls the executor over HTTPS and verifies its certificate against `RUSA_SANDBOX_EXECUTOR_CA_FILE`. It has no Docker socket, Docker group membership, or `DOCKER_HOST`.

Sandbox executor settings (keep these out of the worker/API environment files):

```dotenv
RUSA_SANDBOX_EXECUTOR_DATABASE_URL=postgresql://rusa_sandbox_login:REDACTED@DB_HOST:5432/rusa?sslmode=verify-full
RUSA_SANDBOX_EXECUTOR_TOKEN=THE_SAME_RANDOM_WORKER_EXECUTOR_SECRET
RUSA_SANDBOX_EXECUTOR_TLS_CERT_FILE=/run/secrets/sandbox-executor.crt
RUSA_SANDBOX_EXECUTOR_TLS_KEY_FILE=/run/secrets/sandbox-executor.key
RUSA_SANDBOX_EXECUTOR_CA_FILE=/run/secrets/sandbox-executor-ca.crt
RUSA_SANDBOX_WORKSPACE_ROOT=/var/lib/rusa/workspaces
RUSA_SANDBOX_SNAPSHOT_ROOT=/var/lib/rusa/sandbox-snapshots
RUSA_SANDBOX_IMAGE=ghcr.io/ORG/rusa-aicode-sandbox@sha256:REGISTRY_DIGEST
RUSA_SANDBOX_MAX_CONCURRENCY=2
RUSA_DOCKER_GID=1001
RUSA_DOCKER_SOCKET=/run/user/1001/docker.sock
DOCKER_HOST=unix:///run/docker.sock
```

Only the sandbox executor receives the read-only lease-verifier DSN, rootless Docker socket, TLS private key, and snapshot volume. The snapshot root must be owned by UID 10001, group-owned by the rootless daemon GID, and created with mode 2770 (setgid). The executor creates each temporary parent directory with mode 0710 so the daemon group can traverse it; the snapshot itself is read-only and contains no Git metadata or configured secret paths. The executor derives the workspace path from tenant ID, task UUID, and lease UUID; callers cannot supply a path or command. Each request must pass the database function that verifies the live lease owner, tenant, token, running status, and expiry. The Docker sandbox runs only predefined profiles with network disabled, a read-only snapshot, a pinned image, resource limits, and no capabilities.

Keep `RUSA_AICODE_ENABLED=false` during installation, migration, smoke testing, security review, and restore drills. The API rejects task intake/retry and workers do not claim jobs while disabled. Set it to `true` only after the final release gate is signed off. To pause AICode, set it to `false` and restart the API and worker services: the API then rejects new task submissions/retries and workers stop claiming new jobs. Already-running work is not forcibly terminated; cancel it through the API or let it reach a safe checkpoint. Existing task status, cancellation, health, and metrics endpoints remain available.

Generate raw API keys with a cryptographically secure generator, give each raw key only to its client through a secret manager, and configure only its lowercase SHA-256 digest in `RUSA_API_KEYS_JSON`. Each registry claim must include a unique subject, tenant_id, and one or more supported roles (`platform_admin`, `tenant_admin`, `developer`, `read_only`). Production rejects legacy raw `RUSA_API_KEYS` and `RUSA_API_KEY`. Keep the audit HMAC key at least 32 UTF-8 bytes. Never put credentials in task context, repository files, Docker build arguments, or Git remotes.

## 8. Compose deployment

The included Compose file is a development/single-host example. It uses a local PostgreSQL service and is not a complete HA production topology. For production:
1. Replace the Compose PostgreSQL service with managed PostgreSQL.
2. Use separate `rusa_api_login`, `rusa_worker_login`, and `rusa_sandbox_login` database users; never reuse the migration owner or a shared runtime login.
3. Mount the GitHub App private key read-only into the worker only; do not mount it into API or sandbox-executor.
4. Mount persistent mirror/workspace volumes at the configured paths.
5. Mount the workspace root read/write into the worker and read-only into the sandbox executor.
6. Mount the dedicated rootless Docker socket only into the sandbox executor; never into the worker.
7. Mount the separate snapshot root into the executor at the identical host path visible to the rootless daemon.
8. Mount the executor TLS certificate/key into the executor and the CA certificate into the worker.
9. Restrict the API to a TLS-terminating reverse proxy and keep worker/executor ports on internal networks only.
10. Restrict sandbox-executor egress at the host firewall to the managed database endpoint; do not allow general internet egress.

For local development only, use the bundled PostgreSQL stack:

```bash
docker compose up -d
docker compose --profile worker up -d --build
```

For production, set the API and worker image references to immutable registry digests in the deployment environment, keep `RUSA_AICODE_ENABLED=false` for the initial rollout, then validate and deploy the external-database stack:

```bash
export RUSA_API_IMAGE=ghcr.io/ORG/rusa-ai@sha256:API_IMAGE_DIGEST
export RUSA_WORKER_IMAGE=ghcr.io/ORG/rusa-aicode-worker@sha256:WORKER_IMAGE_DIGEST
export RUSA_SANDBOX_EXECUTOR_IMAGE=ghcr.io/ORG/rusa-aicode-sandbox-executor@sha256:EXECUTOR_IMAGE_DIGEST
docker compose -f docker-compose.production.yml config
docker compose -f docker-compose.production.yml pull
docker compose -f docker-compose.production.yml up -d
docker compose -f docker-compose.production.yml ps
docker compose -f docker-compose.production.yml logs --tail=200 api worker
```

The production Compose file has no local PostgreSQL service. It requires separate managed TLS DSNs for API, worker, and sandbox lease verification. It mounts the rootless Docker socket only into the sandbox executor; the worker receives only its HTTPS endpoint, bearer capability, and CA certificate. The executor gets read-only workspace access and a separate writable snapshot mount. Compose fails configuration if any immutable image, secret, certificate, socket, or storage setting is missing.

API, worker, and sandbox-executor containers run non-root, read-only root filesystems, dropped capabilities, no-new-privileges, and resource limits. The executor is the only service with access to the rootless Docker socket. The worker temporary filesystem permits execution because Git's short-lived askpass helper must be executable; keep that container isolated.

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

The API returns a task UUID and generated feature branch. Poll `GET /v1/aicode/tasks/{task_id}` with the same tenant key. The response includes the durable `result` object after completion (Pull Request URL, commit SHA, and actual lease-specific branch), or after human review is required. The initial submission returns `work_branch_prefix`; the actual branch is assigned per worker lease to prevent stale workers from colliding. Tasks in `awaiting_approval` or terminal `failed` state can be explicitly requeued by the owning tenant with `POST /v1/aicode/tasks/{task_id}/retry` after reviewing the failure. The retry endpoint does not approve PR merges or deployments.

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
- Sandbox adversarial and resource exhaustion tests pass, including expired-lease rejection and confirmation that the worker has no Docker socket.
- GitHub App permissions are least-privilege, tenant installation mapping is correct, and branch protection is active.
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
