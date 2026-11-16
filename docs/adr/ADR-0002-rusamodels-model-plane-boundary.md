# ADR-0002: RusaModels owns the Model Plane

- Date: 2026-11-12
- Status: Accepted for repository integration alignment
- Record key: `ADR-0002-rusamodels-model-plane-boundary.md` (numeric ID collision; see [ADR index](README.md))
- Source: RusaAi_Architecture_v2.0, RUSA_AI_CPU_Deployment_Configuration_v1.0

## Context

The approved architecture separates the RusaAi Control Plane from the RusaModels Model Plane. RusaAi owns agent/workflow decisions, task state, policy, tool execution, verification, audit and the Model Access Gateway. RusaModels owns model registry, artifact lifecycle, model serving, inference routing, CPU runtime, model residency, embedding runtime and model telemetry.

The RusaAi CPU Compose profile previously started its own llama.cpp/GGUF service and mounted model weights in the RusaAi repository deployment. RusaModels already provides a Model Server API with an authenticated OpenAI-compatible proxy and private llama.cpp/embedding runtimes. Maintaining two CPU model-serving paths duplicated artifact ownership and bypassed the intended Model Plane boundary.

## Decision

1. RusaAi is the Control Plane. RusaModels is the only production Model Plane.
2. The inference request path is:
   `RusaAi Worker -> RusaAi API/Model Access Gateway -> RusaModels Model Server -> private model runtime`.
3. RusaAi CPU Compose must not define a llama.cpp service, mount GGUF weights, or set model-runtime thread/context/memory controls.
4. RusaAi calls the RusaModels `/openai/v1` endpoint using a dedicated RusaModels API principal with role `inference`. Manager and Administrator tokens are not accepted as the integration credential.
5. The Worker receives only tenant-scoped RusaAi API keys. It never receives the RusaModels inference token, database credentials, object-store credentials, or direct runtime URL.
6. `RUSA_MODEL_NAME` must be explicitly configured in production and must exactly match the model ID exposed by RusaModels. The `rusa-general-4b` Qwen3-4B profile is a compatibility smoke-test alias, not RusaAi-Edge.
7. Production RusaAi configuration rejects `RUSA_LOCAL_MODEL_PATH` and `RUSA_MODEL_PATH`. The legacy local Transformers/vLLM adapter is development-only.
8. RusaModels owns GGUF/safetensors intake, manifests, hashes, runtime loading and artifact verification. No model weights or GGUF verifier are maintained in RusaAi.

## Consequences

- The RusaAi API has one authenticated upstream Model Server endpoint; it does not know llama.cpp container internals.
- RusaModels can change its runtime or artifact implementation without changing RusaAi agent/workflow code, provided the versioned OpenAI-compatible contract remains compatible.
- RusaAi CPU Compose can be deployed independently only after the RusaModels Model Server is reachable and its dedicated inference principal is configured.
- The RusaAi 8-GB resource overlay covers Control Plane containers only. The combined host budget must include RusaModels and retain the architecture's free-RAM reserve.
- The current RusaModels CPU profile is a 4-vCPU/16-GB Codespaces smoke profile. It does not certify the combined `rusa-edge-8` device profile.
- The canonical shared Agent/Worker knowledge store is selected by the separate [canonical knowledge-store ADR](ADR-0002-canonical-shared-agent-worker-knowledge-store.md): RusaAi `rusa_knowledge`, subject to its implementation and release gates. This Model Plane ADR does not override that decision. Embedding model/dimension, vector index generation and corpus topology remain separate technical decisions subject to benchmark and approval.

## Validation

- Compose regression tests assert that RusaAi CPU deployment has no llama.cpp service or GGUF mount and requires the RusaModels Model Server URL and inference token.
- Configuration tests reject production local model paths, missing model IDs and placeholder model hosts.
- RusaAi Worker configuration tests retain tenant-scoped API keys and route inference through the RusaAi API gateway.
- RusaModels CI separately validates its Model Server, GGUF runtime and embedding service.
- Passing CI does not certify production latency, model quality, 8-GB fit, or autonomous-agent compatibility.
