# Rusa AI Ecosystem — Architecture Decisions Index

**Snapshot:** 2026-11-14  
**Purpose:** cross-repository decision index for the roadmap v3.0. This index records current repository decisions and open alignment work; it does not silently supersede repository ADRs.

## 1. Decisions observed on current main

| Repository | Record | Status observed | Decision / scope | Current issue |
|---|---|---|---|---|
| RusaAi | `docs/adr/ADR-0001-rusaai-edge-v2-cpu-first-baseline.md` | Accepted for repository alignment; full architecture remains proposed | CPU-first model target, admission limits and deployment ownership | Target model checkpoint/artifact is still design-only |
| RusaAi | `docs/adr/ADR-0002-canonical-shared-agent-worker-knowledge-store.md` | Accepted by project owner for implementation planning | Selects RusaAi `rusa_knowledge` as canonical shared Agent/Worker knowledge store | Production activation is not authorized; governance lifecycle remains incomplete |
| RusaAi | `docs/adr/ADR-0002-rusamodels-model-plane-boundary.md` | Accepted for repository integration alignment | RusaAi owns Control Plane; RusaModels owns Model Plane | Numeric identifier collides with the canonical-store ADR |
| RusaModels | `docs/ADR-001-shared-control-plane.md` | Accepted | Model Server and Quant Server share RusaModels' registry/control database and S3-compatible artifact store | “Shared control plane” wording can be confused with the RusaAi ecosystem Control Plane |
| RAG_Cerdas | `docs/ADR_0001_CANONICAL_KNOWLEDGE_STORE.md` | Proposed — no canonical-store decision approved | Keeps local SQLite/FTS5 and the RusaAi/RAG_Cerdas stores isolated pending selection | Conflicts with the later accepted RusaAi canonical-store decision; PR #28 proposes an alignment ADR |
| RusaAi + RusaModels | `documentation/adr/ADR-TRAIN-001-foundation-checkpoint.md` | RusaAi: PILOT_ONLY; RusaModels: DRAFT — no foundation checkpoint approved | Bhineka-GPT is a research/pilot candidate only; architecture reference and product identity remain distinct; approved runtime model ID is pending | Semantics are compatible; no production checkpoint, immutable revision, artifact digest or target-device evidence is approved |

## 2. Relevant open alignment PRs

| Repository | PR | Status | Purpose | Current disposition |
|---|---|---|---|---|
| RusaAi | [#117](https://github.com/StrategyG/RusaAi/pull/117) | Draft/open | Adds ADR index, records duplicate ADR-0002 collision and updates Model Plane/canonical-store references | Review; it deliberately does not renumber the records |
| RusaModels | [#24](https://github.com/StrategyG/RusaModels/pull/24) | Draft/open | Clarifies that the shared PostgreSQL registry store is internal to RusaModels Model Server and Quant Server | Review terminology and links |
| RAG_Cerdas | [#28](https://github.com/StrategyG/RAG_Cerdas/pull/28) | Draft/open | Adds a superseding alignment ADR and marks the old canonical-store proposal superseded for ecosystem selection | Review; preserve IM19 as an isolated pilot |

RusaModels PR #24 (head `ea4461d5`) has 2/2 checks passing and RAG_Cerdas PR #28 (head `65752d94`) has 1/1 check passing. RusaAi PR #117 has a new documentation update under CI at head `e377c268`; its checks were still running at the latest inspection. All three PRs remain open/draft and must not be treated as accepted or merged. Re-check exact heads before any merge.

## 3. Open architecture decisions

1. **ADR numbering collision:** decide a reviewed, stable identifier strategy for the two RusaAi ADR-0002 files. Preserve historical references and update links atomically in a dedicated documentation PR.
2. **Cross-repository canonical-store alignment:** align RAG_Cerdas docs with the RusaAi decision while explicitly preserving IM19's isolated pilot boundary and prohibiting cross-read/write, dual-write, mirroring or bulk synchronization.
3. **RusaModels terminology:** consistently distinguish the RusaAi ecosystem Control Plane from the internal RusaModels registry/control database.
4. **Embedding profile:** select and pin production model identity, immutable artifact digest, preprocessing/input types, dimensions and index-generation migration. 384d and 1536d spaces are not interchangeable merely because both are vectors.
5. **Knowledge governance:** review and complete source identity/version, rights, review, human approval, publication, withdrawal, purge, retention, index/cache/backup reconciliation and tenant erasure.
6. **Inference gateway enforcement:** the CPU Compose profile routes workers through the RusaAi API gateway and clears direct Model Plane credentials, but the generic environment defaults gateway mode off and supports direct worker-to-RusaModels fallback. Production must fail closed on the gateway-only path; this remains a CHUNK 02/06 verification item.
7. **Foundation checkpoint:** RusaAi and RusaModels ADR-TRAIN-001 records are compatible only when read as product/research status versus runtime approval: Bhineka-GPT remains PILOT_ONLY/PENDING_AUDIT, the architecture reference is not an available checkpoint, and the approved runtime model ID remains pending. RusaAi owns product selection/approval; RusaModels owns registry, artifact integrity and runtime release.
8. **Production activation:** no ADR in this index authorizes production migration, ingestion, model release or deployment without the separate release evidence and owner approval.

## 4. Binding ownership rule for subsequent chunks

- RusaAi is the canonical authority for shared Agent/Worker knowledge and Control Plane decisions.
- RusaModels owns model registry, artifacts, inference and embedding runtime; it does not own knowledge rights or publication.
- RAG_Cerdas / IM19 remains an isolated candidate-source and Knowledge Intelligence pilot unless a later approved ADR and versioned interface explicitly change that boundary.
- Knowledge may inform an agent decision but cannot grant tool permissions, override system policy, bypass tenant isolation or replace human approval.
- Model-generated or worker-generated knowledge remains candidate data until provenance, rights, validation and publication gates pass.
