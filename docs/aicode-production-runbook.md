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
```

If the optional RAG knowledge plane is enabled, apply `migrations/007_rag_knowledge_pgvector.sql` separately to a pgvector-enabled knowledge database using its database owner. Do not apply migration 007 to a database that is not prepared for pgvector.

Core migrations 001–006 create the durable task queue, lease fencing, task results, tenant RLS, append-only policy audit, and tenant-scoped client idempotency. Apply them in numeric order. Migration 007 is optional and installs the pgvector knowledge plane; it must be applied only where pgvector is available and the restricted knowledge role is configured. The API and worker fail closed if required core columns, idempotency index, RLS flags, policies, audit grants, or append-only triggers are missing.

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

Do not publish production images from an unreviewed branch or a mutable tag. After the exact commit is merged to `main`, create an authorized semantic-version tag in the form `vMAJOR.MINOR.PATCH`. The `.github/workflows/release.yml` workflow:

1. Rejects non-semantic tags and tags whose commit is not reachable from `main`.
2. Re-runs the complete reusable CI workflow on the exact tagged commit, including the stable **Release Gate**.
3. Builds and publishes the API, worker, and isolated sandbox images to GHCR.
4. Scans the exact published digests for fixable CRITICAL/HIGH vulnerabilities.
5. Creates keyless Cosign signatures using GitHub OIDC, build-provenance attestations, and OCI SBOM attestations.
6. Publishes a GitHub Release with `release-manifest.txt`, which records the three immutable image digests.

The production image names are:

```text
ghcr.io/strategyg/rusa-ai-api@sha256:API_DIGEST
ghcr.io/strategyg/rusa-ai-worker@sha256:WORKER_DIGEST
ghcr.io/strategyg/rusa-ai-sandbox@sha256:SANDBOX_DIGEST
```

The worker uses the sandbox image through the dedicated rootless Docker daemon. Production must use the sandbox digest from the same reviewed release; never use a mutable tag such as `:latest`. The standard API image does not bundle model weights or heavyweight local-model dependencies. A local Transformers/vLLM deployment requires a separately approved model artifact, a compatible image build, and target-hardware validation.

`release-manifest.txt` is the source of truth for the digest values supplied to the manual production deployment workflow. Do not copy tags into the production environment file where a digest is required.

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

For production, pre-provision `.env` with the API, worker, and sandbox digest references from a reviewed release, separate managed TLS DSNs, rootless Docker socket, workspace/mirror paths, tenant API-key digests, and other required secrets. Before every deployment or rollback, set `RUSA_AICODE_ENABLED=false`, wait until all active AICode tasks have reached a safe terminal/checkpoint state, and confirm no worker is executing a task. The deployment script fails closed if task intake is enabled; it does not drain active work itself. Re-enable `RUSA_AICODE_ENABLED=true` only after post-deploy smoke tests and operator approval. The manual `.github/workflows/deploy-production.yml` workflow requires the `production` environment approval, verifies all three Cosign signatures against the exact release workflow identity, and then transfers the Compose manifest and deployment scripts over pinned SSH.

The host deployment script can also be run manually after verifying the signatures and release manifest:

```bash
./scripts/deploy/compose-deploy.sh ghcr.io/strategyg/rusa-ai-api@sha256:API_DIGEST ghcr.io/strategyg/rusa-ai-worker@sha256:WORKER_DIGEST ghcr.io/strategyg/rusa-ai-sandbox@sha256:SANDBOX_DIGEST
```

It requires the previous API, worker, and sandbox digests to already exist in `.env` as rollback targets. It pulls API/worker images from the host Docker daemon, pulls the sandbox image through `RUSA_DOCKER_SOCKET`, updates services, waits for both health checks, and automatically restores the prior digest set if the candidate is unhealthy. Manual rollback uses:

```bash
./scripts/deploy/compose-rollback.sh
```

The deployment workflow requires these GitHub `production` environment variables: `PRODUCTION_SSH_HOST`, `PRODUCTION_SSH_USER`, `PRODUCTION_DEPLOY_PATH`, and `GHCR_PULL_USERNAME`. It requires these environment secrets: `PRODUCTION_SSH_PRIVATE_KEY`, `PRODUCTION_SSH_KNOWN_HOSTS`, and a read-only `GHCR_PULL_TOKEN`. Configure required reviewers and tag restrictions on the environment before using the workflow.

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

The API returns a task UUID and generated feature branch. Poll `GET /v1/aicode/tasks/{task_id}` with the same tenant key. The response includes the durable `result` object after completion (Pull Request URL, commit SHA, and actual lease-specific branch), or after human review is required. The initial submission returns `work_branch_prefix`; the actual branch is assigned per worker lease to prevent stale workers from colliding. Tasks in `awaiting_approval` or terminal `failed` state can be explicitly requeued by the owning tenant with `POST /v1/aicode/tasks/{task_id}/retry` after reviewing the failure. The retry endpoint does not approve PR merges or deployments.

A successful task creates a draft pull request and requires the stable **Release Gate** check to pass on the exact head commit. If the check is missing, pending, failed, or cannot be verified, the task remains `awaiting_approval`; it is never silently marked successful.

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
- The **Release Gate** check passes on the exact PR head and exact release tag, including PostgreSQL integration tests, API/worker/sandbox builds, Trivy scanning, and RAG/MCP integration tests.
- Migration and runtime role permissions are verified.
- Tenant isolation tests show zero cross-tenant access.
- Sandbox adversarial and resource exhaustion tests pass.
- GitHub token is least-privilege; an active `main` ruleset requires PR review and the **Release Gate** check with no routine bypass.
- Model endpoint is reachable and passes task-quality evaluation.
- Backup/restore, worker-restart, automated rollback, and staging disaster-recovery drills pass against owner-approved RPO/RTO targets.
- Metrics are scraped and alerts are tested.
- Published API, worker, and sandbox digests have verified Cosign signatures, provenance, and SBOMs; staging deployment and rollback are evidenced.
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

- Database RLS and separate API/worker database identities are implemented through migrations 004–005; migration 006 adds tenant-scoped client idempotency. Before certification, execute adversarial cross-tenant tests against the actual production login roles and verify grants, policies, idempotency constraints, audit immutability, and off-host retention on the target PostgreSQL instance.
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
