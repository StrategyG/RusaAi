# RUSA AI Ecosystem — Repository Dependency Map

**Snapshot:** 2026-11-14  
**Architecture authority:** Roadmap v3.0 plus verified current-main ADRs, subject to CHUNK 01 reconciliation.

## 1. Ownership boundaries

### RusaAi — Control Plane and canonical knowledge authority

Owns:
- User/tenant identity context, authorization, task/workflow state and orchestration.
- Agent/worker role policy, tool authorization, approval gates and audit.
- Workspace/Git operations, sandboxed tool execution and evidence collection.
- Canonical shared Agent/Worker knowledge through `rusa_knowledge`.
- Tenant/project-aware retrieval, knowledge lifecycle, context assembly and publication governance.

Must not:
- Own model weights, GGUF lifecycle or model runtime internals.
- Give workers direct RusaModels database, object-store or runtime credentials.
- Let retrieved knowledge grant permissions or override policy.

### RusaModels — Model Plane

Owns:
- Model catalog/registry, immutable model versions, manifests and artifact lifecycle.
- Model Server, inference runtime, model routing and embedding runtime.
- Quant Server/Worker, conversion/quantization and model artifact integrity.
- Model/runtime telemetry and model-quality evidence.

Must not:
- Own RusaAi workflow decisions, tenant task state, agent tools or knowledge rights/publication.
- Be accessed by RusaAi through its database or object storage.

Required service boundary:
`RusaAi Worker → RusaAi API / Model Access Gateway → authenticated RusaModels Model Server → private model runtime`.

RusaAi integration uses a dedicated RusaModels `inference` principal and the versioned API boundary. The worker must not receive the RusaModels token or direct runtime URL.

The CPU Compose profile configures this boundary explicitly: worker gateway mode is enabled, the worker calls the RusaAi API using tenant-scoped RusaAi keys, and direct RusaModels credentials are cleared from the worker. The generic `.env.example` defaults gateway mode to false and the provider has a direct-call fallback. Therefore production fail-closed enforcement of gateway mode and absence of direct Model Plane credentials remains a CHUNK 02/06 requirement, not a universal property proven by source inspection.

### RAG_Cerdas / IM19 — Isolated Knowledge Intelligence pilot

Owns within its isolated pilot:
- Candidate-source discovery and source metadata.
- PDF/EPUB processing, extraction/OCR, structured knowledge mining and candidate review.
- Private pilot schema, review workflow, source-rights evidence and retrieval experimentation.

Must not:
- Be treated as the canonical shared Agent/Worker store.
- Be exposed as a multi-tenant answer-facing production API.
- Cross-read/write, dual-write, mirror or bulk-sync with RusaAi without a separately approved ADR and versioned contract.

## 2. Current high-level flow

```text
User / trusted caller
        |
        v
RusaAi API — identity, tenant, authorization and task contract
        |
        v
Task queue / lease / checkpoint
        |
        v
RusaAi Agent orchestrator
        |
        +-------------------------------+
        |                               |
        v                               v
RusaAi rusa_knowledge              RusaAi Policy Engine
tenant/project-scoped retrieval    and Tool Gateway
        |                               |
        | embeddings/inference           v
        +---- authenticated ------> RusaModels Model Server
                                        |
                                        v
                                private model/embedding runtime

RAG_Cerdas IM19:
approved source discovery → rights/versioned extraction → candidate knowledge
       (isolated pilot; no direct connection to canonical RusaAi store)
       (future transfer only through approved contract, rights and review gates)
```

The diagram describes the target boundary and currently implemented code surfaces. It is not proof that the complete flow is deployed or production-active.

## 3. Relevant current interfaces and artifacts

| Interface / artifact | Owner | Consumer | Current evidence / limitation |
|---|---|---|---|
| RusaAi task/worker contracts | RusaAi | Agent runtime and workers | Six fixed roles and WorkerResult exist; typed immutable knowledge references are not yet explicit in the inspected result contract |
| RusaAi task queue and PostgresMemory | RusaAi | Worker runtime | Durable task/lease/checkpoint path exists; it is not a substitute for reusable governed knowledge |
| RusaAi KnowledgeService / PostgresKnowledgeStore | RusaAi | Worker Tool Gateway | Optional tenant/project-scoped RAG; activation flag defaults false |
| RusaAi migrations 011/012 | RusaAi | Knowledge service | Agent/project scope indexes and append-only document-version snapshots exist on main |
| RusaAi migration 013 governance proposal | RusaAi | Knowledge service / reviewers | PR #115 only; not merged |
| Model Server OpenAI-compatible API | RusaModels | RusaAi gateway | Authenticated Model Plane boundary; exact end-to-end release contract still needs reconciliation and verification |
| RusaAi Model Access Gateway | RusaAi | Tenant-scoped RusaAi workers; upstream RusaModels Model Server | API routes and CPU Compose profile exist; generic environment defaults gateway mode off, so production enforcement is NOT_VERIFIED |
| Model registry/manifests/artifacts | RusaModels | Model Server / Quant Server | Implemented infrastructure; production RusaAi-Edge checkpoint/artifact not selected or built |
| IM19 private source/review pipeline | RAG_Cerdas | Pilot operators/reviewers | Isolated pilot; live external providers and production service are not verified |
| Cataran event envelope snapshot | RAG_Cerdas PR #16 | Potential event consumers | Draft/provisional consumer snapshot; it does not establish the complete identity/task/approval/tool contract |

