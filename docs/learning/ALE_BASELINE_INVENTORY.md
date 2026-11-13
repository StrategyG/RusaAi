# RusaAI Adaptive Learning Engine — Baseline Inventory

**Document:** ALE-00 baseline inventory  
**Snapshot date:** 2026-11-14  
**Status:** PARTIAL — source discovery completed for the listed files; no local test execution or runtime deployment was performed.  
**Baseline:** `main` at the exact commits below  
**Execution branch:** `feat/ale-00-baseline-inventory-20261114`  
**Scope:** read-only discovery for ALE-00. This document does not authorize migrations, ingestion, training, production activation, or merging.

## 1. Repository baseline

| Repository | Main SHA | Open PRs | Main protected | CODEOWNERS observed |
|---|---|---:|---|---|
| [RusaAi](https://github.com/StrategyG/RusaAi) | `1adac8d340e5870570910168b6626fe285dd2cf2` | 82 | No (`protected=false`) | Not found in recursive main tree |
| [RusaModels](https://github.com/StrategyG/RusaModels) | `12c49b1372b230f3656be6e1badeabad870bcfa1` | 7 | No (`protected=false`) | `.github/CODEOWNERS` |
| [RAG_Cerdas](https://github.com/StrategyG/RAG_Cerdas) | `1ead461e76d563445d179e7d5e80c25a9ca9f059` | 2 | No (`protected=false`) | Not found in recursive main tree |

All three repositories are public, use `main` as the default branch, and are not archived. The connected GitHub identity reports push/admin permissions. This inventory deliberately does not change any main branch.

### CI evidence observed

| Repository | Workflow evidence | Commit | Result |
|---|---|---|---|
| RusaAi | [RUSA-AI CI #43354051026](https://github.com/StrategyG/RusaAi/actions/runs/43354051026) | `1adac8d3` | success |
| RusaAi | [RUSA AI Web CI #43354050892](https://github.com/StrategyG/RusaAi/actions/runs/43354050892) | `1adac8d3` | success |
| RusaModels | [RusaModels CI #43358929126](https://github.com/StrategyG/RusaModels/actions/runs/43358929126) | `12c49b13` | success |
| RAG_Cerdas | [Institutional RAG checks #43312458157](https://github.com/StrategyG/RAG_Cerdas/actions/runs/43312458157) | `1ead461e` | success |
| RAG_Cerdas | [Cataran Hourly Audit #43481726892](https://github.com/StrategyG/RAG_Cerdas/actions/runs/43481726892) | `1ead461e` | success |

These are GitHub Actions results for the listed exact commits. They do not certify deployment, data rights, model quality, or production operation. Combined commit-status lookup returned no status entries; workflow-run records are the evidence used above. No local test suite was run during this audit.

## 2. RusaAi — Control Plane baseline

### Existing implementation observed

- `rusa_agents/memory.py` contains `SQLiteMemory` and `PostgresMemory`.
- `rusa_agents/runtime.py` wires `PostgresMemory` into the worker runtime.
- `rusa_agents/orchestrator.py` persists task state and retrieves prior task state by tenant/task.
- Persisted task payload includes objective, acceptance criteria, worker results (role, summary, success, artifacts, evidence, blockers), and errors.
- `migrations/001_aicode_core.sql` creates tenant-keyed `aicode_memory`.
- `migrations/004_aicode_tenant_rls.sql` enables and forces RLS on the task memory table and scopes policies by transaction-local `rusa.tenant_id`.
- `rusa_knowledge` provides PostgreSQL/pgvector ingestion and retrieval; migration 007 provides 1,536-dimensional vectors, migration 009 provides a separate 384-dimensional vector space, migration 011 adds agent/project scope indexes, and migration 012 adds append-only document-version history.
- `rusa_knowledge/service.py` routes embedding calls through an OpenAI-compatible provider using the RusaModels Model Plane endpoint by default.
- `rusa_knowledge/evaluation.py` provides offline Recall@K, reciprocal-rank, and nDCG metrics.
- `rusa_agents/policy.py` provides an action allowlist/risk/approval decision mechanism.

### Correct interpretation

The existing memory is primarily **task checkpoint/state memory**, not yet the ALE persistent memory system described in the target architecture. The observed schema is keyed by tenant and task and stores repository, state, payload, and update time. It does not itself define the proposed episodic/semantic/procedural/preference memory classes, candidate approval lifecycle, provenance/consent envelope, expiry policy, contradiction handling, or deletion reconciliation across indexes and caches.

The existing AutonomyPolicy governs actions. It is not yet a complete learning-candidate governance engine with candidate states, reviewer separation, dataset eligibility, and model promotion gates.

The existing canonical knowledge store is a useful base, but RusaAi ADR-0002 explicitly states that the current mainline schema does not yet provide the complete source-rights, review, approval, publication, deletion, and agent/project authorization lifecycle. ADR-0002 selects `rusa_knowledge` as the canonical shared Agent/Worker store for implementation planning; it does **not** authorize production activation.

### Training-plane boundary finding

RusaAi main still contains:
- `rusa_training/dataset.py`
- `llm-project/train/train_sft.py`
- `.github/workflows/agent-sft-training.yml`
- `docs/FINETUNING_OPERATIONS.md`

RusaModels also contains a migrated SFT dataset validator, QLoRA trainer, evaluator, and manual GPU workflow. This creates duplicate training implementation surfaces. The target architecture assigns model training execution, model artifacts, and model release lifecycle to RusaModels. RusaAi should own training-data eligibility, approved export manifests, and orchestration—not a second canonical trainer. The RusaAi training implementation should be classified and reconciled in a dedicated ADR/PR before removal or deprecation; do not delete it during ALE-00.

## 3. RusaModels — Model Plane baseline

### Existing implementation observed

- Model registry and artifact governance exist in `rusamodels/manifests.py` and the model manifest schema.
- The strict immutable Pydantic manifest records model identity/version/type, architecture, tokenizer ID/version, source repository/revision/license/usage restrictions, optional training dataset release/config digest/code revision, artifact format/size/SHA-256, and optional evaluation report ID.
- `rusamodels/training/dataset.py` validates agent roles, response envelopes, tool allowlists, protected paths, duplicate IDs/content, train/eval overlap, and blocked/unblocked evaluation distribution.
- `rusamodels/training/train_sft.py` implements QLoRA SFT and requires a CUDA GPU.
- `rusamodels/evaluation/evaluate_agent_sft.py` evaluates structured agent response contracts and blocked-action behavior.
- `docs/TRAINING.md` describes the training path and promotion requirements.

### Dataset and training evidence

The committed `data/agent_sft/train.jsonl` has 120 examples (20 for each of six roles: analyst, architect, developer, debugger, tester, security). The held-out `eval.jsonl` has 36 examples (6 per role). The training split includes 24 blocked examples; the evaluation split includes 12 blocked examples.

`data/agent_sft/PROVENANCE.md` describes this as a synthetic seed corpus. `docs/TRAINING.md` and the training configuration state that QLoRA has not been executed and no trained RusaModels weights are included. Dataset availability and successful dataset validation are not evidence that a model has learned or improved.

### ALE gaps

- Training examples use a strict runtime envelope, but the inspected record contract does not itself carry a complete source-rights/consent approval chain.
- The model manifest records tokenizer ID/version but has no explicit chat-template digest/identity field in the inspected schema. The trainer does require a tokenizer chat template; release lineage should bind the exact template.
- The dataset seed is synthetic and should not be used to claim domain quality or production readiness.
- The training pipeline is not an online continual-learning path. It is an offline CUDA training process, which is the correct basis for controlled model adaptation.

## 4. RAG_Cerdas / IM19 — Knowledge Intelligence Pilot baseline

### Existing implementation observed

- IM19 separates source discovery, exact-revision/source-rights checks, document processing, OCR, knowledge mining, reviewer workflow, provenance, embeddings, publication checks, and retrieval.
- Source-rights decisions are bound to exact source revision and content hash.
- Reviewer identity is bound to a registered PostgreSQL LOGIN principal in the newer reviewer identity migrations.
- The evaluator requires explicitly human-reviewed labels, a non-empty relevant chunk set, and a review attestation envelope.
- The IM19 runbook identifies Colab as a development/batch environment and says the example worker deployment has not been built/deployed or endurance-tested.

### Evaluation status

The current `eval_cases.json` contains three cases marked `needs_human_review_after_page_aware_ingestion`. They do not constitute human-reviewed relevance judgments. The evaluator therefore must not report retrieval quality as measured until the real reviewer registry, attestation, source/corpus identity, and non-empty relevance labels are verified.

### Security and lifecycle boundary

The IM19 documentation explicitly states that the current private worker/retriever is not a multi-tenant public API and that tenant/project isolation is not established. Rights withdrawal quarantines a version from retrieval but does not erase stored text, embeddings, or downloaded blobs. A reviewed purge/retention workflow remains unimplemented. IM19 must remain isolated from `rusa_knowledge`; no direct database sharing, cross-read, cross-write, or dual-write is authorized by this baseline.

## 5. Open PR backlog — discovery snapshot

| Repository | Open PR count | ALE-relevant open work |
|---|---:|---|
| RusaAi | 82 | [#117 ADR reconciliation](https://github.com/StrategyG/RusaAi/pull/117), [#116 Agent Runtime/DAG executor](https://github.com/StrategyG/RusaAi/pull/116), [#115 knowledge rights/publication gates](https://github.com/StrategyG/RusaAi/pull/115), [#82 RAG scope/version history](https://github.com/StrategyG/RusaAi/pull/82), [#109 inference admission](https://github.com/StrategyG/RusaAi/pull/109), [#110 worker gateway routing](https://github.com/StrategyG/RusaAi/pull/110), [#103 Model Plane API contract](https://github.com/StrategyG/RusaAi/pull/103), [#106 signed release/deploy pipeline](https://github.com/StrategyG/RusaAi/pull/106) |
| RusaModels | 7 | [#24 Model Plane ownership](https://github.com/StrategyG/RusaModels/pull/24), [#20 architecture/tokenizer engine](https://github.com/StrategyG/RusaModels/pull/20), [#18 benchmark engine](https://github.com/StrategyG/RusaModels/pull/18), [#17 dataset engineering](https://github.com/StrategyG/RusaModels/pull/17), [#14 CPU Tiny trainer](https://github.com/StrategyG/RusaModels/pull/14) |
| RAG_Cerdas | 2 | [#28 IM19 alignment with RusaAi canonical knowledge decision](https://github.com/StrategyG/RAG_Cerdas/pull/28), [#16 event-envelope contract draft](https://github.com/StrategyG/RAG_Cerdas/pull/16) |

This is a backlog discovery and relevance shortlist, not a full code review of all 91 open PRs. PR-only implementation must not be counted as mainline capability. PR disposition (merge, close, supersede, or defer) requires per-PR diff, dependency, migration, and CI review.

## 6. Baseline conclusions

1. RusaAi has task-state memory and a canonical tenant-scoped knowledge store, but not yet the full ALE memory/experience/candidate lifecycle.
2. RusaAi and RusaModels both retain SFT-related code. The Model Plane boundary must be made operationally unambiguous.
3. RusaModels has a validated synthetic SFT seed and offline QLoRA pipeline, but no trained model artifact is evidenced.
4. IM19 has stronger source-rights/reviewer governance than the current canonical RusaAi knowledge schema, but remains isolated and is not tenant-ready or production-deployed.
5. The most important ALE work is governance, trusted data flow, and end-to-end evidence—not adding another training script.
6. The main branches report `protected=false`; RusaAi and RAG_Cerdas have no CODEOWNERS file in their main trees. Branch protection and owner review are therefore P0 governance work.
7. The large RusaAi PR backlog is a material integration risk.

## 7. ALE-00 acceptance disposition

| Criterion | Status | Evidence / reason |
|---|---|---|
| Exact main SHAs recorded | PASS | Repository metadata and main branch commit objects |
| Open PR counts recorded | PASS | GitHub PR search |
| Relevant learning components inventoried | PASS | Source inspection listed above |
| Training ownership conflict identified | PASS | RusaAi and RusaModels both contain SFT code |
| Knowledge/IM19 boundary recorded | PASS | RusaAi ADR-0002 and IM19 docs |
| CI runs identified for baseline SHAs | PASS | Exact commit workflow run records |
| Full 91-PR disposition audit | NOT_VERIFIED | Only ALE-relevant shortlist inspected |
| Local unit/integration tests executed | NOT_RUN | No repository clone/runtime execution in this audit |
| Branch protection and required-check policy fully audited | PARTIAL | Main reports unprotected; detailed ruleset and required-check policy not fully reconciled |
| Database runtime state, actual migrations, deployed model artifacts | NOT_VERIFIED | No live database/object store/runtime access |

**ALE-00 status: PARTIAL.** Do not mark this chunk PASS until the remaining PR inventory and repository governance review are completed and the master CHUNK 00 baseline is accepted.

## 8. Next dependency

Proceed to ALE-01 only after the project owner accepts this baseline and the master CHUNK 00 is closed. ALE-01 must reconcile the duplicate ADR-0002 identifiers, confirm the RusaAi/RusaModels training boundary, and define the versioned learning contracts. No code or schema migration should precede that decision.
