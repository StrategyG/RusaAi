# RUSA AI Ecosystem — Open Risks

**Snapshot:** 2026-11-14  
**Source of truth:** current main branches and the open PR metadata recorded in `BASELINE_INVENTORY.md`.  
**Status vocabulary:** OPEN, PARTIAL, BLOCKED, MITIGATION_PROPOSED.

| ID | Severity | Owning repository | Risk / evidence | Required resolution |
|---|---|---|---|---|
| RSK-001 | Critical | All three | GitHub reports `main` branch protection disabled for RusaAi, RusaModels and RAG_Cerdas; no required checks are configured on those branches. | Enable reviewed branch rulesets, required CI checks, PR review requirements, force-push restrictions and appropriate CODEOWNERS before production governance is claimed. |
| RSK-002 | High | RusaAi + RAG_Cerdas | RusaAi main has an accepted canonical shared knowledge-store ADR selecting `rusa_knowledge`; RAG_Cerdas main ADR-0001 still states that no canonical store is selected. RAG_Cerdas PR #28 proposes alignment but remains draft/open. | Complete CHUNK 01 cross-repository ADR reconciliation. Keep IM19 isolated; do not cross-read, cross-write, dual-write, mirror or bulk-sync. |
| RSK-003 | High | RusaAi | Two current files use identifier ADR-0002: canonical shared Agent/Worker knowledge store and RusaModels Model Plane boundary. PR #117 documents the collision but does not renumber the records. | Preserve decision history; approve a controlled ADR numbering correction and update references only through a reviewed PR. |
| RSK-004 | Critical | RusaAi | Mainline migrations 011/012 provide agent/project scope indexes and append-only document-version snapshots, but the complete rights, source review, human approval, publication, withdrawal, purge and retention lifecycle is not fully evidenced on main. PR #115 adds migration 013 governance but is draft/open. | Review and test the governance implementation, source registry and byte snapshots, signed manifests, index-generation reconciliation, cache/vector/backup tombstones, tenant erasure and retention. |
| RSK-005 | High | RusaAi | Current task memory persists task/checkpoint state and WorkerResult has summary, success, artifacts, evidence and blockers; there is no dedicated typed immutable knowledge-reference/version list in the inspected contract. | Extend the task/worker evidence contract so each material decision and output records knowledge IDs, immutable versions, provenance, scope, authorization decision and source references. |
| RSK-006 | High | RusaAi | KnowledgeService is optional in the worker runtime and `RUSA_RAG_ENABLED` defaults to false. | Define and test the approved runtime activation profile, fail-closed behavior, tenant/project identity derivation and production configuration. Do not enable production merely by changing the flag. |
| RSK-007 | Critical | RusaAi + RusaModels | RusaAi-Edge v2.0 remains design-only: non-Qwen checkpoint and tokenizer are not selected, GGUF artifact is not built, artifact digest and target-device benchmark are absent. | Complete checkpoint/license/architecture/tokenizer audit, immutable model manifest, artifact build/checksum, held-out quality/safety evaluation and CPU target-hardware benchmark. |
| RSK-008 | High | RAG_Cerdas | IM19 is an isolated pilot. Current docs state that live Dropbox/Supabase/Qwen/E5 access and deployed autonomous worker operation are blocked; no multi-tenant production service boundary is approved. | Keep isolated. If IM19 is later selected as a source path, approve an ADR and versioned interface, prove rights and tenant/project isolation, and pass CHUNK 08 before it becomes an active dependency. |
| RSK-009 | Critical | RusaAi + RAG_Cerdas + RusaModels | Retrieval quality and knowledge-to-action quality are not supported by a current human-reviewed held-out corpus/benchmark with exact versions and reproducible outcomes. RAG_Cerdas README says evaluation cases are unlabelled; no end-to-end system-building benchmark is evidenced. | Establish CHUNK 09 frozen datasets, human-reviewed qrels, provenance, predeclared thresholds and separate retrieval, diagnosis, system-building, worker reliability and safety metrics. |
| RSK-010 | Critical | All three | No staging/production deployment, target-hardware soak, independent tenant-isolation review, backup/restore drill, rollback rehearsal, incident alerting or owner release sign-off was evidenced in this audit. | Produce exact-release evidence from the intended target environment; keep production NO-GO until all applicable gates pass. |
| RSK-011 | High | RusaAi + RusaModels + RAG_Cerdas | 92 open PRs were returned (RusaAi 83, RusaModels 7, RAG_Cerdas 2). Several material PRs are stacked on feature branches or old main SHAs; successful head CI does not prove current-base compatibility. | Triage duplicate/obsolete PRs, compare each head with current main, rebase/retarget deliberately, rerun exact-head CI and close superseded work. Do not merge by title or historical CI alone. |
| RSK-012 | Medium | RusaAi | `docs/PRODUCTION_READINESS.md` describes migrations 010/011/012 as open-PR work although the current main tree contains those migrations. | Reconcile readiness documentation against current main, exact migration history and actual deployment evidence. Do not equate file presence with migration application. |
| RSK-013 | High | RusaAi + RusaModels | RusaAi supports separate 384- and 1536-dimensional vector spaces. The production embedding model, immutable artifact/version, preprocessing and index-generation migration are not selected/evidenced as one coherent release profile. | Pin exact model identity/digest, dimensions, query/passage behavior and index generation; run compatibility, retrieval quality, rollback and capacity tests. |
| RSK-014 | Medium | Project execution | A local clone and working tree were not available through this audit. Remote GitHub main/PR state is verified; local uncommitted changes cannot be ruled out. | Before local implementation or patch generation, acquire the intended checkout and record clean/dirty state and exact base SHA. |

## Immediate release posture

- **Production:** NO-GO / NOT_VERIFIED.
- **Autonomous agent/worker expansion:** restricted to existing bounded, allowlisted and approval-gated capabilities.
- **Knowledge publication:** no automatic publication of model/worker-generated content.
- **IM19:** isolated pilot only; no cross-system synchronization.
- **Database/deployment:** no production migration, ingestion, destructive operation or deployment is authorized by this baseline.

## Priority order

1. Resolve CHUNK 01 architecture and ADR inconsistencies.
2. Establish CHUNK 02 identity/security and branch governance.
3. Complete canonical knowledge lifecycle in CHUNK 03 and model lifecycle in CHUNK 04.
4. Complete versioned API contracts and inference/runtime prerequisites (CHUNK 05–07).
5. Complete evaluation (CHUNK 09), then Agent/Worker Knowledge-to-Action (CHUNK 16).
6. Only then complete broad agent runtime (CHUNK 10), reliability, supply chain, staging and production gates (CHUNK 11–14).
