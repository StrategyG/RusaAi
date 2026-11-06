# RUSA-AI Agent Security and Operations

## Threat model

| Threat | Control | Residual requirement |
|---|---|---|
| Prompt injection in repository files | Treat repository, issue, memory, and tool content as untrusted; role/tool allowlists; policy outside model | Versioned red-team corpus and continuous adversarial tests |
| Credential exfiltration | No secret-read tool; no shell tool; secrets only in process environment; protected paths blocked; sensitive values redacted from model context | Secret manager, rotation, log scanning, repository secret scanning |
| Cross-tenant data leakage | Per-tenant API keys, per-tenant repository assignments, tenant_id filters, PostgreSQL RLS on queue and memory tables | Run isolation tests with the exact restricted API database role in staging |
| Unauthorized repository mutation | Exact global repository allowlist, per-tenant repository mapping, deterministic tenant/task work branch, protected paths denied | GitHub App installation must match the global allowlist |
| Branch collision or takeover | Branch identity is derived from tenant plus idempotency key/task ID; arbitrary work branches are rejected | Test duplicate submissions and branch-creation races against live GitHub |
| Malicious code execution | Worker never executes repository code; GitHub Actions performs CI in hosted runners | Pin actions and runner images; review workflow changes manually |
| Duplicate task delivery | Payload-checked tenant idempotency, fenced lease owner, heartbeat, checkpoint, idempotent branch/file/PR operations | Chaos test worker crash, database failover, and concurrent claim |
| Stale worker writes | Checkpoint, heartbeat, retry, and acknowledgement require current lease owner and unexpired lease | Validate with multiple worker replicas under load |
| Incomplete CI evidence | Bounded pagination; total-count consistency; exact commit SHA; all check runs complete and successful | GitHub API evidence is adapter provenance, not a cryptographic attestation |
| Changed code after test | Security worker re-reads CI and complete compare diff; a second read-only model pass reviews the retrieved diff; success requires identical tester/security/diff head SHA | Deployment must bind to the immutable verified commit and re-check release gates |
| Secret in a changed file | Gateway blocks detected secrets before writes, redacts common formats on reads, and orchestrator blocks diffs containing redaction markers | Add repository-specific secret scanners and verify against a red-team corpus |
| Oversized or unreviewable diff | Maximum 20 changed files, bounded patch size and total response size; incomplete diffs are blocked | Tune limits using representative repositories and load tests |
| Audit tampering | HMAC chain, file locking, fsync | Ship logs to immutable remote storage; manage key rotation through KMS |
| Provider/GitHub response exhaustion | Streaming response limits, timeouts, bounded file and diff sizes | Load-test limits and configure outbound egress allowlists |
| Dependency compromise | pip-audit in CI, constrained dependencies | Lockfile, SBOM, signed artifacts and provenance |
| Public API abuse | Per-tenant API keys, tenant header, request byte limit, request timeout | TLS gateway, distributed rate limits, WAF and IP policy |

## Database permission model

The Compose bootstrap creates three database identities:

- rusa_agent_owner: database bootstrap/owner identity; never used by application services.
- rusa_agent_worker: worker identity with BYPASSRLS because workers claim tasks across tenants. It is not a superuser and has no role-creation/database-creation permission.
- rusa_agent_api: API identity without BYPASSRLS. It has only SELECT and INSERT on rusa_agent_tasks, no UPDATE/DELETE/TRUNCATE, and no privileges on rusa_agent_memory or its sequence. Forced RLS restricts its task rows to the validated tenant context.

The task and memory tables enable and force row-level security. The API sets a transaction-local tenant context only after validating the tenant-specific API key. Queue insert/read operations also filter by tenant_id. The worker identity is intentionally privileged to process all tenants; keep its credential isolated from the API service. Queue schema initialization explicitly grants the API role only the task operations it needs and revokes legacy/default memory and sequence grants, including on existing database volumes.

The worker healthcheck verifies both schemas and their forced RLS policies. The init script runs only when the PostgreSQL data volume is first initialized. On existing volumes, worker startup migrations re-apply forced RLS policies and minimum API table grants; verify role creation and password changes separately because the init script is not replayed.

## Repository permission model