## 4. Knowledge-to-action responsibility chain

1. Trusted RusaAi identity establishes tenant and permitted project/repository scope.
2. RusaAi retrieves only approved, current and authorized knowledge through the canonical knowledge service.
3. Task context carries stable knowledge IDs and immutable versions, provenance and authorization evidence (required target; not yet fully represented in current worker contract).
4. Agent plans and decomposes the problem; each worker receives a least-privilege context package.
5. RusaAi Policy Engine and Tool Gateway decide whether an action is permitted; knowledge text never grants authority.
6. Worker executes only allowlisted operations in the configured sandbox/resource budget.
7. Test, security, regression and acceptance evidence is independently checked before task completion.
8. Task outcome may become a knowledge candidate linked to source/task/commit/test evidence; it is not published automatically.
9. Reviewer/approved validator decides publication, revision, deprecation or rejection under the knowledge lifecycle.

## 5. Roadmap dependency graph

The graph below follows the supplied ChatGPT-first roadmap v3.0. CHUNK 03 and CHUNK 04 branch after CHUNK 02 and rejoin at CHUNK 05. CHUNK 08 and CHUNK 09 branch after CHUNK 07 and rejoin at CHUNK 10.

```text
CHUNK 00 — Repository baseline
        |
        v
CHUNK 01 — Architecture and ADR reconciliation
        |
        v
CHUNK 02 — Identity, authorization and tenant security
        |
        +----------------------+
        |                      |
        v                      v
CHUNK 03 — Canonical       CHUNK 04 — Model
knowledge lifecycle       registry/artifacts
        |                      |
        +----------+-----------+
                   v
          CHUNK 05 — API contracts
                   |
                   v
          CHUNK 06 — Inference gateway
                   |
                   v
          CHUNK 07 — Runtime/hardware validation
                   |
              +----+----+
              |         |
              v         v
       CHUNK 08 — IM19  CHUNK 09 — Evaluation
       pilot hardening
              |         |
              +----+----+
                   v
          CHUNK 10 — Agent registry/DAG/tool governance
                   |
                   v
          CHUNK 11 — Reliability/DR/observability
                   |
                   v
          CHUNK 12 — Security supply chain/deployment hardening
                   |
                   v
          CHUNK 13 — Integrated staging E2E
                   |
                   v
          CHUNK 14 — Production certification/controlled rollout
                   |
                   v
          CHUNK 15 — Capacity/scale/corpus expansion
```

CHUNK 08 is a roadmap dependency, not an optional extra chunk. Its scope preserves IM19 as an isolated pilot and controlled knowledge boundary; it does not authorize cross-read/write or production data movement.

## 6. Current dependency blockers

- CHUNK 01: RusaAi PR #117 proposes cross-repository ownership, data-flow, trust-boundary and ADR reconciliation; RusaModels PR #24 proposes clarifying its internal registry/control store and now updates the FastAPI OpenAPI description; RAG_Cerdas PR #28 proposes aligning IM19 with the accepted RusaAi canonical-store decision. All remain draft/open, so mainline docs are still inconsistent. The RusaAi ADR-0002 numeric collision remains an explicit OPEN_DECISION. Related drafts: RusaAi #117, RusaModels #24, RAG_Cerdas #28.
- CHUNK 03: canonical knowledge lifecycle is incomplete on main; RusaAi PR #115 remains open.
- CHUNK 04/07: RusaAi-Edge checkpoint/tokenizer/artifact/target-device evidence is absent.
- CHUNK 05/06: cross-repository API contract and exact end-to-end compatibility evidence require reconciliation.
- CHUNK 08: IM19 rights, reviewer identity, purge/retention, tenant-scope and pilot-operational evidence remain incomplete.
- CHUNK 09: human-reviewed qrels and held-out retrieval/model/agent benchmark evidence are not available.
- CHUNK 10: PR #116 is a candidate-only staged registry/DAG implementation; do not build a duplicate runtime or activate candidate roles before CHUNK 08/09 and its own gates pass.

