# RUSA-AI — VPS full-ecosystem deployment

## Scope and release boundary

This guide deploys the current repository services on a single Linux VPS:

- FastAPI inference/API gateway.
- PostgreSQL-backed AICode queue and autonomous worker.
- Isolated Docker sandbox driven through a dedicated rootless Docker daemon.
- Tenant-scoped PostgreSQL/pgvector knowledge store.
- Read-only, bearer-authenticated Knowledge MCP service.

The current repository does **not** include trained RUSA-AI model weights, a browser workspace/user-administration frontend, or a complete identity/JWT product. Chat inference therefore requires an approved OpenAI-compatible model endpoint until a separately approved local checkpoint is supplied. MCP is a separate endpoint; it is not automatically mounted into the core API.

This is a production-candidate deployment guide, not a certification. Keep `RUSA_AICODE_ENABLED=false` until every release gate in this guide passes.

**Scope clarification:** “Full stack” here means the currently implemented API, AICode worker/sandbox, PostgreSQL queue, pgvector knowledge service, and MCP service. It does **not** mean the proposed eight-agent DevOps lifecycle is already implemented. The Plan, Code, Build, Test, Release, Deploy, Operate, and Monitor specialists, DevOps Orchestrator, Security Guard, and their durable inter-agent event workflow remain a separate phased implementation.

## 1. Proposed topology

| Service | Placement | Public exposure |
|---|---|---|
| TLS reverse proxy | VPS host or dedicated ingress | 80/443 only |
| API | Docker Compose | Loopback 127.0.0.1:8000 behind TLS |
| Worker | Docker Compose | Internal network only |
| MCP | Docker Compose overlay | Loopback 127.0.0.1:8002 behind TLS |
| Sandbox | Ephemeral rootless Docker containers | No public ports |
| PostgreSQL 16 + pgvector | Managed/external database preferred | Private network + TLS only |
| Chat model endpoint | Approved external or separately hosted provider | Private/TLS egress |
| Embedding endpoint | Approved OpenAI-compatible provider | Private/TLS egress |
| Metrics | External Prometheus-compatible scraper | Private only |

Do not expose PostgreSQL, worker metrics (9100), Docker sockets, or sandbox ports to the public internet.

**Egress is an open production gate.** The current Compose bridge named `rusa-egress` provides outbound connectivity; it is not an allowlist or egress proxy. Before enabling autonomous work for untrusted tenants, enforce deny-by-default outbound policy for the worker and sandbox, permitting only explicitly approved model, GitHub, embedding, database, DNS, and telemetry destinations through controlled gateways/proxies. Validate the policy with negative tests. Do not treat a Docker bridge network alone as egress isolation.

## 2. VPS and external prerequisites

For an initial single-node deployment using an external model provider, use a Linux LTS VPS with a starting budget of at least 8 vCPU, 16 GB RAM, and 150–200 GB SSD/NVMe. This is a sizing proposal, not a measured capacity guarantee; sandbox concurrency and repository size may require more. Local LLM inference requires separate GPU/VRAM sizing based on the exact approved checkpoint and is not included in this baseline.

Prepare:

1. A static public IP and DNS names for the API and MCP endpoints.
2. SSH access restricted to an administrator IP/key; disable password SSH after confirming key access.
3. Docker Engine with Compose v2.
4. A dedicated unprivileged `rusa-worker` account and rootless Docker daemon.
5. Persistent worker workspace and Git mirror directories.
6. PostgreSQL 16 with pgvector, TLS, encrypted backups, and point-in-time recovery.
7. A container registry (GHCR is suitable) for immutable API, worker, sandbox, and MCP images.
8. An approved OpenAI-compatible chat endpoint and embeddings endpoint.
9. A fine-grained GitHub token restricted to the allowed repository list.
10. A TLS reverse proxy, monitoring, alerting, and a tested backup/restore process.

## 3. Network and host preparation

