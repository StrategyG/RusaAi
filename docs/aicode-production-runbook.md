# RUSA-AI AICode — Production Deployment Runbook

This runbook deploys AICode as a production candidate. Complete every preflight and acceptance gate before enabling production tenants.

## 1. Required infrastructure

- PostgreSQL 15+ with automated encrypted backups, point-in-time recovery, TLS, and a dedicated runtime role.
- Dedicated Linux worker host running a rootless Docker daemon.
- Persistent Git mirror and workspace volumes mounted at the same absolute paths inside the worker container and visible to the rootless Docker daemon.
- Registry containing the API image and AICode sandbox image.
- Self-hosted or approved OpenAI-compatible model endpoint.
- Fine-grained GitHub token restricted to the repository allowlist.
- TLS-terminating reverse proxy/ingress, Prometheus-compatible scraper, and alerting.

Do not expose PostgreSQL, worker metrics, or the Docker socket publicly.

## 2. Database migration and runtime role

Apply the migration as a schema owner/admin, not as the application runtime user:

```bash
psql "$RUSA_MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_aicode_core.sql
```

The migration creates `aicode_jobs`, `aicode_memory`, `aicode_audit_events`, `aicode_rate_limits`, audit immutability triggers, and the restricted `rusa_runtime` role.

Provision a login credential for `rusa_runtime` through the DBA workflow or secret manager and grant CONNECT on the application database. Never use the schema owner or a PostgreSQL superuser in API/worker containers.

Runtime permissions:
- Queue: SELECT, INSERT, UPDATE, sequence usage.
- Memory: SELECT, INSERT, UPDATE.
- Rate limits: SELECT, INSERT, UPDATE, DELETE.
- Audit: SELECT, INSERT only.
- No schema ownership, DDL, UPDATE, DELETE, or TRUNCATE on the audit table.

Verify the runtime role can connect and audit UPDATE, DELETE, and TRUNCATE operations are rejected.

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

Build both images from the reviewed commit:

```bash
docker build -t ghcr.io/ORG/rusa-ai:RELEASE .
docker build -f Dockerfile.agent-sandbox -t ghcr.io/ORG/rusa-aicode-sandbox:RELEASE .
docker push ghcr.io/ORG/rusa-ai:RELEASE
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
RUSA_DATABASE_URL=postgresql://rusa_runtime:REDACTED@DB_HOST:5432/rusa
RUSA_API_KEYS={"LONG_RANDOM_API_KEY_AT_LEAST_32_BYTES":"tenant-acme"}
RUSA_TENANT_REPOSITORIES={"tenant-acme":["StrategyG/RusaAi"]}
RUSA_MODEL_BASE_URL=https://MODEL_HOST/v1
RUSA_MODEL_NAME=rusa-ai
RUSA_RATE_LIMIT_CHAT_PER_MINUTE=60
RUSA_RATE_LIMIT_TASKS_PER_MINUTE=10
RUSA_RATE_LIMIT_DEFAULT_PER_MINUTE=120
```

Worker settings:

```dotenv
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

API keys and the audit HMAC key must each contain at least 32 UTF-8 bytes. Use a secret manager. Never put credentials in task context, repository files, Docker build arguments, or Git remotes.

## 8. Compose deployment

The included Compose file is a development/single-host example. It uses a local PostgreSQL service and is not a complete HA production topology. For production:
1. Replace the Compose PostgreSQL service with managed PostgreSQL.
2. Use `rusa_runtime`, not the migration owner.
3. Mount persistent mirror/workspace volumes at the configured paths.
4. Mount only the dedicated rootless Docker socket into the worker.
5. Confirm the rootless daemon sees the same workspace host paths.
6. Restrict the API to a TLS-terminating reverse proxy.
7. Keep worker metrics on an internal network only.

After migration and image verification:

```bash
docker compose up -d api
docker compose --profile worker up -d worker
docker compose ps
docker compose logs --tail=200 api worker
```

API and worker containers run non-root, read-only root filesystems, dropped capabilities, no-new-privileges, and resource limits. The worker temporary filesystem permits execution because Git's short-lived askpass helper must be executable; keep that container isolated.

## 9. Smoke tests

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/health/ready
curl -fsS -H "X-API-Key: $RUSA_API_KEY" http://127.0.0.1:8000/metrics
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

The API returns a task UUID and generated feature branch. Poll `GET /v1/aicode/tasks/{task_id}` with the same tenant key.

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
