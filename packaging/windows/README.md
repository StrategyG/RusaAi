# Windows Local Bundle — Release Contract

**Target:** Windows 11 x64  
**Distribution modes:** Online Installer and Offline Bundle  
**Current status:** NOT RELEASE-READY

This folder defines the package boundary for the future consumer installer. The repository currently contains the local first-run API-key bootstrap and web setup page, but it does not yet contain a signed, complete desktop bundle. Do not distribute this source tree as a plug-and-play installer.

## User experience target

1. User downloads the signed Online Installer or complete Offline Bundle.
2. Installer checks Windows version, x64 architecture, disk capacity and required virtualization support.
3. Installer installs or starts the supported local container runtime with user consent where OS elevation is required.
4. Installer verifies package signatures, image digests, model license, model manifest and every model artifact SHA-256.
5. Installer creates local-only secrets and persistent data directories.
6. Installer starts the RusaAi Control Plane, RusaModels Model Plane, local model runtime and local web dashboard.
7. Installer opens `http://127.0.0.1:3000/setup`.
8. The user creates and saves the one-time RUSA API key, then connects a supported editor.

No user-facing step should ask for Docker, Python, database, model path, API provider, or environment-variable configuration.

## Bundle layout contract

A release bundle is expected to contain:

```text
RUSA-AI-Local-win-x64/
  rusaai-local-bundle.json
  rusaai-local-bundle.sig
  Install-RusaAI.ps1
  Uninstall-RusaAI.ps1
  compose.local.yml
  images/
    rusaai-control-plane.tar
    rusaai-web.tar
    rusamodels-model-server.tar
    rusa-model-runtime.tar
  models/
    model-manifest.json
    model.gguf
    embedding-model/
  checksums.sha256
  LICENSES/
```

The Online Installer downloads the same signed manifest and pinned image/model assets. The Offline Bundle contains all required runtime assets and must be installable without contacting model hubs or cloud inference services.

## Required manifest fields

Validate the release manifest against [`local-bundle-manifest.schema.json`](local-bundle-manifest.schema.json). A manifest with `release_status: READY` must declare both model and embedding artifacts as `APPROVED`; the release pipeline must additionally verify the detached signature and every artifact digest.


The release manifest must bind:

- Bundle version, release channel, target OS and architecture.
- Exact OCI image digests for API, web, Model Server, model runtime and required local services.
- Exact model ID, immutable upstream revision, license, tokenizer identity/revision, context limit, model artifact filename and SHA-256.
- Exact embedding model identity, revision, license, dimensions and artifact/tree SHA-256.
- Required free disk and RAM floor for the selected model profile.
- SHA-256 for every package file.
- Signature key ID and signature algorithm.

The installer must reject missing, unsigned, expired, mismatched, or unapproved manifests. It must never substitute a different model silently.

## Local security requirements

- API and dashboard ports bind to loopback by default.
- Local API key store persists on a dedicated volume writable only by the API service account.
- Setup capability is generated per install and shared only with the server-side web process.
- Raw API keys are shown once; only SHA-256 digests are persisted.
- RusaModels credentials are separate, least-privilege and never sent to the browser or Agent Worker.
- MCP/RAG outbound connections require explicit user/admin approval, TLS, endpoint allowlists, scoped credentials, size/time limits and audit.
- Offline mode does not fall back to external inference.
- Uninstall preserves user data unless the user explicitly selects secure data removal.

## Current contributor preview launcher

For repository contributors only, `Start-LocalPreview.ps1` creates a local-only `.env` with cryptographically random secrets, restricts its Windows ACL, validates the Compose configuration, starts the RusaAi API/web/PostgreSQL control plane, and opens `http://127.0.0.1:3000/setup`.

Run from a checked-out repository on Windows 11 x64 with Docker Desktop and its WSL2 backend already installed:

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\windows\Start-LocalPreview.ps1
```

This is deliberately a **control-plane preview**, not the consumer installer. Its local Compose overlay disables model inference and uses the API liveness endpoint so first-run API-key onboarding is available before a model is provisioned. It does not install Docker Desktop, include RusaModels images, download model weights, or certify chat inference. Do not distribute it as the plug-and-play product.

## Release blockers

The bundle cannot be certified until all of these have evidence:

1. An approved foundation checkpoint and tokenizer; the current RusaAi-Edge manifest remains `DESIGN_ONLY`.
2. A built, checksummed, license-reviewed GGUF artifact and local embedding artifact.
3. RusaModels local Model Server + llama.cpp + embedding runtime packaged as pinned images.
4. A combined Compose bundle that starts both Control Plane and Model Plane without external inference.
5. Windows 11 clean-machine install, upgrade, uninstall and rollback tests.
6. Hardware profile tests on declared RAM/CPU/GPU targets.
7. Local API, chat, streaming, embeddings, editor, MCP and external RAG end-to-end tests.
8. Offline install and offline inference test.
9. Signed release manifest, SBOM, vulnerability scan and reproducible artifact checks.

The current branch implements foundational components only. It must not be tagged or advertised as a finished installer until every mandatory release gate is PASS.
