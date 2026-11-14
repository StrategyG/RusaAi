# RUSA AI Ecosystem — Baseline Inventory

**Snapshot:** 2026-11-14  
**Roadmap:** RUSA AI Ecosystem ChatGPT-First Engineering Roadmap v3.0  
**Audit type:** read-only repository discovery and evidence inventory  
**Coordinator repository:** `StrategyG/RusaAi`  
**Scope:** `StrategyG/RusaAi`, `StrategyG/RusaModels`, `StrategyG/RAG_Cerdas`

## 1. Executive baseline

All three repositories are reachable through the connected GitHub integration. The connected identity reports administrative and push permissions. No source-code, database, model-artifact, deployment, or production-data changes were made during this audit.

| Repository | Default branch | Verified main SHA | Branch protected | Open PRs returned |
|---|---|---|---:|---:|
| [RusaAi](https://github.com/StrategyG/RusaAi) | `main` | `1adac8d340e5870570910168b6626fe285dd2cf2` | No | 85 |
| [RusaModels](https://github.com/StrategyG/RusaModels) | `main` | `12c49b1372b230f3656be6e1badeabad870bcfa1` | No | 7 |
| [RAG_Cerdas](https://github.com/StrategyG/RAG_Cerdas) | `main` | `1ead461e76d563445d179e7d5e80c25a9ca9f059` | No | 2 |
| **Total** | | | | **94** |

The initial RusaAi repository-scoped search returned 83 open PRs. A follow-up search on 2026-11-14 returned 85 after PRs #119 and #120 were opened; RusaModels remained at 7 and RAG_Cerdas at 2 (94 total). PR #118's own ALE baseline records 82. These are time-stamped snapshots, not contradictory immutable counts.

### Exact-main CI evidence

| Repository | Check runs on exact main SHA | Result | Evidence |
|---|---:|---|---|
| RusaAi | 4 | All completed successfully | [CI run 43354051026](https://github.com/StrategyG/RusaAi/actions/runs/43354051026), [Web CI run 43354050892](https://github.com/StrategyG/RusaAi/actions/runs/43354050892) |
| RusaModels | 2 | All completed successfully | [CI run 43358929126](https://github.com/StrategyG/RusaModels/actions/runs/43358929126) |
| RAG_Cerdas | 5 | All completed successfully | [RAG tests 43312458157](https://github.com/StrategyG/RAG_Cerdas/actions/runs/43312458157), [hourly audit 43481726892](https://github.com/StrategyG/RAG_Cerdas/actions/runs/43481726892) |

The legacy combined commit-status endpoint returned no status entries (and reported `pending`). The separate GitHub Checks API returned the completed successful check runs listed above. These results validate only the exact commits and checks shown; they do not prove staging, production, external-provider, model-quality, or operational readiness.

## 2. Repository inventory

Counts below are from the recursive Git tree for the recorded main SHA. “Tree entries” includes both blobs and directories. Test and migration counts are path-pattern inventories, not executed test counts.

| Repository | Tree entries | Test files | Migration files | GitHub workflows |
|---|---:|---:|---:|---:|
| RusaAi | 286 | 43 | 12 | 4 |
| RusaModels | 183 | 23 | 7 | 3 |
| RAG_Cerdas | 243 | 26 | 9 | 3 |

### Repository instructions and ownership controls

| Repository | Instructions found | CODEOWNERS | Main branch protection |
|---|---|---|---|
| RusaAi | `.github/copilot-instructions.md` | Not found in recursive main tree | Disabled |
| RusaModels | No `AGENTS.md`, `CONTRIBUTING*`, or Copilot instruction file found in inspected tree | `.github/CODEOWNERS` assigns repository and sensitive paths to `@StrategyG` | Disabled |
| RAG_Cerdas | `.github/copilot-instructions.md` | Not found in recursive main tree | Disabled |

No `AGENTS.md` or `CONTRIBUTING*` file was found in the inspected recursive trees. RusaAi and RAG_Cerdas have repository-specific Copilot instructions. RusaModels has a CODEOWNERS policy but no equivalent instruction file was found in the inspected tree.

### RusaAi — Control Plane

**Mainline structure observed:** `rusa_agents`, `rusa_knowledge`, `rusa_mcp`, `rusa_quant`, `rusa_training`, `llm-project`, `shared/contracts`, `shared/schemas`, `migrations`, `schemas`, `scripts`, `tests`, `web`, Dockerfiles and multiple Compose profiles.

**Relevant source inspection:**

- `rusa_agents/orchestrator.py`: fixed six-role pipeline (analyst, architect, developer, debugger, tester, security), manager hierarchy, bounded parallelism and evidence-gated repair.
- `rusa_agents/autonomous.py`: structured JSON tool loop; default maximum 12 steps and 8 actions per step; role-scoped tool allowlists.
- `rusa_agents/agent_tools.py`: deterministic role-to-tool and role-to-check profiles.
- `rusa_agents/tool_gateway.py`: named workspace operations and predefined Docker validation profiles; sandbox is network-disabled, read-only, non-root and resource-bounded.
- `rusa_agents/runtime.py`: production worker uses PostgreSQL task queue, PostgreSQL task memory, audit, workspace manager, sandbox and optional KnowledgeService. Knowledge activation is controlled by `RUSA_RAG_ENABLED`; the code default is false.
- `rusa_agents/memory.py`: SQLite/PostgreSQL task-state persistence. The stored task payload includes objective, acceptance criteria, worker summaries, evidence, blockers and errors. This is task/checkpoint memory, not by itself a complete reusable knowledge lifecycle.
- `rusa_agents/contracts.py`: `CodingTask` and `WorkerResult` contracts. The current WorkerResult shape has summary, success, artifacts, evidence and blockers; it does not expose a dedicated typed list of immutable knowledge IDs/versions.
- `rusa_knowledge/service.py` and `store.py`: tenant-scoped PostgreSQL/pgvector ingestion and retrieval; supported vector dimensions are 384 and 1536; agent/project scope validation exists.
- `migrations/011_rag_agent_project_scope.sql`: agent/project scope lookup indexes.
- `migrations/012_rag_knowledge_version_history.sql`: append-only document-version and version-chunk snapshots with tenant RLS.
- `docs/adr/ADR-0002-canonical-shared-agent-worker-knowledge-store.md`: selects RusaAi `rusa_knowledge` as the canonical shared Agent/Worker knowledge store for implementation planning; production activation remains unauthorized.
- `docs/adr/ADR-0002-rusamodels-model-plane-boundary.md`: RusaAi Control Plane / RusaModels Model Plane boundary.

**Production boundary:** RusaAi README and `docs/PRODUCTION_READINESS.md` explicitly do not claim production certification. The non-Qwen RusaAi-Edge checkpoint and tokenizer are not selected; weights/GGUF and target-hardware benchmark are not evidenced. Knowledge governance, retrieval quality, deployment, security and operational release gates remain open.

**Documentation drift observed:** `docs/PRODUCTION_READINESS.md` still describes migrations 010, 011 and 012 as open-PR work, while the current main tree contains all three migration files and current main commit text says it integrates inference admission and scoped RAG. Reconcile this document against the exact mainline state during CHUNK 01; do not infer that every deployment gate has passed merely because the migrations are present.

### RusaModels — Model Plane

**Mainline structure observed:** `rusamodels`, `services`, `configs`, `models`, `schemas`, `scripts`, `deploy`, `migrations`, `tests`, Dockerfiles, Compose profiles and CI workflows.

**README and architecture findings:**

- Model Server owns model registry/lifecycle and authenticated OpenAI-compatible inference.
- Quant Server and Quant Worker own conversion/quantization workflows and immutable artifact registration.
- PostgreSQL/S3 are the shared RusaModels registry/artifact services in standard deployments; RusaAi is an authenticated client and must not access the database or object store directly.
- Development/edge SQLite and local files are distinct from standard shared deployment services.
- The CPU Codespaces runtime is a development profile, not proof of target-device production capacity.
- The RusaAi-Edge v2.0 non-Qwen checkpoint, tokenizer/chat template and Q4_K_M GGUF artifact remain unselected/unbuilt; no target-device benchmark or production artifact digest is evidenced.
- Dynamic model loading/deployment, multi-model routing, scheduling, automated training orchestration and dashboard are identified as not yet implemented in the current README.
- `.github/CODEOWNERS` assigns repository and sensitive deployment/security paths to `@StrategyG`.

### RAG_Cerdas — IM19 Knowledge Intelligence pilot

**Mainline structure observed:** `im19`, `agent`, `scripts`, `supabase`, `docs`, `tests`, Dropbox/knowledge extraction and evaluation workflows.

**README, instructions and validation findings:**

- IM19 is explicitly labelled a pilot and not production-ready.
- The repository contains versioned source discovery/processing, PDF/EPUB extraction, selective OCR, structured knowledge mining, reviewer roles, private PostgreSQL/pgvector storage, worker jobs and citation-aware retrieval.
- Source-rights decisions and human approvals are separate gates. A later hold/reject can quarantine published content, but the runbook says quarantine is not deletion; purge/retention remains a release blocker.
- The README says current evaluation cases are unlabelled; metrics require reviewed cases with accountable reviewers.
- The validation report records live Dropbox/Supabase/Qwen/E5 access as blocked and says no autonomous worker is deployed.
- The current IM19 ADR on main still says no canonical shared store has been selected. This conflicts with the accepted RusaAi canonical-store ADR; RAG_Cerdas PR #28 proposes alignment but is not merged.
- The repository Copilot instructions prohibit direct main changes, production writes, migration execution, Dropbox writes, public deployment, and paid external calls without explicit human approval.

## 3. Relevant open pull requests

The following are the open PRs identified as materially relevant to architecture, knowledge governance, agent/worker execution, model lifecycle, contracts, or release gates. “CI verified” means the exact PR head SHA had completed successful GitHub check runs when inspected. A passing PR check does not make an old base branch current or make a PR merged.

### RusaAi

| PR | Status | Base → head | Head SHA (short) | CI at inspected head | Relevance |
|---|---|---|---|---|---|
| [#118](https://github.com/StrategyG/RusaAi/pull/118) | Draft | `main` → `feat/ale-00-baseline-inventory-20261114` | `202a1c16` | 3 checks pass | Separate ALE baseline inventory; not merged |
| [#119](https://github.com/StrategyG/RusaAi/pull/119) | Draft | `main` → `docs/roadmap-chunk-00-baseline-20261114` | `fded768e` | 2/3 complete PASS; image build/scan in progress | Roadmap v3.0 baseline, risks, dependency map, evidence and execution state; documentation only | Roadmap v3.0 baseline, risks, dependency map, evidence and execution state; documentation only |
| [#120](https://github.com/StrategyG/RusaAi/pull/120) | Draft | `main` → `docs/precision-agent-worker-chunk-framework-2026-11-14` | `ba04a053` | 3/3 pass on attempt 2; attempt 1 failed one FIFO-ordering test | Agent/Worker chunk framework, PR #116 reconciliation and five-million knowledge chunk program; documentation only |
| [#117](https://github.com/StrategyG/RusaAi/pull/117) | Draft | `main` → `docs/chunk-01-architecture-adr-reconciliation-20261113` | `251a7d1f` | 2/3 complete PASS; image build/scan in progress; current test suite 337 passed / 6 skipped | ADR index, migration-state correction, ecosystem architecture, duplicate ADR-0002 disclosure |
| [#116](https://github.com/StrategyG/RusaAi/pull/116) | Draft | `main` → `feature/agent-runtime-v1` | `faf686ac` | 3 checks pass; raw Trivy HIGH findings remain | Staged capability registry and DAG executor; registry remains draft/disabled and preview-only |
| [#115](https://github.com/StrategyG/RusaAi/pull/115) | Draft | `main` → `feat/rusa-knowledge-governance-lifecycle` | `d8e7a7c5` | 3 checks pass | Rights, review, human approval and publication gates; further lifecycle controls remain open |
| [#82](https://github.com/StrategyG/RusaAi/pull/82) | Draft | Old main SHA `00aa9e4a` → `feat/rusa-knowledge-core-phase1` | `c882928e` | 3 checks pass | Project-scoped RAG and knowledge mining; base is behind current main and migration overlap must be reconciled |
| [#109](https://github.com/StrategyG/RusaAi/pull/109) | Ready | `feat/architecture-v2-cpu-first-alignment` → `feat/postgres-inference-admission` | `aa3fda1b` | 2 checks pass, 1 security check cancelled | Stacked inference-admission branch; migration 010 already exists on main |
| [#110](https://github.com/StrategyG/RusaAi/pull/110) | Draft | `feat/architecture-v2-cpu-first-alignment` → `feat/shared-api-inference-gateway-rebased` | `79f756a1` | Not verified in this audit | Stacked worker inference gateway |
| [#108](https://github.com/StrategyG/RusaAi/pull/108) | Ready | `feat/architecture-v2-cpu-first-alignment` → `feat/shared-api-inference-gateway` | `8e79cc8e` | Not verified in this audit | Stacked worker inference gateway |
| [#107](https://github.com/StrategyG/RusaAi/pull/107) | Ready | Old main SHA `31a2bb74` → `feat/architecture-v2-cpu-first-alignment` | `9ecd2570` | Historical exact-head CI reported pass | CPU-first architecture alignment; base is behind current main |
| [#106](https://github.com/StrategyG/RusaAi/pull/106) | Draft | Old main SHA `31a2bb74` → `fix/production-hardening-2026-11-11` | `aaa6be1e` | Not verified in this audit | Signed release/deploy pipeline |
| [#103](https://github.com/StrategyG/RusaAi/pull/103) | Draft | Old main SHA `31a2bb74` → `feature/model-plane-contract-v0.1.0-2026-11-11` | `ee3020bc` | Not verified in this audit | Versioned Model Plane API contract |
| [#99](https://github.com/StrategyG/RusaAi/pull/99) | Draft | Old main SHA `31a2bb74` → `integration/rusa-ai-phases-1-6-review` | `4fa4c589` | Not verified in this audit | Platform Core and Agentic DevOps foundation |
| [#100](https://github.com/StrategyG/RusaAi/pull/100) | Draft | `feat/devops-staging-deploy-2026-11-10` → `feat/devops-operations-monitoring-2026-11-10` | `99f94302` | Not verified in this audit | Operations/monitoring/security specialists |
| [#98](https://github.com/StrategyG/RusaAi/pull/98) | Ready | Old main SHA `c2866e2d` → `feature/devops-agent-contract-foundation` | `268c140f` | Not verified in this audit | Versioned DevOps agent contracts |
| [#95](https://github.com/StrategyG/RusaAi/pull/95) | Draft | Old main SHA `c2866e2d` → `feature/devops-contract-foundation` | `06733f11` | Not verified in this audit | Overlapping DevOps contract foundation |
| [#94](https://github.com/StrategyG/RusaAi/pull/94) | Draft | `feat/devops-release-candidate-2026-11-10` → `feat/devops-staging-deploy-2026-11-10` | `e0c3eb00` | Not verified in this audit | Stacked signed staging deployment/rollback |
| [#92](https://github.com/StrategyG/RusaAi/pull/92) | Draft | `feat/devops-build-independent-qa-2026-11-10` → `feat/devops-release-candidate-2026-11-10` | `516f6fe8` | Not verified in this audit | Stacked release candidate |
| [#91](https://github.com/StrategyG/RusaAi/pull/91) | Draft | `feat/devops-plan-code-execution-2026-11-08` → `feat/devops-build-independent-qa-2026-11-10` | `96b501e7` | Not verified in this audit | Stacked build and independent QA specialists |
| [#88](https://github.com/StrategyG/RusaAi/pull/88) | Draft | Old main SHA `5c58e1a4` → `feat/aicode-code-refinery-v3` | `6a9320ce` | Not verified in this audit | Large AICode integration; 300 commits/83 files |
| [#87](https://github.com/StrategyG/RusaAi/pull/87) | Draft | Old main SHA `5c58e1a4` → `docs/four-foundations-repository-audit-2026-11-09` | `896b1367` | Not verified in this audit | Prior integration audit/migration plan |
| [#86](https://github.com/StrategyG/RusaAi/pull/86) | Ready | Old main SHA `5c58e1a4` → `fix/p0-approval-resource-bounds` | `a730b93d` | Not verified in this audit | Approval and resource bounds |
| [#85](https://github.com/StrategyG/RusaAi/pull/85) | Ready | Old main SHA `5c58e1a4` → `feat/rusa-ai-precision-control-center` | `5f659e9a` | Not verified in this audit | Enterprise control center |

### RusaModels

| PR | Status | Base → head | Head SHA (short) | CI at inspected head | Relevance |
|---|---|---|---|---|---|
| [#24](https://github.com/StrategyG/RusaModels/pull/24) | Draft | `main` → `docs/chunk-01-model-plane-terminology-20261113` | `128343fe` | Windows script validation PASS; test job in progress | Clarifies internal registry-store vs ecosystem Control Plane; updates FastAPI OpenAPI description to state Model Plane ownership |
| [#20](https://github.com/StrategyG/RusaModels/pull/20) | Draft | `carbon-cpu/from-scratch-v0.1` → `feat/phase3-neural-architecture-tokenizer` | `74ab40bd` | Not verified in this audit | Stacked neural architecture/tokenizer engine |
| [#17](https://github.com/StrategyG/RusaModels/pull/17) | Ready | Old main SHA `c5ee2f0b` → `feat/phase2-dataset-engine-main` | `cb4e7b80` | Not verified in this audit | Dataset engineering engine |
| [#18](https://github.com/StrategyG/RusaModels/pull/18) | Ready | Old main SHA `c5ee2f0b` → `feat/phase5-benchmark-engine` | `eedd2a52` | Not verified in this audit | Frozen benchmark/evaluation engine |
| [#14](https://github.com/StrategyG/RusaModels/pull/14) | Ready | Old main SHA `c5ee2f0b` → `carbon-cpu/from-scratch-v0.1` | `9256597b` | Not verified in this audit | CPU research trainer |
| [#3](https://github.com/StrategyG/RusaModels/pull/3) | Ready | Old main SHA `004d7032` → Dependabot pytest update | `6b682801` | Not verified in this audit | Development dependency update |
| [#4](https://github.com/StrategyG/RusaModels/pull/4) | Ready | Old main SHA `004d7032` → Dependabot datasets update | `419407c1` | Not verified in this audit | Development dependency update |

### RAG_Cerdas

| PR | Status | Base → head | Head SHA (short) | CI at inspected head | Relevance |
|---|---|---|---|---|---|
| [#28](https://github.com/StrategyG/RAG_Cerdas/pull/28) | Draft | `main` → `docs/chunk-01-canonical-store-alignment-20261113` | `65752d94` | 1 check passes | Aligns IM19 docs with RusaAi canonical-store decision and links parent architecture authority |
| [#16](https://github.com/StrategyG/RAG_Cerdas/pull/16) | Draft | Old main SHA `18f65b00` → `integration/cataran-contracts-v1-20261105` | `8e8bd2fc` | 3 checks pass | Provisional event-envelope snapshot; base is behind current main |

The remaining open RusaAi PR backlog is not represented as merge-ready by this inventory. The 85-result follow-up search is the current repository-wide count; the table above identifies PRs with direct relevance to the current roadmap's architecture, knowledge, agent/worker, model, contract and release dependencies. Older or unrelated PRs require a separate deduplication and disposition pass before any merge.

## 4. Mainline implementation classification

| Capability | Mainline status | Evidence boundary |
|---|---|---|
| RusaAi Control Plane / RusaModels Model Plane separation | Implemented in code/docs; accepted RusaAi ADR | Static inspection and CI; cross-repo ADR wording still being aligned |
| RusaAi fixed six-role worker pipeline | Implemented | Static inspection; no knowledge-to-action held-out benchmark |
| Staged capability registry and new DAG executor in PR #116 | PR-only, draft | Not merged; registry described as disabled/preview-only |
| Tenant-scoped task queue and task checkpoint memory | Implemented | Code/migration/tests present; no production DR drill |
| RusaAi canonical `rusa_knowledge` store | Implemented as optional service; selected by RusaAi ADR for planning | Static inspection and main CI; not production activated |
| Agent/project scope indexes and document version snapshots | Present on current RusaAi main | Migrations 011/012 present; deployment/migration execution is not inferred |
| Rights/review/approval/publication lifecycle for RusaAi knowledge | PR-only extension | PR #115 draft; remaining deletion/index reconciliation controls still open |
| IM19 extraction/review pipeline | Implemented as isolated pilot | Repository tests and documentation; live providers/corpus quality/production operation blocked |
| RusaAi-Edge production model checkpoint and GGUF artifact | Not implemented / not evidenced | Checkpoint/tokenizer not selected, artifact not built, benchmark not run |
| RusaModels dataset/architecture/benchmark engine extensions | PR-only | PRs #14/#17/#18/#20 open; do not report as merged |
| Production deployment, staging soak, backup/restore and rollback certification | Not verified | No target-environment evidence in this audit |

## 5. Audit limitations

- The GitHub connector provided remote source and metadata inspection. A local clone/working tree was not available; local uncommitted changes are therefore **NOT_VERIFIED**.
- This audit did not run repository tests locally. The cited test results are GitHub Actions check runs only.
- No live PostgreSQL/Supabase, Dropbox, object-store, model-runtime, production host, secret manager, or staging environment was accessed.
- No PR was merged, no branch protection was changed, no production deployment was attempted, and no database migration was executed.
- A PR's successful checks do not prove it is based on current main, mergeable, or approved.

## 6. CHUNK 00 acceptance result

| Acceptance criterion | Result | Evidence |
|---|---|---|
| Main SHA for all three repositories recorded | PASS | Section 1 |
| Relevant open PRs inventoried with base/head/status and CI evidence where inspected | PASS | Section 3; uninspected CI explicitly marked |
| PR-only features kept separate from mainline features | PASS | Sections 3–4 |
| Test evidence linked to exact commit SHA | PASS | Sections 1 and Evidence Ledger |
| Blockers assigned to owning repositories | PASS | `OPEN_RISKS.md` |
| No source code changed during audit | PASS | Documentation-only audit artifacts; no source changes |

**CHUNK 00 result: PASS for the read-only baseline gate.** This closes repository discovery only. It does not certify production readiness or authorize implementation, merge, migration, or deployment.