Configure RUSA_AGENT_ALLOWED_REPOSITORIES as an exact comma-separated global list of owner/name repositories. Configure RUSA_AGENT_TENANT_REPOSITORIES as a JSON map from each tenant ID to a non-empty list of repositories. Every tenant must have an API key and every tenant repository must be a subset of the global allowlist.

The API rejects submissions outside the authenticated tenant's assignment. The GitHub gateway independently enforces both the global list and tenant assignment before making a request. The task contract derives its work branch from the tenant and idempotency identity; clients cannot select arbitrary shared branches. Exact required check-run names are configured through RUSA_AGENT_REQUIRED_CI_CHECKS.

Create a dedicated GitHub App or fine-grained token scoped to the same global repository list. Minimum permissions:

- Contents: read/write, required for branch file changes.
- Pull requests: read/write, required to create a draft PR.
- Checks: read-only, required to observe CI.
- Metadata: read-only, generally required by GitHub.

Do not grant Administration, Actions write, Secrets, Environments, Deployments, or organization permissions to the agent credential. Configure protected branches so required CI checks and human review are mandatory.

## Secret handling

Required worker environment variables are RUSA_AGENT_DATABASE_URL, RUSA_AGENT_PROVIDER_BASE_URL, RUSA_AGENT_PROVIDER_API_KEY, RUSA_AGENT_MODEL, RUSA_AGENT_GITHUB_TOKEN, RUSA_AGENT_ALLOWED_REPOSITORIES, RUSA_AGENT_TENANT_REPOSITORIES, and RUSA_AGENT_AUDIT_KEY. The API requires RUSA_AGENT_API_DATABASE_URL, RUSA_AGENT_ALLOWED_REPOSITORIES, RUSA_AGENT_TENANT_REPOSITORIES, and RUSA_AGENT_TENANT_KEYS as a JSON map of tenant identifiers to unique ASCII API keys of at least 32 characters. Keep the database owner, worker, and API database passwords separate. Use a secret manager in production. Never commit real values to Git, task metadata, memory, or logs.

## Reliability

- Queue lease default: 300 seconds; task attempt default: 4.
- Worker renews its lease at approximately one-third of the lease interval.
- Checkpoints, heartbeats, retry, and acknowledgement are fenced by worker identity and lease expiry.
- A retry clears the prior failed checkpoint so a transient failure restarts from the task entry point; GitHub writes and PR creation must remain idempotent.
- Completed worker stage results are reused from a checkpoint after restart; external CI is always refreshed after a WAITING_EXTERNAL pause.
- Dependency manifests, container definitions, infrastructure, and deployment paths require human approval before modification.
- Worker and tool timeout default: 120 seconds.
- CI pending recheck default: 30 seconds.
- Repair cycles default: 2.
- PostgreSQL backup and restore drills are mandatory before launch.
- Alert on expired leases, repeated retries, queue age, blocked/approval tasks, provider failures, audit verification failures, and stale CI evidence.

## Human approval boundary

The runtime can autonomously create a deterministic work branch, modify permitted files, and open a draft PR. It cannot merge, deploy, release, change secrets, change permissions, modify CI workflows, or perform destructive actions. Human reviewers approve the PR and release through GitHub protected-branch and environment controls. High/critical risk and protected-path requests stop in awaiting_approval and require operator review; the runtime does not auto-approve them.

## Production checklist

- [ ] GitHub App/token has minimum repository scope and matches the exact configured global repository allowlist.
- [ ] Every tenant has a unique API key and an explicit repository assignment; cross-tenant assignment tests pass.
- [ ] main branch protection requires review and all required CI checks.
- [ ] API uses the restricted non-BYPASSRLS database role; worker uses the isolated BYPASSRLS role.
- [ ] RLS tests pass using the exact restricted API role, including cross-tenant insert/read attempts.
- [ ] PostgreSQL TLS, encryption at rest, backups, PITR, and restore drill are configured.
- [ ] Agent API is behind TLS gateway and distributed rate limiting.
- [ ] Provider/GitHub outbound egress is allowlisted.
- [ ] Audit is shipped to immutable remote storage and HMAC key is managed by KMS.
- [ ] Prompt-injection, tool abuse, duplicate delivery, stale-worker, secret-redaction, and worker-crash tests pass.
- [ ] Staging load/soak test, monitoring, alerting, incident response, and rollback are approved.
- [ ] Production model quality and safety benchmark has an owner, golden dataset, acceptance threshold, and signed result.
