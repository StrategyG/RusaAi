# RUSA-AI Independent QA Runtime

**Status:** Implemented in the integration branch; production deployment and certification remain **HOLD** until the target VPS, dedicated QA Docker daemon, key provisioning, and staging tests are verified.

## 1. Trust boundary

The QA service is a separate Compose service named `qa`. It uses the pinned worker image digest as its code image, but runs as a separate container with a separate workload boundary and credentials.

- The worker receives the QA service URL, a service-to-service bearer token, the QA Ed25519 public key, and the expected QA sandbox image digest.
- The worker container is explicitly given an empty `RUSA_QA_PRIVATE_KEY_B64`.
- The API container is explicitly given empty QA secrets.
- The QA container receives the Ed25519 private key and service token, but no GitHub token, worker/API database DSN, audit HMAC key, model API key, or tenant API/MCP key maps.
- The QA service is attached only to the private `rusa-internal` network. Do not publish port 8010 to the host or public ingress.
- The QA service uses a dedicated rootless Docker daemon socket. It must not reuse the worker's rootless Docker socket.
- The QA service does not mount the worker workspace, Git mirror, or Git credentials. It receives an artifact archive over the private service API.

This creates a separate signing identity and runtime process. It is not yet mutual-TLS authenticated: worker-to-QA transport currently uses a high-entropy bearer token on the private Compose network. Add mTLS before claiming the service boundary is fully enterprise-certified.

## 2. Artifact and evidence binding

The release candidate is represented by the staged Git tree, not by a mutable directory copied after testing.

1. `GitHubPublisher.prepare_candidate_snapshot()` stages only paths permitted by the existing publication allowlist.
2. It exports the exact Git index tree as a deterministic tar archive. It rejects symlinks, submodules, non-regular Git objects, credential-like paths, and protected configuration paths.
3. The worker sends the archive with the authenticated tenant ID, task ID, and Git tree SHA.
4. The QA service safely extracts only regular files, rejects path traversal, duplicate paths, symlinks, hardlinks, special files, oversized files, and credential-like paths, then computes a canonical SHA-256 manifest digest. The digest covers path, executable mode, file size, and SHA-256 file content.
5. QA runs the fixed profiles `pytest`, `bandit`, `ruff`, and `compile` in the isolated Docker sandbox. The sandbox is non-root, read-only, network-disabled, capability-dropped, resource-limited, and has no access to QA service credentials.
6. QA signs a short-lived Ed25519 verdict containing the schema version, service identity, key ID, tenant ID, task ID, Git tree SHA, artifact digest, sandbox image digest, profile exit codes, output digests, issue time, and expiry.
7. The worker verifies the signature, identity, key ID, tenant/task binding, expiry, expected sandbox image, complete profile set, and PASS/FAIL consistency. It independently recomputes the artifact digest from its current candidate tree.
8. Immediately before publication, the publisher recomputes the candidate Git tree and artifact digest. It refuses to commit if either differs from the signed QA verdict. After commit, it verifies that `HEAD^{tree}` is still the exact attested tree.

The Git tree SHA binds the Git object graph; the SHA-256 manifest digest provides a second, algorithm-independent content binding. A worker cannot turn a local test result into a signed QA PASS because only the QA service holds the Ed25519 private key.

## 3. Required QA profiles

| Profile | Purpose |
|---|---|
| `pytest` | Full repository test suite |
| `bandit` | Static security checks on application packages |
| `ruff` | Lint checks across application packages and tests |
| `compile` | Python bytecode compilation across application packages, serving code, and tests |

A missing profile, malformed exit code, image mismatch, non-zero result, invalid signature, stale verdict, or changed candidate tree cannot authorize publication.

## 4. Production environment

Add the following to the VPS-only secret-managed `.env` file. Never commit real values.

| Variable | Purpose |
|---|---|
| `RUSA_QA_URL` | Private worker endpoint; Compose uses `http://qa:8010` |
| `RUSA_QA_SERVICE_TOKEN` | High-entropy shared worker-to-QA bearer token |
| `RUSA_QA_PRIVATE_KEY_B64` | Raw 32-byte Ed25519 private key, QA service only |
| `RUSA_QA_PUBLIC_KEY_B64` | Matching raw 32-byte Ed25519 public key, worker only |
| `RUSA_QA_KEY_ID` | Stable identifier for the signing key |
| `RUSA_QA_SANDBOX_IMAGE` | Immutable sandbox image digest; deployment keeps it equal to `RUSA_SANDBOX_IMAGE` |
| `RUSA_QA_DOCKER_SOCKET` | Dedicated rootless QA Docker socket, different from worker socket |
| `RUSA_QA_DOCKER_GID` | Group ID that can access the QA Docker socket |
| `RUSA_QA_WORKSPACE_ROOT` | Host path mounted at the same absolute path inside QA service |
| `RUSA_QA_WORKSPACE_GID` | Shared group ID allowing QA service and its rootless daemon to read/write the QA-only workspace |
| `RUSA_QA_CHECK_TIMEOUT_SECONDS` | Per-profile sandbox timeout |
| `RUSA_QA_VERDICT_TTL_SECONDS` | Signed verdict lifetime; maximum accepted lifetime is one hour |