Allow inbound TCP 80/443 and SSH only from trusted administration addresses. Bind application ports to loopback. Do not publish PostgreSQL 5432, worker metrics 9100, or the rootless Docker socket.

Create persistent paths using the actual worker UID/GID and ensure the rootless Docker daemon can see the same absolute workspace path that is mounted inside the worker container:

```bash
sudo install -d -o 10001 -g 10001 -m 0750 /var/lib/rusa/workspaces
sudo install -d -o 10001 -g 10001 -m 0750 /var/lib/rusa/mirrors
```

Create a bare Git mirror for each explicitly authorized repository. The mirror remote must be credential-free HTTPS; Git credentials are supplied at runtime by the worker's short-lived askpass helper.

Install and enable rootless Docker for the dedicated worker account. Record the actual socket path and socket group ID; do not assume they are `/run/user/1001/docker.sock` and `1001`. The Compose worker must mount that rootless socket, not the host's rootful Docker socket.

## 4. PostgreSQL migrations and roles

Use PostgreSQL 16 with pgvector available. Back up the database before applying migrations. Apply migrations in this exact order using a schema-owner/admin connection:

```bash
for migration in \
  migrations/001_aicode_core.sql \
  migrations/002_aicode_lease_fencing.sql \
  migrations/003_aicode_task_results.sql \
  migrations/004_aicode_tenant_rls.sql \
  migrations/005_aicode_task_policy_audit.sql \
  migrations/006_aicode_idempotency.sql \
  migrations/007_rag_knowledge_pgvector.sql \
  migrations/008_rag_read_only_reader.sql
do
  psql "$RUSA_MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f "$migration" || exit 1
done
```

Migration 007 creates the tenant-scoped vector store and the ingestion role. Migration 008 creates a separate read-only role, moves autonomous retrieval away from the write-capable ingestion role, grants SELECT only, and revokes the legacy knowledge-table SELECT grants from the API runtime role. Do not run application containers as the migration owner or a PostgreSQL superuser.

Create separate login identities after the migrations. Generate unique passwords in a secret manager; never paste passwords into source control:

```sql
CREATE ROLE rusa_api_login LOGIN PASSWORD 'managed-secret' IN ROLE rusa_api_runtime;
CREATE ROLE rusa_worker_login LOGIN PASSWORD 'managed-secret' IN ROLE rusa_worker_runtime;
CREATE ROLE rusa_mcp_login LOGIN NOINHERIT PASSWORD 'managed-secret';
GRANT rusa_knowledge_reader TO rusa_mcp_login WITH INHERIT FALSE;
GRANT CONNECT ON DATABASE rusa TO rusa_api_login, rusa_worker_login, rusa_mcp_login;
```

Use `rusa_api_login` only for the API DSN, `rusa_worker_login` only for the worker DSN, and `rusa_mcp_login` only for the MCP DSN. The worker runtime may assume the reader role for retrieval; it must not retain membership in the write-capable ingestion role after migration 008. The API runtime role has no direct access to the knowledge tables. Use the ingestion role only for a separately controlled document-ingestion job.

The database must enforce TLS with certificate verification. Prefer a private database network. Confirm role memberships, RLS flags/policies, SELECT-only MCP grants, and cross-tenant isolation using the repository PostgreSQL integration tests before enabling tenant traffic.

## 5. Build and publish immutable images

The current CI builds and scans images but does not publish them to a registry automatically. Build from the reviewed `main` commit, authenticate to the registry on the VPS or a trusted build host, and push versioned images:

```bash
docker build -t ghcr.io/OWNER/rusa-ai:RELEASE .
docker build --build-arg RUSA_INSTALL_RAG=1 -f Dockerfile.worker \
  -t ghcr.io/OWNER/rusa-aicode-worker:RELEASE .
docker build -f Dockerfile.agent-sandbox \
  -t ghcr.io/OWNER/rusa-aicode-sandbox:RELEASE .
docker build -f Dockerfile.rag-mcp \
  -t ghcr.io/OWNER/rusa-ai-rag-mcp:RELEASE .

docker push ghcr.io/OWNER/rusa-ai:RELEASE
docker push ghcr.io/OWNER/rusa-aicode-worker:RELEASE
docker push ghcr.io/OWNER/rusa-aicode-sandbox:RELEASE
docker push ghcr.io/OWNER/rusa-ai-rag-mcp:RELEASE
```

