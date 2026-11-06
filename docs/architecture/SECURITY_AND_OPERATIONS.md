# RUSA-AI Agent Security and Operations

## Threat model

| Threat | Control | Residual requirement |
|---|---|---|
| Prompt injection in repository files | Treat retrieved content as untrusted; role/tool allowlists; policy outside model | Red-team corpus and continuous adversarial tests |
| Credential exfiltration | No secret-read tool; no shell tool; secrets only in process environment | Secret manager, rotation, log scanning |
| Cross-tenant data leakage | Per-tenant API keys, tenant_id filters, PostgreSQL RLS on queue and memory tables | Test RLS with the exact restricted API database role in staging |
| Unauthorized repository mutation | One repository per task; work branch only; protected paths denied | GitHub App installation restricted to target repositories |
| Malicious code execution | Worker never executes repository code; GitHub Actions performs CI in hosted runners | Pin actions and runner images; review workflow changes manually |
| Duplicate task delivery | tenant-scoped idempotency key, lease, idempotent branch/file/PR operations | Chaos test worker crash and concurrent claim |
| Audit tampering | HMAC chain, file locking, fsync | Ship logs to immutable remote storage; rotate keys via KMS |
| Provider outage | bounded HTTP timeout, durable queue, worker lease | Circuit breaker, provider fallback policy, SLO and alerting |
| Dependency compromise | pip-audit in CI, constrained dependencies | Lockfile, SBOM, signed artifacts and provenance |
| Public API abuse | per-tenant API keys, tenant header, request validation | TLS gateway, distributed rate limits, WAF and IP policy |

## Database permission model

The Compose bootstrap creates three database identities:

- rusa_agent_owner: database bootstrap/owner identity; never used by application services.
- rusa_agent_worker: worker identity with BYPASSRLS because workers claim tasks across tenants. It is not a superuser and has no role-creation/database-creation permission.
- rusa_agent_api: API identity without BYPASSRLS. It has table privileges but is subject to row-level security on task and memory tables.

The task and memory tables enable and force row-level security. The API sets a transaction-local tenant context only after validating the tenant-specific API key. Queue insert/read operations also filter by tenant_id. The worker identity is intentionally privileged to process all tenants; keep its credential isolated from the API service.

The init script runs only when the PostgreSQL data volume is first initialized. For an existing volume, apply the role grants and RLS migrations explicitly; do not assume a new init script will be replayed automatically.

## Repository permission model

Create a dedicated GitHub App or fine-grained token scoped to the single target repository. Minimum permissions:

- Contents: read/write, required for branch file changes.
- Pull requests: read/write, required to create a draft PR.
- Checks: read-only, required to observe CI.
- Metadata: read-only, generally required by GitHub.

Do not grant Administration, Actions write, Secrets, Environments, Deployments, or organization permissions to the agent credential. Configure protected branches so required CI checks and human review are mandatory.

## Secret handling

Required worker environment variables are RUSA_AGENT_DATABASE_URL, RUSA_AGENT_PROVIDER_BASE_URL, RUSA_AGENT_PROVIDER_API_KEY, RUSA_AGENT_MODEL, RUSA_AGENT_GITHUB_TOKEN, and RUSA_AGENT_AUDIT_KEY. The API requires RUSA_AGENT_API_DATABASE_URL and RUSA_AGENT_TENANT_KEYS as a JSON map of tenant identifiers to unique API keys of at least 32 characters. Keep the database owner, worker, and API database passwords separate. Use a secret manager in production. Never commit real values to Git, task metadata, memory, or logs.

## Reliability

- Queue lease default: 300 seconds; task attempt default: 4.
- Worker and tool timeout default: 120 seconds.
- CI pending recheck default: 30 seconds.
- Repair cycles default: 2.
- PostgreSQL backup and restore drills are mandatory before launch.
- Alert on expired leases, repeated retries, queue age, blocked tasks, provider failures, and audit verification failures.

## Human approval boundary

The runtime can autonomously create a branch, modify permitted files, and open a draft PR. It cannot merge, deploy, release, change secrets, change permissions, modify CI workflows, or perform destructive actions. Human reviewers approve the PR and release through GitHub protected-branch and environment controls. High/critical risk and protected-path requests stop in awaiting_approval and require operator review; the runtime does not auto-approve them.

## Production checklist

- [ ] GitHub App/token has minimum repository scope and no administration/secrets/deployment rights.
- [ ] main branch protection requires review and all required CI checks.
- [ ] Each tenant has a unique API key; rotation and revocation are tested.
- [ ] API uses the restricted non-BYPASSRLS database role; worker uses the isolated BYPASSRLS role.
- [ ] RLS tests pass using the exact restricted API role, including cross-tenant insert/read attempts.
- [ ] PostgreSQL TLS, encryption at rest, backups, PITR, and restore drill are configured.
- [ ] Agent API is behind TLS gateway and distributed rate limiting.
- [ ] Provider/GitHub outbound egress is allowlisted.
- [ ] Audit is shipped to immutable remote storage and HMAC key is managed by KMS.
- [ ] Prompt-injection, tool abuse, duplicate delivery, and worker crash tests pass.
- [ ] Staging load/soak test, monitoring, alerting, incident response, and rollback are approved.


## Database least-privilege boundary

The API database role is granted only SELECT and INSERT on the tenant-scoped task table. It receives no UPDATE/DELETE privileges and no access to episodic memory tables or sequences. The worker role owns queue and memory operations; row-level security remains enabled and forced on tenant-scoped tables. Existing volumes must run the queue/memory initialization migration so prior default grants are revoked.
