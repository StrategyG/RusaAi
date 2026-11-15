# Rusa AI Ecosystem — Execution State

**Execution specification:** `RUSA_AI_ECOSYSTEM_CHATGPT_ROADMAP_EN.md` v3.0  
**Project priority instructions:** `note.md`  
**Initial snapshot:** 2026-11-14  
**Latest refresh:** 2026-11-15  
**Coordinator:** ChatGPT Project NOTE  
**Overall status:** IN_PROGRESS  
**Active chunk:** CHUNK 01 — Architecture Reconciliation and ADR Governance  
**Last closed chunk:** CHUNK 00 — Repository Discovery, Baseline, and Evidence Inventory (PASS; refreshed 2026-11-15)

## 1. Operating rules

- Roadmap v3.0 is the primary execution specification. Its chunk sequence is CHUNK 00–15 and its fixed scorecard has nine dimensions with weights totalling 100.
- Work one dependency-approved chunk at a time.
- Never modify `main` directly, merge a PR, deploy production, perform destructive DDL, bulk-ingest data or delete external data without explicit approval.
- A PR branch is not mainline implementation. CI success is evidence only for the exact SHA and checks shown.
- Use PASS only when every chunk acceptance criterion is satisfied; PARTIAL, BLOCKED, FAIL and NOT_VERIFIED do not satisfy dependencies.
- CHUNK 00 made no source-code changes. CHUNK 01 includes a one-line FastAPI OpenAPI description metadata change in RusaModels PR #24; no runtime behavior, schema, model artifact, production data, or deployment behavior changed.

## 2. Repository baseline

| Repository | Branch | Verified main SHA | Exact-main CI checks | Main protected | Open PRs at baseline |
|---|---|---|---:|---:|---:|
| RusaAi | main | `1adac8d340e5870570910168b6626fe285dd2cf2` | 4/4 pass | No | 85 |
| RusaModels | main | `12c49b1372b230f3656be6e1badeabad870bcfa1` | 2/2 pass | No | 7 |
| RAG_Cerdas | main | `1ead461e76d563445d179e7d5e80c25a9ca9f059` | 5/5 pass | No | 2 |

The exact-main CI check-run evidence and workflow URLs are in `EVIDENCE_LEDGER.md`. The local working-tree state is **NOT_VERIFIED** because this audit used remote GitHub inspection and no local checkout was available.

## 3. Chunk tracker — Roadmap v3.0

| Chunk | Status | Evidence / note |
|---|---|---|
| 00 — Repository Discovery, Baseline, Evidence Inventory | PASS | Refreshed baseline artifacts on PR #119; final inspected head `d1f00563` passed 3/3 exact-head checks in run 43685578070; uncertainties are explicitly recorded |
| 01 — Architecture Reconciliation and ADR Governance | PARTIAL | Active: PRs #117, #24 and #28 are open, mergeable and green on their exact inspected heads, but unreviewed/unmerged; mainline documentation remains inconsistent |
| 02 — Trusted Identity, Authorization, Tenant Security | NOT_STARTED | Depends on CHUNK 01 |
| 03 — Canonical Knowledge Lifecycle and Governance | NOT_STARTED | Depends on CHUNK 02 |
| 04 — Model Registry, Artifact Integrity, Release Lifecycle | NOT_STARTED | Depends on CHUNK 02 |
| 05 — Versioned API Contract Between RusaAi and RusaModels | NOT_STARTED | Depends on CHUNK 03 and 04 |
| 06 — Inference Gateway, Admission Control, Worker Reliability | NOT_STARTED | Depends on CHUNK 05 |
| 07 — Model Runtime, CPU Profile, Hardware Validation | NOT_STARTED | Depends on CHUNK 06 |
| 08 — IM19 Pilot Hardening and Controlled Knowledge Boundary | NOT_STARTED | Depends on CHUNK 07; retain IM19 isolation |
| 09 — Retrieval, Model, Agent Evaluation Framework | NOT_STARTED | Depends on CHUNK 07; human-reviewed evaluation data required |
| 10 — Agent Registry, DAG Orchestration, Tool Governance | NOT_STARTED | Depends on CHUNK 08 and 09 |
| 11 — Reliability, Disaster Recovery, Observability | NOT_STARTED | Depends on CHUNK 10 |
| 12 — Security Supply Chain and Deployment Hardening | NOT_STARTED | Depends on CHUNK 11 |
| 13 — Integrated Staging End-to-End Certification | NOT_STARTED | Depends on CHUNK 12 |
| 14 — Production-Readiness Certification and Controlled Rollout | NOT_STARTED | Depends on CHUNK 13 |
| 15 — Capacity, Scale, Corpus Expansion | NOT_STARTED | Depends on CHUNK 14 |