Resolve and record the registry SHA-256 digest for all four images. Set `RUSA_API_IMAGE`, `RUSA_WORKER_IMAGE`, `RUSA_SANDBOX_IMAGE`, and `RUSA_MCP_IMAGE` to immutable `@sha256:` references. Do not deploy mutable tags. The worker image must include the optional RAG dependencies, and the sandbox image must be available to the same rootless daemon used by the worker.

## 6. Secret-managed environment

Create a VPS-only `.env` file with owner-only permissions (`chmod 600`). Never commit it or send its contents through chat. Set the following variables with real values:

- Immutable image digests: `RUSA_API_IMAGE`, `RUSA_WORKER_IMAGE`, `RUSA_SANDBOX_IMAGE`, `RUSA_MCP_IMAGE`.
- Database DSNs: `RUSA_API_DATABASE_URL`, `RUSA_WORKER_DATABASE_URL`, `RUSA_WORKER_KNOWLEDGE_DATABASE_URL`, `RUSA_MCP_DATABASE_URL`; use the matching least-privilege login and `sslmode=verify-full`.
- API security: `RUSA_API_KEYS_JSON` (SHA-256 raw API-key digest to tenant/subject/roles), `RUSA_METRICS_API_KEY`, `RUSA_AUDIT_HMAC_KEY` (at least 32 random UTF-8 bytes).
- Worker: `RUSA_GITHUB_TOKEN` (fine-grained token), `RUSA_TENANT_REPOSITORIES`, `RUSA_WORKSPACE_ROOT`, `RUSA_GIT_MIRROR_ROOT`, `RUSA_DOCKER_SOCKET`, `RUSA_DOCKER_GID`, and `RUSA_SANDBOX_IMAGE`.
- Chat model: `RUSA_MODEL_BASE_URL`, `RUSA_MODEL_NAME`, and `RUSA_MODEL_API_KEY` for an approved OpenAI-compatible chat endpoint.
- Embeddings: `RUSA_EMBEDDING_BASE_URL`, `RUSA_EMBEDDING_API_KEY`, `RUSA_EMBEDDING_MODEL` (default `text-embedding-3-small`, 1536 dimensions).
- MCP: `RUSA_MCP_TENANT_KEYS` (JSON map of lowercase SHA-256 bearer-token digests to tenant IDs), `RUSA_MCP_ALLOWED_HOSTS`, `RUSA_MCP_BIND_PORT`.
- Retrieval: `RUSA_KNOWLEDGE_DB_ROLE=rusa_knowledge_reader`, `RUSA_KNOWLEDGE_COLLECTION=agent`, `RUSA_RAG_ENABLED=false` initially.
- Safety: `RUSA_AICODE_ENABLED=false` initially.

The MCP token map must contain hashes, never raw bearer tokens. Generate each raw token outside the repository, deliver it only to its intended tenant, and configure only its SHA-256 digest. The API key registry must likewise contain hashes, not raw API keys. Keep chat, embedding, database, GitHub, audit, API, metrics, and MCP credentials separate.

## 7. Validate and start the full stack

From the repository directory on the VPS, with the protected `.env` present:

```bash
docker compose -f docker-compose.production.yml \
  -f docker-compose.production.rag-mcp.yml config --quiet
docker compose -f docker-compose.production.yml \
  -f docker-compose.production.rag-mcp.yml pull
docker compose -f docker-compose.production.yml \
  -f docker-compose.production.rag-mcp.yml up -d
docker compose -f docker-compose.production.yml \
  -f docker-compose.production.rag-mcp.yml ps
docker compose -f docker-compose.production.yml \
  -f docker-compose.production.rag-mcp.yml logs --tail=200 api worker rusa-mcp
```

