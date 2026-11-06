# RUSA-AI Staging Deployment Runbook

## Purpose and deployment boundary

This runbook prepares an isolated Docker Compose staging deployment of RUSA-AI. It is not evidence that staging has already been deployed. Do not use production data, production credentials, or production endpoints during this exercise.

The release policy is human-approved merge and deployment. CI success makes a candidate eligible for review; it does not authorize merge or deployment.

## Prerequisites

- A staging VM or host with Docker Engine and Docker Compose v2.
- A private Docker network. PostgreSQL must not have a public port mapping.
- A TLS reverse proxy for any externally reachable API or MCP endpoint. The Compose services bind their host ports to loopback.
- A versioned, licensed model artifact with tokenizer and chat template at the configured model directory. Record its revision and SHA-256 checksum.
- A staging-only `.env` populated from a secret manager. Set file permissions to `0600`; never commit it.
- A staging PostgreSQL backup/restore plan.
- A staging GitHub token restricted to the approved repository list and required contents/PR/check-read permissions.
- A tenant-separated RAG evaluation set with approved, non-sensitive documents.

Required secrets/configuration include the database owner, worker and API passwords; inference API key; agent provider key and model; GitHub token; HMAC audit key; embedding provider key; tenant API key maps; MCP tenant token maps and SHA-256 token digests. Do not paste secrets into pull requests, chat, logs, or source control.

## Configuration checks

1. Configure `RUSA_MODEL_DIR` to the mounted model artifact directory and verify the model manifest/checksum.
2. Configure `RUSA_AGENT_PROVIDER_BASE_URL`, `RUSA_AGENT_PROVIDER_API_KEY`, and `RUSA_AGENT_MODEL` for the staging agent provider.
3. Configure `RUSA_AGENT_TENANT_KEYS` and `RUSA_AGENT_TENANT_REPOSITORIES` with staging-only tenants and explicit repository allowlists.
4. Configure `RUSA_RAG_ENABLED=true` only when the embedding provider and database migration are ready. Set `RUSA_EMBEDDING_BASE_URL`, `RUSA_EMBEDDING_MODEL`, and the provider key if required.
5. For remote MCP tool routing, set `RUSA_MCP_SERVER_URL=http://rusa-mcp:8002/mcp`, `RUSA_MCP_INTERNAL_HTTP_HOSTS=rusa-mcp`, and `RUSA_MCP_TENANT_TOKENS`. Configure `RUSA_MCP_TENANT_KEYS` on the MCP service as a JSON map from SHA-256 bearer-token digests to tenant IDs. Each raw worker token must hash to the digest configured for the same tenant.
6. Set `RUSA_MCP_ALLOWED_HOSTS` to the exact hostnames accepted by the reverse proxy and internal service network. Do not use a wildcard host allowlist.
7. Validate configuration without printing resolved secret values:

~~~bash
docker compose config --quiet
docker compose --profile knowledge config --quiet
docker compose --profile migrate config --quiet
~~~

## Deployment procedure

1. Check out the exact human-approved candidate commit. Record `git rev-parse HEAD` and the image tags/digests. Confirm the PR is approved and all required checks on that exact SHA are green.
2. Populate the staging `.env` from the secret manager. Verify that model artifacts and persistent storage paths exist.
3. Start PostgreSQL and wait for its health check:

~~~bash
docker compose up -d rusa-agent-db
docker compose ps rusa-agent-db
~~~

4. Take a database backup before schema changes. Store the backup outside the container and verify that it is readable:

~~~bash
docker compose exec -T rusa-agent-db pg_dump -U rusa_agent_owner -d rusa_agent -Fc > rusa-agent-staging-before-rag.dump
docker compose exec -T rusa-agent-db pg_restore --list < rusa-agent-staging-before-rag.dump
~~~

5. Apply the versioned RAG migration using the dedicated one-shot service. It runs as the database owner, not as the MCP API identity:

~~~bash
docker compose --profile migrate run --rm rusa-knowledge-migrate
~~~

6. Verify the restricted role and row-level security flags:

~~~bash
docker compose exec -T rusa-agent-db psql -U rusa_agent_owner -d rusa_agent -c "SELECT rolname, rolcanlogin, rolsuper, rolbypassrls FROM pg_roles WHERE rolname IN ('rusa_agent_api', 'rusa_agent_worker', 'rusa_knowledge_worker');"
docker compose exec -T rusa-agent-db psql -U rusa_agent_owner -d rusa_agent -c "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname IN ('rusa_knowledge_documents', 'rusa_knowledge_chunks');"
~~~

The knowledge role must be NOLOGIN, NOSUPERUSER, and NOBYPASSRLS. Both knowledge tables must have RLS enabled and forced. The MCP API role must remain read-only.

7. Start the MCP service if using remote MCP tools:

~~~bash
docker compose --profile knowledge up -d --build rusa-mcp
~~~

8. Start the inference API, worker, and agent API:

~~~bash
docker compose up -d --build rusa-api rusa-agent-worker rusa-agent-api
docker compose ps
~~~

9. Verify readiness endpoints from the host. These services bind to loopback; expose them externally only through the TLS reverse proxy:

~~~bash
curl --fail --silent http://127.0.0.1:8000/health/ready
curl --fail --silent http://127.0.0.1:8001/health/ready
curl --fail --silent http://127.0.0.1:8002/health/ready
~~~

10. Verify worker database access using the configured restricted role:

~~~bash
docker compose exec -T rusa-agent-worker python -m rusa_knowledge.cli check
~~~

11. Use an authenticated MCP host/Inspector to list the three read-only knowledge tools and execute a small `list_collections` or `search_knowledge` request. Confirm missing, invalid, and cross-tenant tokens are rejected.
12. Run the approved tenant-separated RAG gold set. Record Recall@5/10, MRR@10, nDCG@10, source attribution accuracy, unsupported-claim rate, and p50/p95 retrieval latency. Do not mark the RAG release gate complete without domain-specific results.
13. Submit one non-destructive canary agent task using a staging tenant. Verify queue persistence, lease renewal, worker recovery, draft PR creation, exact-SHA CI evidence, and audit records. Do not merge the PR or deploy a release from the agent.

## Rollback

- Stop application services without deleting volumes: `docker compose down`.
- Restore the previously approved image tags/commit and redeploy using the recorded configuration version.
- Keep the additive RAG schema and data during application rollback. Do not run `docker compose down -v` on staging or production.
- Restore the database backup only when the migration or data integrity check requires it and after owner approval.
- Rotate any credential suspected of exposure and invalidate the affected tenant token digest.

## Staging exit criteria

- Exact candidate SHA has green tests, dependency audits, Docker builds, and container smoke tests.
- PostgreSQL migration job succeeds; restricted roles, grants, and forced RLS are verified.
- Tenant isolation, prompt-injection, secret-screening, and MCP authentication tests pass.
- Model artifact, license, checksum, quality/safety benchmark, and target-hardware capacity evidence are recorded.
- TLS ingress, distributed rate limiting, egress restrictions, backups/PITR, restore drill, monitoring, alerts, immutable audit retention, and rollback rehearsal are verified.
- A staging soak is completed and the owner approves the release.

Passing this runbook does not automatically authorize production deployment. Production remains a separate approval and release decision.
