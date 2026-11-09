# RUSA-AI Advanced Code Refinery v3.0

**Status:** implementation branch; MicroVM supervisor integration contract, not a production certification.  
**Autonomy profile:** autonomous coding in a tenant-authorized task worktree; human approval remains required before merge or deployment.

## 1. Selected configuration

| Area | Configuration |
|---|---|
| Execution | Hybrid Docker + MicroVM |
| LLM | Primary OpenAI-compatible endpoint, optional local OpenAI-compatible endpoint, fail-closed mock |
| Approval | Human approval before merge/deploy |
| Quality threshold | 85/100 plus mandatory syntax/security evidence |
| Refinement | Existing AICode bounded repair cycles remain authoritative |

The existing AICode runtime, PostgreSQL task queue, six specialist workers, tool gateway, audit ledger, and draft-PR flow remain canonical. This package adds quality scoring and provider/sandbox adapters; it does not create a second orchestrator or queue.

## 2. Code quality gate

The Security worker receives a new `run_check(profile="refinery")` profile. The gateway:

1. Determines changed, staged, and untracked Python files from the assigned Git worktree.
2. Reads only those files through the bounded workspace path resolver.
3. Parses source with Python AST; analyzed code is never imported or executed.
4. Calculates type safety, documentation, error handling, code quality, complexity, security, and testability scores.
5. Emits a canonical JSON report and SHA-256 report digest.

Weights:

| Dimension | Weight |
|---|---:|
| Type safety | 15% |
| Documentation | 10% |
| Error handling | 15% |
| Code quality | 15% |
| Complexity | 10% |
| Security | 20% |
| Testability | 15% |

The default threshold is 85. Configure with `RUSA_REFINERY_MIN_SCORE`. Syntax errors, credential-like literals, and AST-detected dangerous execution primitives block the gate independently of the aggregate score. The orchestrator verifies the numeric score, report digest, syntax status, and blocking-finding count; a model-authored claim cannot satisfy the gate.

Scores are deterministic heuristics. They do not prove functional correctness, test coverage, absence of vulnerabilities, or release readiness. Pytest, Ruff, Bandit, compile checks, and CI remain separate required evidence.

If no Python files changed, the refinery reports `not_applicable=true` with an auditable no-op result. The rest of the required test/security checks still apply.

## 3. Hybrid LLM provider

The coding runtime uses this fallback order:

1. Primary `RUSA_MODEL_BASE_URL` / `RUSA_MODEL_NAME`.
2. Local endpoint `RUSA_LOCAL_MODEL_BASE_URL` / `RUSA_LOCAL_MODEL_NAME` (required in production).
3. Mock circuit-breaker response.

Both live providers must return the bounded JSON action envelope already required by AICode. Invalid JSON, unknown fields, or malformed actions cause fallback. The mock never fabricates code or test evidence; it returns `blocked=true`, causing the worker to stop safely.

Local endpoint configuration (required in production; optional in development):

- `RUSA_LOCAL_MODEL_BASE_URL`
- `RUSA_LOCAL_MODEL_NAME` (default `rusa-local`)
- `RUSA_LOCAL_MODEL_API_KEY` (optional where the endpoint requires it)

HTTPS is required except for loopback development endpoints. Production requires HTTPS. Model endpoint data residency, retention, access control, and processor approval remain deployment-owner decisions.

## 4. Hybrid Docker + MicroVM execution

The existing rootless, network-disabled Docker sandbox remains the standard backend for ordinary checks. The hybrid router classifies dynamic `pytest` execution using bounded static inspection. Detected dynamic execution, shell-enabled subprocesses, native/binary artifacts, install lifecycle hooks, unparseable Python, or inventory-limit uncertainty route to MicroVM.

**Fail-closed rule:** if a high-risk test requires MicroVM and the MicroVM backend is absent or returns unverifiable evidence, the task fails/blocks. The system never silently falls back to Docker for a high-risk workload.

Production startup requires the MicroVM backend by default. Environment:

- `RUSA_MICROVM_BASE_URL`: internal HTTPS URL of the trusted MicroVM supervisor.
- `RUSA_MICROVM_API_KEY`: bearer credential, at least 24 UTF-8 bytes.
- `RUSA_MICROVM_REQUIRED`: Optional Boolean for non-production. Production always requires MicroVM and rejects an explicit false value.
- `RUSA_MICROVM_TIMEOUT_SECONDS`: 30–3600 seconds, default 900.

The HTTP adapter contract is:

### Readiness

`GET /health/ready`

Expected JSON:

```json
{"ready": true}
```

### Execute a fixed profile

`POST /v1/executions` as multipart/form-data with:

- `snapshot`: sanitized `workspace.tar.gz` file; no Git metadata, symlinks, credentials, secret files, or private keys.
- `profile`: one of `pytest`, `compile`, `ruff`, `bandit`.
- `timeout_seconds`, `cpu_count`, `memory_mib`.
- `snapshot_sha256`: SHA-256 of the exact compressed snapshot.

Expected JSON response:

```json
{
  "run_id": "opaque-run-id",
  "exit_code": 0,
  "snapshot_sha256": "64-hex-character-digest",
  "runner_image_digest": "sha256:64-hex-character-digest",
  "stdout": "bounded text",
  "stderr": ""
}
```

The client verifies the echoed snapshot digest and immutable runner image digest. It does not accept an arbitrary command from the model. The supervisor must independently enforce a Firecracker/MicroVM boundary, no host credentials, bounded CPU/memory/PIDs/disk/time, restricted network, disposable filesystem, and signed/pinned runner image.

**Important:** this PR implements the client, contract, routing, and mocked protocol tests. It does not deploy or certify the Firecracker supervisor. Until a real supervisor is provisioned and independently tested, production readiness remains blocked.

## 5. Human approval boundary

AICode may analyze, plan, write allowed files in its task worktree, run validation, push a generated feature branch, and open a draft PR according to existing policy.

This change does not authorize:
- PR merge;
- release or production deployment;
- production infrastructure changes;
- secret or permission changes;
- changes to protected manifests, migrations, CI workflows, or deployment configuration.

The existing queue `awaiting_approval` transition remains the human intervention boundary. No merge/deploy automation is added.

## 6. Tests and evidence

New unit tests cover:
- AST score and syntax/security hard gates;
- changed-file scope;
- security-worker profile registration and gateway invocation;
- primary → local → mock provider fallback;
- rejection of model-supplied evidence fields;
- Docker vs MicroVM routing;
- fail-closed behavior when MicroVM is unavailable;
- MicroVM readiness, TLS requirement, snapshot digest binding, and image digest validation.

The repository CI must pass on the exact PR head. MockTransport tests validate the HTTP client contract only; they are not evidence that a real Firecracker supervisor is available or secure.
