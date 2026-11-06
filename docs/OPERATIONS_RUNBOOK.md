# RUSA-AI Operations Runbook

This runbook covers the current AICode runtime foundation. It is not a production certification; complete the release gates in [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md) before exposing the service to production traffic.

## 1. Deployment prerequisites

- Docker Engine and Docker Compose v2.
- A PostgreSQL 16-compatible deployment for the durable queue, shared memory, and audit history.
- A local model artifact at `RUSA_MODEL_DIR` for inference. Without it, the API remains live but `/health/ready` returns HTTP 503 and inference returns HTTP 503.
- An approved OpenAI-compatible provider endpoint. Remote provider hostnames must be explicitly listed in `RUSA_PROVIDER_ALLOWED_HOSTS`.
- A GitHub App installation token or fine-grained token with the minimum required repository contents, pull-request, and check-read permissions.
- A repository allowlist. Multi-tenant deployments require a separate repository list for every authenticated tenant.

## 2. Secrets and tenant configuration

Copy `.env.example` to `.env`. Never commit `.env` or paste production credentials into task prompts.

Generate random secrets:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

For a single-tenant deployment, set:

- `RUSA_API_KEY`: random secret with at least 32 characters.
- `RUSA_TENANT_ID`: tenant identifier.
- `RUSA_GITHUB_ALLOWED_REPOSITORIES`: comma-separated owner/repo allowlist.

For a multi-tenant deployment:

1. Generate one API key per tenant.
2. Store only each API key's SHA-256 hex digest in `RUSA_API_KEY_TENANTS_JSON`, mapped to its tenant ID.
3. Set `RUSA_GITHUB_TENANT_REPOSITORIES_JSON` with an explicit owner/repo list for every tenant.
4. Do not use a shared global repository allowlist as a substitute for per-tenant lists.

Example values (set each environment variable separately; replace all example values):

`RUSA_API_KEY_TENANTS_JSON`:

```json
{
  "<sha256-api-key-a>": "tenant-a",
  "<sha256-api-key-b>": "tenant-b"
}
```

`RUSA_GITHUB_TENANT_REPOSITORIES_JSON`:

```json
{
  "tenant-a": ["org-a/repo-a"],
  "tenant-b": ["org-b/repo-b"]
}
```

Configure `RUSA_PROVIDER_BASE_URL`, `RUSA_PROVIDER_ALLOWED_HOSTS`, `RUSA_PROVIDER_MODEL`, and `RUSA_PROVIDER_API_KEY` as appropriate. Use a secret manager in production; the `.env` file is a local deployment example.

## 3. Start and verify services

Set `RUSA_AGENT_ENABLED=true` and configure the database, provider, GitHub token, repository allowlist, and model directory.

Start the API and database:

```bash
docker compose up --build -d postgres rusa-api
```

Start the AICode worker:

```bash
docker compose --profile agent up --build -d rusa-agent-worker
```

Verify process liveness:

```bash
curl --fail http://127.0.0.1:8000/health/live
```

Verify model readiness:

```bash
curl -i http://127.0.0.1:8000/health/ready
```

A 503 readiness response is expected until a valid local model is mounted and loaded. Liveness alone does not mean inference is ready.

For multiple worker replicas:

```bash
docker compose --profile agent up -d --scale rusa-agent-worker=3
```

Workers claim jobs using PostgreSQL row locks and leases. SIGTERM/SIGINT cancels the active orchestration, records cancellation, and releases the current lease for safe requeue without consuming a retry attempt. Give each replica a unique worker hostname/ID and monitor provider/GitHub rate limits.

## 4. Staging image publication and deployment

The repository provides a manual GitHub Actions workflow at `.github/workflows/deploy-staging.yml`. It never deploys automatically on push or merge.