The MCP listener binds only to VPS loopback port 8002. Configure the TLS reverse proxy to route the chosen MCP hostname to `127.0.0.1:8002`; include that hostname in `RUSA_MCP_ALLOWED_HOSTS`. Route the API hostname to `127.0.0.1:8000`. Do not proxy worker metrics or database ports publicly.

The overlay deliberately injects only MCP's dedicated database login and tenant token hashes into the MCP container. It blanks MCP credentials from API/worker environments and does not give the MCP container the GitHub token or API-key registry.

## 8. Smoke tests and staged activation

Check service liveness/readiness from the VPS:

```bash
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/health/ready
curl --fail http://127.0.0.1:8002/health/live
curl --fail http://127.0.0.1:8002/health/ready
```

Then verify:
1. API `/health/ready` reports verified model/provider readiness and authenticated configuration.
2. MCP `/health/ready` returns 200 only with a valid tenant-key map and healthy knowledge database.
3. A tenant bearer token can call MCP tools, while a missing/invalid token receives 401.
4. MCP database login can SELECT only after assuming `rusa_knowledge_reader`, and cannot INSERT/UPDATE/DELETE.
5. Tenant A cannot retrieve tenant B documents.
6. Worker metrics are scraped only over the private network.
7. Rootless sandbox execution, cancellation, timeouts, and resource limits pass a staging test.
8. PostgreSQL backup and restore are tested; audit HMAC key material is preserved.

Keep `RUSA_AICODE_ENABLED=false` and `RUSA_RAG_ENABLED=false` during initial boot, migration verification, and restore/security drills. After the database, embeddings, and MCP checks pass, set `RUSA_RAG_ENABLED=true` and restart the worker to enable read-only retrieval tools. Keep AICode task intake disabled until the model endpoint passes the task-quality check, GitHub permissions are least-privilege, branch protection is verified, CI is green on the deployed SHA, monitoring/alerts are active, and the restore drill succeeds. Only then may the operator deliberately set `RUSA_AICODE_ENABLED=true`.

## 9. Model boundary

No trained RUSA-AI checkpoint is included in the repository. With an external model endpoint, RUSA-AI provides its own API, queue, agents, safety boundaries, RAG, and MCP services, but the language-model inference is supplied by that configured provider. Do not describe this as a standalone trained RUSA model.

Local Transformers/vLLM serving requires an approved checkpoint, tokenizer/chat template, license, target hardware, manifest, externally pinned `RUSA_MODEL_MANIFEST_SHA256`, and a separately built image with the local model dependencies. Validate actual generation, streaming cancellation, capacity, and model quality on the target GPU before switching away from the external endpoint.

## 10. Rollback and operations

- Disable AICode intake first; stop or safely drain workers.
- Restore the previous reviewed immutable image digests.
- Do not roll back or delete audit ledger data.
- Apply only forward migrations; database schema rollback is not part of the deployment procedure.
- Verify API/MCP readiness, queue state, audit integrity, tenant isolation, and GitHub branch/PR state before resuming.
- Maintain encrypted database backups/PITR, off-host copy, disk alerts, queue-age alerts, worker lease alerts, API error/latency alerts, MCP readiness alerts, and incident/restore runbooks.

## 11. Remaining release gates

- Implement and verify the eight-stage Agentic DevOps workflow as a separate phased delivery; do not advertise the target lifecycle as deployed until each agent contract, orchestrator state transition, QA gate, audit event, and approval boundary is evidenced.
- Apply deny-by-default egress controls to worker and sandbox traffic, and test that unapproved destinations are unreachable.

Production certification is blocked until the exact VPS and provider configuration is known and tested. Required evidence includes exact image digests, database migration and role verification, real model/provider smoke and task-quality tests, sandbox adversarial tests, TLS verification, tenant-isolation review, backup/restore, monitoring/alerts, and operator approval. A successful GitHub CI run is necessary but is not production certification.