No CHUNK 16 exists in the supplied v3.0 roadmap. Any additional engineering work must be placed within the existing chunks or proposed as an explicit roadmap revision; it must not be silently added to this tracker.

## 4. Active CHUNK 01 PR set

| Repository | PR | Head SHA | CI result | State |
|---|---:|---|---|---|
| RusaAi | [#117](https://github.com/StrategyG/RusaAi/pull/117) | `5f6b7c150aa234a852e413749412a0f7929ef2c6` | 3/3 PASS in run 43679880047; 337 passed / 6 skipped | Ready/open |
| RusaModels | [#24](https://github.com/StrategyG/RusaModels/pull/24) | `128343fee6973fe19158182a4c8b40c5be5a72c2` | 2/2 PASS (run 43680033627) | Ready/open |
| RAG_Cerdas | [#28](https://github.com/StrategyG/RAG_Cerdas/pull/28) | `84e9a13450c4b107d470eed775c1bb0998981acb` | 1/1 PASS (run 43680080103) | Ready/open |

PR #117 now includes:
- Cross-repository architecture reconciliation v1.0.
- Updated canonical knowledge ADR migration-state description.
- Updated production-readiness migration-state description.
- ADR index and explicit duplicate ADR-0002 `OPEN_DECISION`.
- Current-main inference and embedding data-flow qualification, including direct Worker → RusaModels embeddings when `RUSA_RAG_ENABLED=true` and the separate MCP service's cached `KnowledgeService` client.
- RAG/MCP operations warning that production Worker RAG remains disabled until embedding calls are brokered through an approved gateway; any MCP deployment needs a separately reviewed credential/egress boundary.

PR #24 clarifies that the RusaModels shared PostgreSQL registry is internal to Model Server and Quant Server, not the RusaAi ecosystem Control Plane. It also updates the FastAPI OpenAPI description so the API no longer labels RusaModels as the ecosystem Control Plane.

PR #28 supersedes the stale no-selection language in the RAG_Cerdas ADR at PR-branch level, documents RusaAi canonical-store selection and preserves IM19 isolation.

**Merge status:** none of these PRs is merged. The current main branches remain unchanged. Explicit user approval is required before merging.

### CHUNK 01 task-level scorecard

Scores below measure evidence-backed completion of the eight CHUNK 01 tasks in the candidate documentation/source branches. They are not the ecosystem production-readiness score. An unresolved decision receives credit only when it is explicitly recorded and the affected implementation remains blocked. Mainline adoption is still pending.

| Roadmap task | Score / 100 | Status | Evidence and remaining gap |
|---|---:|---|---|
| 1. Compare ADRs across all three repositories | 90 | PARTIAL | RusaAi, RusaModels and RAG_Cerdas ADRs inspected; RusaAi duplicate ADR-0002 identifier remains an explicit OPEN_DECISION |
| 2. Confirm `rusa_knowledge` as canonical shared store | 100 | PASS — decision confirmation | Accepted RusaAi ADR-0002 confirmed; PR #28 aligns IM19 documentation without authorizing production activation |
| 3. Reconcile RAG_Cerdas no-selection wording | 85 | PARTIAL | PR #28 updates ADR, README and roadmap on its branch; RAG_Cerdas main remains unchanged until review/merge |
| 4. Define API, model, database, artifact, knowledge, worker and identity ownership | 95 | PARTIAL | Cross-repository ownership matrix and exact service boundaries documented; PR #24 also corrects RusaModels API description; all alignment PRs remain open and ready for review; merge approval is still pending |
| 5. Define cross-repository communication rules | 90 | PARTIAL | Provider/consumer routes and credentials documented; direct worker embedding calls bypass the chat gateway and are now explicitly marked NOT_APPROVED for production; custom inference JSON schema is not yet consumed and live E2E conformance is not verified |
| 6. Define data classification and trust boundaries | 95 | PASS — documentation evidence | Public/internal/tenant-confidential/restricted-secret/untrusted classes and fail-closed invariants documented; deployment enforcement remains unverified |
| 7. Record unresolved decisions as OPEN_DECISION | 100 | PASS — documentation evidence | ADR numbering, embedding profile, IM19 inference path and production activation constraints are explicitly recorded |
| 8. Create architecture decision matrix | 95 | PASS — documentation evidence | Selected, rejected, retained and open options documented with rationale and conditions |
| **Candidate documentation completion** | **94 / 100** | **PARTIAL overall** | High document coverage does not satisfy merge, mainline consistency, integration or production gates |

## 5. Roadmap v3.0 production-readiness scorecard

Fixed weights from section 2.3 of the supplied roadmap are used. These are preliminary evidence-based assessments, not certifications. A weighted score does not override a failed critical release gate.

| Dimension | Weight | Score / 100 | Weighted contribution | Evidence and remaining gap |
|---|---:|---:|---:|---|
| Architecture and ownership | 10 | 70 | 7.00 | Control Plane/Model Plane and canonical-store decisions exist; ADR number collision and mainline cross-repository doc alignment remain open |
| API contracts and integration | 15 | 45 | 6.75 | Chat gateway is profile-dependent; optional KnowledgeService embedding calls bypass it and require Model Server credentials in the worker; complete versioned provider/consumer contract and cross-repo E2E conformance are not verified |
| Identity, security and tenant isolation | 15 | 50 | 7.50 | RLS, roles, worker allowlists and sandbox controls exist; optional direct worker embedding path can expose a Model Server API credential, all main branches lack protection, and independent cross-tenant security evidence is absent |
| Model and artifact readiness | 10 | 25 | 2.50 | Registry/runtime infrastructure exists; production RusaAi-Edge checkpoint, tokenizer, immutable GGUF artifact and target-device benchmark are absent |
| Knowledge governance and provenance | 10 | 40 | 4.00 | Tenant/project retrieval and version history exist; complete rights/review/approval/publication/withdrawal/purge lifecycle is not on main |
| Testing and evaluation quality | 10 | 60 | 6.00 | Exact-main and selected PR CI pass; reviewed retrieval qrels, held-out agent evaluation and system-building benchmarks are not verified |
| Reliability and recovery | 10 | 45 | 4.50 | Queue/lease/checkpoint/retry foundations exist, but the cross-process PostgreSQL FIFO admission test has repeated exact-head failures; staging failure injection, backup/restore and disaster-recovery drills are not evidenced |
| Deployment and observability | 10 | 30 | 3.00 | Container/Compose/workflow assets exist; target staging deployment, soak, alerting and operational evidence are absent |
| Release governance and evidence | 10 | 35 | 3.50 | Readiness gates and evidence workflows exist; main branch protection is disabled, PR backlog is large, and no release-owner certification exists |
| **Total** | **100** | | **44.75 / 100** | **Rounded baseline: 45/100. Revised from 48 after the direct worker embedding path and repeated FIFO admission failures were verified. Production remains NO-GO / NOT_VERIFIED.** |

The roadmap's previous-audit engineering maturity estimates (RusaAi 71, RusaModels 78, RAG_Cerdas 70, ecosystem 58) are historical estimates and have not been re-scored by this baseline. Engineering maturity and production readiness remain separate measures.

### Additional verified architecture evidence

- RusaModels main `rusamodels/api.py` exposes `/openai/v1/models`, `/openai/v1/chat/completions`, `/openai/v1/completions` and `/openai/v1/embeddings`.
- RusaAi main `llm-project/serve/app.py` exposes the tenant-authenticated `/v1/models` and `/v1/chat/completions` gateway routes with admission control.
- The RusaAi CPU Compose profile enables worker gateway mode, supplies tenant-scoped RusaAi worker keys and clears direct RusaModels worker credentials. The generic environment defaults chat gateway mode off and supports a direct chat-call fallback; production-wide fail-closed enforcement is not verified.
- When `RUSA_RAG_ENABLED=true`, `rusa_agents/runtime.py` constructs `KnowledgeService.from_env()` inside the worker. `rusa_knowledge/service.py` resolves the Model Server base URL and `RUSA_EMBEDDING_API_KEY` / `RUSA_MODEL_API_KEY`, then posts to `/embeddings`; RusaModels `/openai/v1/embeddings` accepts `inference`, `manager`, or `administrator` roles. This optional path bypasses the RusaAi chat gateway. The audited CPU Compose profile leaves RAG disabled by default; production Worker RAG must remain disabled until a tenant-aware embedding gateway/broker is implemented.
- PR #117's superseded head `e377c268` failed the FIFO admission test in run 43552467511; intermediate head `251a7d1f` passed 3/3 checks in run 43552929727, including 337 tests with 6 skipped, with raw Trivy HIGH findings 103/118/117/103 across four images and 0 CRITICAL. The latest head `25dd631d` adds the embedding and MCP credential-boundary qualifications; its CI is queued in run 43558557983, so earlier PASS evidence does not apply to this SHA.
- PR #120's exact head `ba04a053` initially failed the same cross-process FIFO admission test; workflow 43504174547 attempt 2 passed all three jobs, including 337 tests with 6 skipped. PR #117 head `25dd631d` failed the same test again in workflow 43558557983 (336 passed, 1 failed, 6 skipped; expected 2 PostgreSQL scheduler waiters). Treat the repeated exact-head failures as an unresolved concurrency/race issue, not a harmless flake.
- PR #119's earlier head `5772c23c` completed workflow 43554368639 with 3/3 checks passing and raw Trivy HIGH counts 103/118/117/103 across API, agent-sandbox, worker and RAG/MCP images, 0 CRITICAL; the gate uses `--ignore-unfixed`. Its latest tracker head before this refresh was `a790d203`; live metadata is authoritative because this update starts another CI run. PR #24's latest head `128343fe` passes 2/2 checks in run 43554181866; raw Trivy findings remain recorded in RSK-019.
- PR #116's exact-head CI passed, but its Trivy workflow output contains raw HIGH findings across the four images and uses `--ignore-unfixed`; the findings are not an all-clear security result.

## 6. Current blockers

- Three architecture-alignment PRs are open and ready for review, with no review submissions or inline review threads. RusaAi #117 (head 5f6b7c15), RusaModels #24 (128343fe), and RAG_Cerdas #28 (84e9a134) have green exact-head CI and are reported mergeable, but none is reviewed or merged; mainline documentation remains inconsistent. A separate newer run on out-of-sequence RusaAi PR #121 (head f12642d7) reproduced the FIFO test failure with a PostgreSQL advisory-lock/RowExclusiveLock deadlock (348 passed, 1 failed, 6 skipped), so the concurrency risk is not resolved by the passing #117 head.
- RusaAi contains two ADR files with numeric identifier ADR-0002; the collision is explicitly an OPEN_DECISION and must be corrected in a dedicated reviewed change with repository-wide reference validation.
- The current RAG_Cerdas main branch still has the historical no-selection ADR until PR #28 is merged.
- The current RusaModels main terminology is clarified only on PR #24 until merged.
- The current main branches report branch protection disabled.
- PR #119 is a self-updating tracker. The last observed head before this refresh, `e41ea1d9fb2304296edbd7fa2be650ae08b458be`, had 2/3 checks complete and its production-image scan in progress in run 43557157903. This tracker update triggers a new run, so live PR metadata is authoritative for the latest SHA and CI. Its seven changed paths remain documentation only.
- RusaAi PR #115 proposes additional knowledge governance migration 013; it remains open/draft and is not mainline.
- RusaAi worker result contracts do not yet expose a complete typed immutable knowledge-reference/version trace.
- RusaAi KnowledgeService is optional and the runtime activation flag defaults false; its worker-side embedding provider bypasses the RusaAi chat gateway when enabled, and the MCP service uses the same provider factory (RSK-020).
- Production RusaAi-Edge model checkpoint, tokenizer, artifact and target-device evidence are absent.
- Human-reviewed evaluation corpus, staging end-to-end evidence, backup/restore and release certification are absent.

PR #117 and RusaModels #24 image scans completed with raw Trivy HIGH findings (RSK-015 and RSK-019); green CI is not a zero-vulnerability certification. CHUNK 12 owns remediation. PR #117 removed the overlapping Agent/Worker specification from its CHUNK 01 branch; PR #120 remains deferred for CHUNK 10 under RSK-018. RSK-016 records the repeated FIFO admission failure; RSK-020 records direct Worker and optional MCP → RusaModels embedding paths. See `OPEN_RISKS.md` for risk ownership and required resolution.

## 7. CHUNK 01 next actions

1. The three PRs are already marked ready for review. Complete owner/maintainer review; obtain explicit project-owner approval before any merge. No reviews or inline review threads are currently recorded.
2. Proposed merge order: RusaModels #24, then RAG_Cerdas #28 (both align with already-accepted RusaAi main ADRs), then refresh RusaAi #117 wording to reflect the merged states, rerun its exact-head CI, and merge RusaAi #117 last.
3. After the coordinated merges, verify all three new main SHAs and repeat the cross-repository documentation/link consistency check.
4. Keep the duplicate RusaAi ADR-0002 identifier as an explicit OPEN_DECISION until its separate reference-checked numbering PR is reviewed; do not silently renumber.
5. Close CHUNK 01 only after mainline consistency is verified; then proceed to CHUNK 02.

## 8. Resume rule

At each new session, re-check current default branch SHAs, repository instructions, PR base/head SHAs, CI check-runs and relevant source files. Continue from the first unpassed chunk. Never infer mainline status from a PR branch or prior conversation summary.


## 9. Live execution refresh — 2026-11-15

This section supersedes the snapshot-sensitive baseline counts and PR statuses above. The full refreshed inventory is in [BASELINE_REFRESH_2026-11-15.md](BASELINE_REFRESH_2026-11-15.md).

### Current verified repository baseline

| Repository | Main SHA | Exact-main checks | Open PRs | Notes |
|---|---|---:|---:|---|
| RusaAi | 1adac8d340e5870570910168b6626fe285dd2cf2 | 4/4 success | 87 | 337 passed / 6 skipped; raw image HIGH findings remain |
| RusaModels | 12c49b1372b230f3656be6e1badeabad870bcfa1 | 2/2 success | 8 | 155 passed / 1 skipped / 3 warnings; raw image HIGH findings remain |
| RAG_Cerdas | 1ead461e76d563445d179e7d5e80c25a9ca9f059 | 1 test check success plus 10 scheduled audit runs | 4 | 240 passed on the push test run |

All three main SHAs are unchanged. Branch metadata returned protected=false; detailed branch-protection rules remain NOT_VERIFIED because the branch-protection API returned 403. Local working-tree state remains NOT_VERIFIED.

### Chunk tracker — current state

| Chunk | Status | Evidence / blocker |
|---|---|---|
| 00 — Repository Discovery, Baseline, Evidence Inventory | PASS | PR #119 head `d1f00563` passed all 3 exact-head checks in run 43685578070; 337 passed / 6 skipped, sandbox 304 passed / 39 skipped; raw Trivy HIGH findings are retained as an open risk |
| 01 — Architecture Reconciliation and ADR Governance | PARTIAL | Active chunk. PR #117, RusaModels #24 and RAG_Cerdas #28 have green exact-head CI but no submitted reviews or inline threads and remain unmerged; mainline documentation remains inconsistent |
| 02–15 | NOT_STARTED | Roadmap dependency graph; CHUNK 01 must pass first |

### Current alignment PR state

- RusaAi #117: head 5f6b7c15, 3/3 CI success; 337 passed / 6 skipped, including the cross-process FIFO test.
- RusaModels #24: head 128343fe, latest 2/2 CI success.
- RAG_Cerdas #28: head 84e9a134, 1/1 CI success.
- RusaAi #119: final inspected pre-closeout head `d1f00563d0beab2123bc9d5e96ae2d437c5296d5` passed 3/3 checks in workflow 43685578070 (337 passed / 6 skipped; sandbox 304 passed / 39 skipped). This closeout update advances the tracker branch; re-check the new head before any merge.
- RusaAi #121: head f12642d7; RUSA-AI CI test/lint/security FAIL on the FIFO PostgreSQL deadlock (348 passed, 1 failed, 6 skipped), RAG/MCP PASS, image build/scan PASS; Web, VS Code extension and Windows launcher syntax CI PASS.
- RusaModels #25: head 9cf2816d, CI PASS; RAG_Cerdas #30: head b7e314de, CI PASS. Both remain draft and out of sequence; they do not satisfy CHUNK 07 or CHUNK 08.
- No reviews or inline review threads were present for #119, #117, #24, or #28 at the inspected snapshot. No merge was performed or authorized.

### Updated risks and score

- RusaAi PR #117 head 5f6b7c15 passes the FIFO test, but a newer independent PR #121 run reproduces a PostgreSQL DeadlockDetected in the same cross-process scheduler test (348 passed, 1 failed, 6 skipped). RSK-016 remains High and unresolved; CHUNK 06/11 cannot pass until lock-ordering is fixed and repeated tests pass.
- RusaAi #122 head 947052c2 has a Ruff E731 lint failure. RusaAi #121 head f12642d7 has Web, VS Code and Windows launcher CI passing, RAG/MCP integration and image build/scan passing, but RUSA-AI test/lint/security failing on the FIFO deadlock.
- RusaModels #25 and RAG_Cerdas #30 are draft companion PRs for the same local-first workstream; all three (#121/#25/#30) are out of roadmap sequence and remain parked.
- Direct Worker/MCP embedding credential paths, duplicate RusaAi ADR-0002 identifiers, mainline documentation drift, raw Trivy HIGH findings, missing staging/restore evidence, and absent approved model artifact remain open.
- The ecosystem production-readiness score remains 45/100; this refresh adds no production or staging evidence and no score increase is justified.

### Next action

CHUNK 00 is closed as PASS for the read-only baseline audit. Continue CHUNK 01 review of PRs #117, #24 and #28. Preserve the coordinated order: merge #24, then #28, refresh #117 to reflect both merges, then merge #117 last — only after explicit project-owner approval. Do not merge any of these PRs without that approval.


## 10. CHUNK 00 closeout — 2026-11-15

- **Status:** PASS for the read-only repository baseline and evidence inventory.
- **Coordinator PR:** [RusaAi #119](https://github.com/StrategyG/RusaAi/pull/119), documentation-only branch; seven changed paths, no source-code changes.
- **Verified head at closeout:** `d1f00563d0beab2123bc9d5e96ae2d437c5296d5`.
- **Exact-head workflow:** [43685578070](https://github.com/StrategyG/RusaAi/actions/runs/43685578070), 3/3 jobs successful.
- **Test evidence:** 337 passed, 6 skipped; isolated sandbox suite 304 passed, 39 skipped; cross-process FIFO test passed on this exact head.
- **Security qualification:** raw Trivy HIGH counts 103 / 118 / 117 / 103 across API, agent-sandbox, worker and RAG/MCP images; CRITICAL 0. The blocking scan uses `--ignore-unfixed`; the scan is not a zero-finding certification. Keep RSK-015 open.
- **Limitations:** local working-tree state NOT_VERIFIED; detailed branch-protection configuration NOT_VERIFIED (branch metadata protected=false; direct endpoint returned 403); no staging/production evidence.
- **Boundary:** no main branch, merge, source code, database, model artifact, deployment or production data was changed. CHUNK 01 is now the active chunk.


## 13. Live CHUNK 01 review refresh — 2026-11-15

This section supersedes earlier PR-head, CI, and review snapshots above for the active CHUNK 01 PR set. It does not change the dependency order or authorize merges.

### Current active PR evidence

| Repository | PR | Latest head SHA | Exact-head CI evidence | Review state | Merge state |
|---|---:|---|---|---|---|
| RusaAi | [#117](https://github.com/StrategyG/RusaAi/pull/117) | `85a9d936bc0d8feb3b0c33a3336fa9fa83cdd3fa` | Run 43703955776: RAG/MCP integration PASS; Test/lint/security PASS (337 passed, 6 skipped; cross-process FIFO test passed); production image build/test/scan IN_PROGRESS | COMMENT reviews 5910229219 and 5910245817; corrections committed; one repository-instruction clarification remains open | Open; not merged |
| RusaModels | [#24](https://github.com/StrategyG/RusaModels/pull/24) | `7ca01d6373c1b90aadc0182461982754ca54afce` | Run 43704012560: 2/2 checks PASS; 156 passed, 1 skipped; Windows script, lint, compile, static security, PostgreSQL migration upgrade/rollback, image build, Trivy, health and Compose checks passed | COMMENT reviews 5910229332 and 5910262769; service-specific OpenAPI descriptions and regression test committed | Open; not merged |
| RAG_Cerdas | [#28](https://github.com/StrategyG/RAG_Cerdas/pull/28) | `84e9a13450c4b107d470eed775c1bb0998981acb` | Run 43680080103: 1/1 Institutional RAG check PASS | COMMENT review 5910241299; no blocking static documentation finding | Open; not merged |

### Review corrections committed

- RusaAi PR #117: commit `ce77576f405c21b96738ec23a47deaada33335c8` reconciles the production-readiness narrative with merged PR #111/current mainline and replaces ambiguous ADR-0002 shorthand; commit `85a9d936bc0d8feb3b0c33a3336fa9fa83cdd3fa` updates the RAG/MCP operations ADR references.
- RusaModels PR #24: commit `1d6bde6bc2c8d22dd1880c3b436e4e339e2712b0` makes the OpenAPI description service-specific; commit `7ca01d6373c1b90aadc0182461982754ca54afce` adds a regression test for Model Server versus Quant Server descriptions.
- RusaAi `.github/copilot-instructions.md` on main still describes RUSA-AI as a target independent AI model/product “not an already-existing orchestrator.” A review comment records this ambiguity against the current Control Plane ownership model. It is not silently rewritten; explicit clarification remains open.

### Concurrency risk refresh

RusaAi PR #121 current head `70d4f082e49105d62db678179cd6ec84109eeb66` has 4/4 associated workflows successful. Its RUSA-AI CI run `43690890924` reports 370 passed and 6 skipped; `test_fifo_admission_is_shared_across_separate_os_processes` passed on this exact head. Earlier exact-head runs recorded PostgreSQL deadlock/FIFO waiter failures. One successful run on the newer head does not prove the race is eliminated; RSK-016 remains open pending repeated same-head evidence and staging crash/restart verification.

### Governance and current boundaries

- Current main SHAs remain RusaAi `1adac8d340e5870570910168b6626fe285dd2cf2`, RusaModels `12c49b1372b230f3656be6e1badeabad870bcfa1`, and RAG_Cerdas `1ead461e76d563445d179e7d5e80c25a9ca9f059`.
- Branch metadata reports `protected=false` on all three main branches. RusaAi and RusaModels have no active repository rulesets; RAG_Cerdas has active ruleset `Rag` (ID 25698740), apparently applying to all branches, requiring 10 approving reviews, latest-push approval, review-thread resolution, linear history, signed commits, and CodeQL (HIGH-or-higher security alerts); no bypass actors are configured. Do not change repository rules or permissions without explicit approval.
- No PR was merged; no main branch, production database, production data, model artifact, secret, or deployment was changed.
- CHUNK 01 remains PARTIAL until exact-head CI completes, remaining documentation ambiguity is resolved or explicitly recorded, all three changes are adopted on main in the approved order, and cross-repository consistency is rechecked.
- Required merge order remains RusaModels #24, RAG_Cerdas #28, then refresh RusaAi #117 to reflect those merges and merge it last. Explicit project-owner approval is required before any merge.
- CHUNK 02 remains NOT_STARTED until CHUNK 01 passes.


## 14. Governance ruleset and PR #24 final CI refresh — 2026-11-15

- RusaModels PR #24 current head `7ca01d6373c1b90aadc0182461982754ca54afce` completed run `43704012560` with 2/2 checks PASS. Its test step reports 156 passed, 1 skipped. The Windows script validation, lint, compile, static security, PostgreSQL migration upgrade/rollback, container build, image scan, health smoke test, devcontainer and Compose validation completed successfully.
- Raw Trivy results for both RusaModels CI images: 47 HIGH OS-package findings and one HIGH Python SBOM finding for `setuptools@70.3.0` per image; 0 CRITICAL. The runtime check reports `setuptools 84.2.1`, the scoped VEX disposition for the stale SBOM component is verified, and the blocking gate uses `--ignore-unfixed`. This is not a zero-HIGH image result; RSK-019 remains open.
- RAG_Cerdas ruleset `Rag` (ID 25698740) is active. Its rules include 10 required approving reviews, approval of the latest push, review-thread resolution, required linear history, required signatures, and CodeQL security-alert threshold HIGH-or-higher. No bypass actors are configured. The ruleset has no ref include/exclude entries, so treat it as applying to all branches unless the owner verifies otherwise. This is in addition to branch metadata reporting `protected=false`.
- RAG_Cerdas PR #28 review 5910264729 records this merge prerequisite. No ruleset change is authorized.
- RusaAi PR #117 run 43703955776 remains IN_PROGRESS in the production image build/test/scan job; its RAG/MCP and Test/lint/security jobs have passed. The tracker PR #119 head `e24056726bddd3829953cb097d3a2cfa6d3812cd` is running workflow 43704538764 after the execution-state/evidence-ledger refresh.
- No PR has been merged. CHUNK 01 remains PARTIAL.


## 15. Live baseline and CHUNK 01 review refresh — 2026-11-15 14:58 UTC

### CHUNK 00 exact-head recheck

- Coordinator PR [#119](https://github.com/StrategyG/RusaAi/pull/119) was rechecked at head `8424918df9abe54de66543c66deec779eb4e40df`.
- Workflow [43704970382](https://github.com/StrategyG/RusaAi/actions/runs/43704970382) initially failed its Test/lint/security job because the cross-process PostgreSQL FIFO admission test encountered `DeadlockDetected` and did not observe the expected two waiting requests (336 passed, 1 failed, 6 skipped).
- The failed job was rerun without changing source code. Attempt 2 completed all three workflow jobs successfully; the test job reports 337 passed and 6 skipped. CHUNK 00 remains PASS as a read-only baseline audit on this exact head.
- This retry does not close RSK-016: the same scheduler test has failed on other exact PR heads and one retry pass is not repeated-race or staging crash/restart evidence.

### Current CHUNK 01 PR state

| Repository | PR | Exact head SHA | Current exact-head CI | Review / governance state | Merge |
|---|---:|---|---|---|---|
| RusaAi | [#117](https://github.com/StrategyG/RusaAi/pull/117) | `85a9d936bc0d8feb3b0c33a3336fa9fa83cdd3fa` | 3/3 PASS in run 43703955776 | Reviews 5910229219, 5910245817 and 5910269063 are COMMENTED; the Copilot mission wording clarification remains unresolved | Open |
| RusaModels | [#24](https://github.com/StrategyG/RusaModels/pull/24) | `7ca01d6373c1b90aadc0182461982754ca54afce` | 2/2 PASS in run 43704012560; 156 passed, 1 skipped | Review comments were addressed with service-specific OpenAPI descriptions and a regression test | Open |
| RAG_Cerdas | [#28](https://github.com/StrategyG/RAG_Cerdas/pull/28) | `84e9a13450c4b107d470eed775c1bb0998981acb` | 1/1 PASS in run 43680080103 | Review 5910264729 records active ruleset `Rag` (ID 25698740), requiring 10 approving reviews, latest-push approval, resolved threads, linear history, signed commits and CodeQL; no bypass actors | Open |

No PR was merged. CHUNK 01 remains PARTIAL. The required merge order remains RusaModels #24, RAG_Cerdas #28, then refresh and merge RusaAi #117 last, subject to explicit owner approval and all repository rules. CHUNK 02–04 remain blocked by the CHUNK 01 dependency; NEXUS model intake must not bypass this gate.

### NEXUS intake boundary

RusaModels main contains a strict Model Manifest v1, immutable artifact/version records and a draft-to-approved release lifecycle. No NEXUS/Argon model artifact or NEXUS-specific registration exists on RusaModels main. The RusaAi #123 assessment branch is a draft intake-boundary/remediation proposal, not a registered or approved model. Actual NEXUS source revision, tokenizer files, model weights, license evidence and checksummed artifacts remain prerequisites for a later CHUNK 04 candidate intake.