1. In GitHub, create the `staging` environment and configure required reviewers/approval rules before enabling deployment.
2. Add environment variables `STAGING_APP_DIR` (absolute path on the host), `GHCR_PULL_USER`, and optionally `STAGING_URL`.
3. Add environment secrets `STAGING_HOST`, `STAGING_USER`, `STAGING_SSH_PRIVATE_KEY`, `STAGING_KNOWN_HOSTS`, and `GHCR_PULL_TOKEN`. The registry token should be read-only (`read:packages`); the deploy SSH account should only manage this staging application's directory and Compose project.
4. Prepare the host directory and a host-managed `.env` file. Never copy secrets from GitHub Actions into the repository. Configure PostgreSQL, a valid model artifact directory, auth/bootstrap secrets, provider allowlists, and a least-privilege GitHub token as required.
5. Add the exact SSH host public key to `STAGING_KNOWN_HOSTS` out-of-band. Do not replace pinned host verification with `ssh-keyscan` at deployment time.
6. Open **Actions → Publish and deploy staging**, select the intended revision, and choose `build-only` first. This publishes immutable `staging-<commit-sha>` images to GHCR without changing the host.
7. After reviewing the image revision and staging host configuration, run the workflow again with `deploy`. GitHub environment protection must approve the deployment before SSH is used.
8. The deployment uploads only the Compose manifests, validates the host-managed configuration, pulls the immutable image tag, and waits for Compose health checks. The worker profile is not started automatically; enable it separately only after provider/GitHub credentials and the approved repository allowlist are configured.

The workflow's `staging` tag is a convenience pointer. Deployment uses the immutable commit-specific tag for reproducibility and rollback. The workflow does not deploy to production.

## 5. Task operations

All task operations require the tenant's API key in the `X-API-Key` header.

- Submit: `POST /v1/agent/tasks` with an A2A `task.create` envelope and an `Idempotency-Key` header. The runtime appends the first eight task-ID characters to the requested work-branch prefix to isolate concurrent tasks.
- Inspect: `GET /v1/agent/tasks/{task_id}`.
- Submit, cancel, and replay requests are appended to the tenant-scoped PostgreSQL audit chain before queue mutation; task execution lifecycle events are recorded by the orchestrator.
- Cancel: `POST /v1/agent/tasks/{task_id}/cancel`. The current lease is fenced immediately; the worker observes lease loss within at most 30 seconds and cancels its orchestration.
- Queue statistics: `GET /v1/agent/queue/stats`.
- Dead letters: `GET /v1/agent/dead-letters`.
- Replay: `POST /v1/agent/tasks/{task_id}/retry`. Only dead-letter tasks can be replayed; replay resets the attempt counter. Review the task failure before replaying.

The orchestrator can classify work as `succeeded`, `blocked`, `failed`, or `cancelled`. The durable queue exposes `queued`, `running`, `succeeded`, `failed`, `dead_letter`, and `cancelled`; a blocked orchestration is persisted as a non-retryable queue failure. A draft PR and successful CI evidence do not authorize merging or deployment.

## 6. Database backup and restore

A basic logical backup example:

```bash
docker compose exec -T postgres pg_dump -U rusa -d rusa > rusa-backup.sql
```

Do not treat a successful dump as a restore test. Before production, automate encrypted backups, retention, point-in-time recovery where required, and a restore drill in an isolated environment. The current repository does not provide PostgreSQL HA or disaster-recovery certification.

## 7. Security and incident controls

- Keep the API behind a TLS-terminating gateway. Compose binds the API to loopback by default.
- Configure distributed rate limits, request timeouts, ingress size limits, and network egress restrictions at the gateway/platform layer.
- Keep GitHub credentials least-privileged and scoped to the allowlisted repositories.
- Repository files under .ssh, .aws, .kube, .docker, .config/gcloud, .config/gh, and secret-bearing Terraform state/variable paths are blocked from model reads.
- Do not grant workers merge, release, deployment, workflow, or secret-management permissions.
- Rotate API, provider, GitHub, and database credentials using the deployment secret manager.
- If a worker behaves unexpectedly, stop the worker service, revoke its GitHub installation token, and inspect tenant-scoped task status, dead letters, and the PostgreSQL audit chain.
- Do not delete or truncate the audit table. Update/delete/truncate operations are blocked by database triggers; preserve database backups separately.

## 8. Known release gates

Before production rollout, complete and record evidence for PostgreSQL HA and restore, queue-age alerting, replay approval workflow, worker health monitoring, centralized logs/metrics/traces, prompt-injection and adversarial testing, real provider/GitHub integration, model artifact provenance, model quality benchmarks, hardware capacity, load/soak testing, rollback, and owner sign-off.