The QA service uses the worker image digest as its code image; there is no separate QA image digest. Its separate trust boundary comes from the service container, credentials, network attachment, QA-only workspace, and dedicated rootless Docker daemon.

### Generate the Ed25519 key pair offline

Run this on a trusted administrative machine with the `cryptography` package installed. Store the private value only in the QA service secret store. Put the public value in the worker environment.

```bash
python - <<'PY'
import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

key = Ed25519PrivateKey.generate()
private = key.private_bytes(
    serialization.Encoding.Raw,
    serialization.PrivateFormat.Raw,
    serialization.NoEncryption(),
)
public = key.public_key().public_bytes(
    serialization.Encoding.Raw,
    serialization.PublicFormat.Raw,
)
print("RUSA_QA_PRIVATE_KEY_B64=" + base64.b64encode(private).decode())
print("RUSA_QA_PUBLIC_KEY_B64=" + base64.b64encode(public).decode())
PY

python -c 'import secrets; print("RUSA_QA_SERVICE_TOKEN=" + secrets.token_urlsafe(48))'
```

Use a new key ID for each key rotation. Update the QA private key and worker public key as one coordinated change, then restart and verify the QA readiness endpoint before enabling task intake.

## 5. Host and Docker prerequisites

The QA service must have its own rootless Docker daemon. The QA daemon must have the exact `RUSA_QA_SANDBOX_IMAGE` digest pre-pulled. The deployment script pulls the same sandbox digest through both the worker and QA rootless sockets and enforces that the two configured sandbox digests match.

The QA workspace root is a host bind mount at the same absolute path inside the QA container. This is necessary because the rootless Docker daemon creates sandbox containers outside the QA service container and must be able to resolve the bind-mount source path. The host directory must be writable by the QA service UID and readable by the QA rootless daemon user/group. Do not mount this path into the worker or API containers.

The QA service has no egress network. Each test sandbox uses `--network=none`. The worker can reach QA on the private internal network; QA cannot call GitHub, PostgreSQL, model endpoints, or public services.

## 6. Validation commands

Run focused tests:

```bash
python -m pytest -q \
  tests/test_qa_attestation.py \
  tests/test_qa_service.py \
  tests/test_qa_client.py \
  tests/test_qa_policy_validation.py \
  tests/test_aicode_runtime.py \
  tests/test_git_workspace.py
```

Run the full repository checks:

```bash
python -m pytest -q
ruff check rusa_agents rusa_knowledge rusa_mcp tests llm-project/serve
bandit -q -r rusa_agents rusa_knowledge rusa_mcp
python -m compileall -q rusa_agents rusa_knowledge rusa_mcp llm-project/serve tests
docker compose --env-file .env -f docker-compose.production.yml config --quiet
```

Before production, verify the QA service on staging with negative cases: missing/incorrect token, path traversal, duplicate archive paths, symlink and hardlink archive entries, oversized archives, wrong tenant/task/tree, expired verdict, invalid signature, mismatched sandbox image, profile failure, QA outage, workspace mutation after QA, and artifact digest mismatch immediately before commit.

## 7. Remaining limitations and release decision

- mTLS is not yet implemented; transport relies on a high-entropy bearer token and private network isolation.
- The QA service uses the worker image digest as its code image; a separately built minimal QA image is a future hardening improvement.
- The service uses one active signing key ID/public key per worker configuration; coordinated rotation is required.
- The central PDP/PEP is not yet enforced at every external tool/data/publication boundary. The current changes add signed QA enforcement and an exact-tree publication gate; they do not complete global policy enforcement.
- Worker and sandbox egress enforcement remains an open production blocker.
- Dedicated rootless QA daemon provisioning, host workspace permissions, key injection, staging deployment, rollback rehearsal, and adversarial integration tests have not been performed here.
- GitHub Actions currently reports failed workflow runs with zero jobs on this integration branch; therefore no green CI evidence exists for the current head.

Keep `RUSA_AICODE_ENABLED=false`. Do not merge, release, or enable production task intake until the exact branch head passes CI and all staging/deployment prerequisites above are evidenced.
