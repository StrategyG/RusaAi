# RusaAi Architecture v2.0 — repository implementation alignment

## Source authority and implementation boundary

- Source: supplied RusaAi_Architecture_v2.0.md
- Source version: 2.0.0
- Source date: 2026-11-11
- Source status: Proposed Architecture — Not Yet Implemented
- Source SHA-256: 912c791af03216d2eec4fe3b609f46628aea8063510609d5f8e5533c4af36246

The source document is the design authority for this alignment. Its proposed
components must not be described as implemented merely because a directory,
configuration, schema, or interface exists. This repository change is an
incremental implementation baseline, not a claim that the complete v2.0
architecture has shipped.

## Model identity and neural-core specification

The canonical target identity is rusaai-edge-1.54b (RusaAi-Edge-1.54B).
The model blueprint and YAML configuration now describe the v2.0 dense
decoder-only Transformer rather than the previous unrelated 96-layer MoE
blueprint.

| Property | v2.0 target |
|---|---:|
| Architecture | Dense decoder-only Transformer, Pre-Norm |
| Decoder layers | 32 |
| Hidden size | 2,048 |
| Query heads / KV heads | 16 / 4 |
| Head dimension / GQA ratio | 128 / 4 |
| FFN | SwiGLU, intermediate size 5,632 |
| Vocabulary | 48,000 |
| Embeddings | Tied |
| Position encoding | RoPE |
| RMSNorm epsilon | 1e-5 |
| Linear bias / initial dropout | Disabled / 0.0 |
| MoE | Disabled |
| Reference parameter count | 1,541,277,696 |
| Deployment quantization target | GGUF Q4_K_M |

The parameter reference is calculated as 32 × (10,485,760 attention
parameters + 34,603,008 SwiGLU parameters) + 98,304,000 tied embedding
parameters + 133,120 normalization parameters = 1,541,277,696.

The model target is design-only. A non-Qwen foundation checkpoint and
tokenizer are not selected; weights and GGUF artifacts are not built; training
has not started; and no device benchmark has been run. The 100.1M-parameter
from-scratch path is a separate research path, not the 1.54B product model.

The existing Qwen3-4B QLoRA configuration is retained as a separate legacy
AICode worker-contract experiment. It is not the RusaAi-Edge neural core, is
not evidence of a non-Qwen foundation checkpoint, and must not be promoted as
the v2.0 core model.

## Control Plane and Model Plane mapping

The repository now marks the two ownership boundaries without copying or
renaming the existing packages:

| Architecture plane | Current repository implementation | Status |
|---|---|---|
| Rusaai Control Plane | rusa_agents/, rusa_knowledge/, rusa_mcp/, migrations/, llm-project/serve/app.py | Existing baseline; not yet physically consolidated under Rusaai/ |
| RusaModels Model Plane | docker-compose.cpu.yml llama.cpp service, rusa_agents/gguf.py, scripts/verify_gguf_model.py, provider adapter | Partial CPU integration; no full registry or residency manager |
| Shared contracts | schemas/a2a/, schemas/llm/, shared/contracts/agent_contract/, shared/contracts/inference_contract/, shared/contracts/artifact_contract/, shared/schemas/ | Existing A2A/artifact schemas, new agent/artifact ownership markers, and new versioned inference envelope; inference envelope is not yet wired to the public endpoint |

Rusaai/ and RusaModels/ are ownership-boundary documents. They deliberately
avoid duplicate Python packages. Moving the existing code is deferred until
imports, images, migrations, deployment paths, and CI can be migrated in one
reviewed change.

## CPU-first inference baseline

The CPU deployment profile uses llama.cpp and a read-only GGUF artifact,
zero GPU-offload layers, one model-server parallel slot, and one active API
generation. The API admission controller now uses a FIFO queue with an exact
waiting limit, timeout cleanup, and cancellation-safe slot transfer. The
rusa-cpu-16 (16-GB) profile starts with at most three waiting API requests. The rusa-edge-8 (8-GB) profile uses the smaller bounded queue in .env.cpu-8gb.example
(one waiting request as a conservative repository default) and must not
increase context or background work without device-specific measurement.

The API-side admission queue does not yet arbitrate globally across API and
worker processes. The worker still calls the private llama.cpp endpoint
directly; the llama.cpp server's single parallel slot is therefore the final
cross-process active-generation guard. A single shared cross-process queue
and unified deadline propagation remain open Model Plane work. Do not claim
global FIFO fairness between API and worker callers.

Context starts at 4K. An 8K context is allowed only after memory validation
on the target device. Q8_0 KV-cache quantization remains optional and must be
checked against the selected llama.cpp build and model. CPU threads should be
chosen from physical-core availability and benchmarked; the checked-in value
of four is an initial profile, not a performance claim.

## Agent, memory, verification, and deployment status

- The current AICode workflow has six specialist worker roles. These are not
  the ten core agents specified by v2.0. See
  docs/AUTONOMOUS_IMPLEMENTATION_ROADMAP.md for the role-by-role mapping.
- The eight DevOps lifecycle specialists and their separate approval-gated
  runtime are not implemented.
- SQLite task memory, PostgreSQL task memory, tenant-scoped RAG, and audit
  facilities exist. The complete five-category memory architecture and its
  lifecycle/retention policies are not yet implemented as one unified store.
- Tool allowlists, deterministic policy checks, isolated validation, tenant
  controls, and audit evidence exist. The complete v2.0 PRM-Lite/ORM pipeline
  and its calibration/evaluation gates are not yet implemented end-to-end.
- Knowledge ingestion and retrieval exist. A complete Carbon OCR/ASR pipeline
  and native image/audio model path are not established by this repository.
- Linux Docker CPU deployment is represented. Windows 11 and Android ARM64
  deployment targets have not been validated here.
- Existing Prometheus metrics cover API requests and AICode workers. Queue
  wait, model memory, TTFT, and token-throughput instrumentation still need
  implementation and device benchmarks.

## Change-control rules

1. Keep the architecture source status and model-artifact status explicit.
2. Do not commit model weights, tokenizer payloads, calibration corpora, or
   private GGUF files.
3. Pin model, tokenizer, GGUF, and runtime image revisions and checksums before
   a production release.
4. Keep authorization in the deterministic Policy Engine and tool gateway;
   model output is never an authorization decision.
5. Preserve the existing tenant, queue, lease, audit, sandbox, and CI controls
   while migrating architecture boundaries.
6. Add a versioned contract rather than changing a published v1 contract
   incompatibly.
7. Treat Windows and Android as unverified targets until build, smoke,
   resource, and security evidence is recorded.

## Verification boundary for this change

The repository changes add an exact design blueprint, parameter-count
assertions, versioned inference schemas, and cancellation-safe API admission
control. They do not select or build a model checkpoint, run training, produce
a GGUF artifact, certify a deployment, implement all agents, or complete the
cross-process scheduler. Those items remain blocked on their stated evidence
and implementation work.
