# Rusa AI Ecosystem — Architecture Decisions Index

**Initial snapshot:** 2026-11-14  
**Latest refresh:** 2026-11-16  
**Purpose:** cross-repository decision index for the roadmap v3.0. This index records current repository decisions and open alignment work; it does not silently supersede repository ADRs.

## 1. Decisions observed on current main

| Repository | Record | Status observed | Decision / scope | Current issue |
|---|---|---|---|---|
| RusaAi | `docs/adr/ADR-0001-rusaai-edge-v2-cpu-first-baseline.md` | Accepted for repository alignment; full architecture remains proposed | CPU-first model target, admission limits and deployment ownership | Target model checkpoint/artifact is still design-only |
| RusaAi | `docs/adr/ADR-0002-canonical-shared-agent-worker-knowledge-store.md` | Accepted by project owner for implementation planning | Selects RusaAi `rusa_knowledge` as canonical shared Agent/Worker knowledge store | Production activation is not authorized; governance lifecycle remains incomplete |
| RusaAi | `docs/adr/ADR-0002-rusamodels-model-plane-boundary.md` | Accepted for repository integration alignment | RusaAi owns Control Plane; RusaModels owns Model Plane | Numeric identifier collides with the canonical-store ADR |
| RusaModels | `docs/ADR-001-shared-control-plane.md` | Accepted | Model Server and Quant Server share RusaModels' registry/control database and S3-compatible artifact store | PR #24 clarifies that this is internal to RusaModels, not the RusaAi ecosystem Control Plane |
| RAG_Cerdas | `docs/ADR_0001_CANONICAL_KNOWLEDGE_STORE.md` | Historical comparison; superseded by accepted ADR-0002 | Retains the original local SQLite/FTS5 and isolated-store analysis as historical context | The accepted ADR-0002 alignment merged in PR #28 on 2026-11-16; IM19 remains isolated |
| RAG_Cerdas | `docs/ADR_0002_RUSAAI_CANONICAL_KNOWLEDGE_STORE_ALIGNMENT.md` | Accepted repository alignment; merged in PR #28 on 2026-11-16 | Aligns IM19 as an isolated pilot and prohibits implicit data movement/public multi-tenant exposure | Production activation remains NOT APPROVED |
| RusaAi + RusaModels | `documentation/adr/ADR-TRAIN-001-foundation-checkpoint.md` | RusaAi: PILOT_ONLY; RusaModels: DRAFT — no foundation checkpoint approved | Bhineka-GPT is a research/pilot candidate only; architecture reference and product identity remain distinct; approved runtime model ID is pending | Semantics are compatible; no production checkpoint, immutable revision, artifact digest or target-device evidence is approved |

## 2. Historical CHUNK 01 alignment PRs (all merged 2026-11-16)

| Repository | PR | Status | Purpose | Current disposition |
|---|---|---|---|---|
| RusaAi | [#117](https://github.com/StrategyG/RusaAi/pull/117) | Merged | Adds ADR index, records duplicate ADR-0002 collision and updates Model Plane/canonical-store references | Merged at `6485a38c`; numeric ADR collision remains an explicit open decision |
| RusaModels | [#24](https://github.com/StrategyG/RusaModels/pull/24) | Merged | Clarifies that the shared PostgreSQL registry store is internal to RusaModels Model Server and Quant Server | Merged at `926f557c`; README now links to the mainline reconciliation document |
| RAG_Cerdas | [#28](https://github.com/StrategyG/RAG_Cerdas/pull/28) | Merged | Adds the accepted alignment ADR and marks the old canonical-store proposal historical for ecosystem selection | Merged at `2966572b`; preserve IM19 as an isolated pilot |

All three alignment PRs are merged. Their post-merge documentation closeout PRs RusaAi #124, RusaModels #27 and RAG_Cerdas #31 are also merged. Exact-head CI and merge commits are recorded in `EVIDENCE_LEDGER.md`. No ADR identifier was silently renumbered.

## 3. Open architecture decisions

1. **ADR numbering collision:** decide a reviewed, stable identifier strategy for the two RusaAi ADR-0002 files. Preserve historical references and update links atomically in a dedicated documentation PR.
2. **Embedding profile:** select and pin production model identity, immutable artifact digest, preprocessing/input types, dimensions and index-generation migration. 384d and 1536d spaces are not interchangeable merely because both are vectors.
3. **Knowledge governance:** review and complete source identity/version, rights, review, human approval, publication, withdrawal, purge, retention, index/cache/backup reconciliation and tenant erasure.
4. **Inference and embedding gateway enforcement:** the CPU Compose profile routes chat completion through the RusaAi API gateway and clears direct chat Model Plane credentials, but generic environment defaults and optional Worker RAG previously allowed direct Model Plane paths. CHUNK 02 production Worker fail-closed guard merged in RusaAi PR #125; post-merge image build/scan is still pending, and MCP remains separately gated.
5. **Foundation checkpoint:** RusaAi and RusaModels ADR-TRAIN-001 records are compatible only when read as product/research status versus runtime approval: Bhineka-GPT remains PILOT_ONLY/PENDING_AUDIT, the architecture reference is not an available checkpoint, and the approved runtime model ID remains pending. RusaAi owns product selection/approval; RusaModels owns registry, artifact integrity and runtime release.
6. **Governance enforcement:** RAG_Cerdas ruleset metadata reports 10 approvals and no bypass actors, while GitHub accepted PR #28 and #31 without APPROVED reviews and no ruleset change. The enforcement path requires owner investigation.
7. **Production activation:** no ADR in this index authorizes production migration, ingestion, model release or deployment without the separate release evidence and owner approval.

## 4. Binding ownership rule for subsequent chunks

- RusaAi is the canonical authority for shared Agent/Worker knowledge and Control Plane decisions.
- RusaModels owns model registry, artifacts, inference and embedding runtime; it does not own knowledge rights or publication.
- RAG_Cerdas / IM19 remains an isolated candidate-source and Knowledge Intelligence pilot unless a later approved ADR and versioned interface explicitly change that boundary.
- Knowledge may inform an agent decision but cannot grant tool permissions, override system policy, bypass tenant isolation or replace human approval.
- Model-generated or worker-generated knowledge remains candidate data until provenance, rights, validation and publication gates pass.
