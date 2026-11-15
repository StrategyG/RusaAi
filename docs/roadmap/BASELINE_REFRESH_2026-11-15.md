# RUSA AI Ecosystem — Baseline Refresh

**Snapshot:** 2026-11-15 (GitHub remote inspection; timestamps are UTC)  
**Purpose:** Refresh the 2026-11-14 CHUNK 00 baseline against live repository metadata, exact main SHAs, current open PR heads, and exact-SHA CI evidence.  
**Scope:** StrategyG/RusaAi, StrategyG/RusaModels, StrategyG/RAG_Cerdas  
**Mode:** Read-only audit plus documentation-only refresh on the existing CHUNK 00 PR branch. No source code, main branch, production data, secrets, deployments, or databases were changed.

> This refresh supersedes time-sensitive counts, PR head SHAs, PR CI statuses, and PR review states in the 2026-11-14 baseline snapshot. The original snapshot is retained as historical evidence.

## 1. Current repository baseline

| Repository | Default branch | Main SHA | Main commit date (UTC) | Tree entries | Test modules* | Migration files | Workflows | Open PRs |
|---|---|---|---|---:|---:|---:|---:|---:|
| RusaAi | main | 1adac8d340e5870570910168b6626fe285dd2cf2 | 2026-11-13 02:42:41 | 286 | 43 | 12 | 4 | 87 |
| RusaModels | main | 12c49b1372b230f3656be6e1badeabad870bcfa1 | 2026-11-13 03:26:00 | 183 | 23 | 7 | 3 | 8 |
| RAG_Cerdas | main | 1ead461e76d563445d179e7d5e80c25a9ca9f059 | 2026-11-12 21:39:47 | 243 | 26 | 9 | 3 | 4 |
| **Total** | | | | **712** | **92** | **28** | **10** | **99** |

*Test-module counts exclude test fixtures and package initializer files (for example, conftest.py and __init__.py). Tree counts include blobs and directories. Migration and workflow counts are path inventories, not proof that migrations were applied or workflows deployed.

All three default branches remain named main and their SHAs are unchanged from the previous baseline. The open-PR counts have changed since the 2026-11-14 snapshot: RusaAi 87, RusaModels 8, RAG_Cerdas 4 (99 total).

### Branch governance inspection

- GitHub branch metadata returned protected=false for main in all three repositories.
- Direct reads of the branch-protection endpoint returned HTTP 403 (Resource not accessible by integration). Consequently, the detailed required-check, review, ruleset, and force-push configuration is NOT_VERIFIED; do not infer its exact contents from this audit.
- No AGENTS.md or CONTRIBUTING* file was found in the inspected recursive main trees.
- RusaAi has .github/copilot-instructions.md; RAG_Cerdas has .github/copilot-instructions.md; RusaModels has .github/CODEOWNERS assigning the repository and sensitive paths to @StrategyG.
- No local clone or working tree was available. Local uncommitted changes remain NOT_VERIFIED.

## 2. Exact-main CI and test evidence

| Repository | Exact main SHA | GitHub check runs | Test evidence | Result |
|---|---|---:|---|---|
| RusaAi | 1adac8d340e5870570910168b6626fe285dd2cf2 | 4/4 success | Main test job: 337 passed, 6 skipped. Isolated sandbox test: 304 passed, 39 skipped. RAG/MCP integration job succeeded. Web build succeeded. | PASS for these exact CI checks |
| RusaModels | 12c49b1372b230f3656be6e1badeabad870bcfa1 | 2/2 success | 155 passed, 1 skipped, 3 warnings. PostgreSQL migration upgrade/rollback validation succeeded. Windows RUSA-CPU-16 script validation succeeded. | PASS for these exact CI checks |
| RAG_Cerdas | 1ead461e76d563445d179e7d5e80c25a9ca9f059 | 11 check runs | One push-triggered test run: 240 passed. Ten other successful runs were scheduled audit/agent-dispatch jobs, not ten additional unit-test runs. | Test suite PASS; scheduled audit jobs PASS |

Evidence links:

- RusaAi main CI: [run 43354051026](https://github.com/StrategyG/RusaAi/actions/runs/43354051026) and [web CI 43354050892](https://github.com/StrategyG/RusaAi/actions/runs/43354050892).
- RusaModels main CI: [run 43358929126](https://github.com/StrategyG/RusaModels/actions/runs/43358929126).
- RAG_Cerdas main tests: [run 43312458157](https://github.com/StrategyG/RAG_Cerdas/actions/runs/43312458157). The latest scheduled audit on this SHA was [run 43676646067](https://github.com/StrategyG/RAG_Cerdas/actions/runs/43676646067).

The legacy combined commit-status endpoint returned pending with no status entries for these SHAs, while the Checks API returned the completed successful check runs above. Record the Checks API evidence explicitly; do not describe the legacy status endpoint as green.

### Security scan qualification

- RusaAi main image scan reported raw Trivy HIGH counts of 103 (API), 118 (agent sandbox), 117 (worker), and 103 (RAG/MCP), with 0 CRITICAL. The blocking command uses --ignore-unfixed. A green CI result is not a zero-finding security certification.
- RusaModels main image scans reported 47 HIGH OS-package findings and 1 HIGH Python-package finding for each of the Model Server and worker images, with 0 CRITICAL. CI validated a narrowly scoped VEX disposition for the setuptools CVE-2025-47273 finding; this does not dispose of the OS-package findings.
- The inspected RAG_Cerdas test workflow does not provide an equivalent production-container vulnerability scan.
- No SBOM/VEX review or independent exploitability assessment was performed as part of this baseline refresh.

## 3. Current relevant open pull requests

The following PRs touch active or near-term roadmap dependencies. Base SHAs are included because a successful check on an old or stacked branch does not prove compatibility with current main. CI is the exact-head state returned during this refresh; re-fetch before review or merge because PR heads and workflows can change.

### RusaAi

| PR | Status | Base branch / base SHA | Head SHA | Exact-head CI | Mergeability / relevance |
|---|---|---|---|---|---|
| [#122](https://github.com/StrategyG/RusaAi/pull/122) | Draft | main / 1adac8d3 | 947052c2 | 3 success, 1 failure; lint stopped on Ruff E731, so later tests in that job were skipped | Mergeable=true; Agent/Worker queue and lease hardening; fix lint before further review |
| [#121](https://github.com/StrategyG/RusaAi/pull/121) | Draft | main / 1adac8d3 | f12642d7 | RUSA-AI CI: test/lint/security FAIL (348 passed, 1 failed, 6 skipped; PostgreSQL FIFO admission deadlock); RAG/MCP PASS; image build/scan IN_PROGRESS. Web CI, VS Code extension CI and Windows launcher syntax CI PASS. | Mergeable=true; local-first desktop and hybrid connectors; out-of-sequence draft, not fully green |
| [#120](https://github.com/StrategyG/RusaAi/pull/120) | Draft | main / 1adac8d3 | ba04a053 | 3/3 success | Agent/Worker chunk framework; defer to its roadmap dependency |
| [#119](https://github.com/StrategyG/RusaAi/pull/119) | Ready | main / 1adac8d3 | f1e4b938 | 3/3 success before this refresh commit | CHUNK 00 documentation package; six baseline artifacts; this refresh is being added on this branch |
| [#118](https://github.com/StrategyG/RusaAi/pull/118) | Draft | main / 1adac8d3 | 202a1c16 | 3/3 success | Separate ALE baseline inventory; not merged |
| [#117](https://github.com/StrategyG/RusaAi/pull/117) | Ready | main / 1adac8d3 | 5f6b7c15 | 3/3 success; main test job 337 passed, 6 skipped; cross-process FIFO test passed on this head | CHUNK 01 architecture/ADR reconciliation; image scan still reports raw HIGH findings |
| [#116](https://github.com/StrategyG/RusaAi/pull/116) | Draft | main / 1adac8d3 | faf686ac | 3/3 success | Candidate staged agent registry/DAG executor; not merged |
| [#115](https://github.com/StrategyG/RusaAi/pull/115) | Draft | main / 1adac8d3 | d8e7a7c5 | 3/3 success | Knowledge rights/review/approval/publication proposal; migration 013 remains PR-only |
| [#110](https://github.com/StrategyG/RusaAi/pull/110) | Draft | feat/architecture-v2-cpu-first-alignment / 9ecd2570 | 79f756a1 | 3/3 success | Stacked inference-gateway work; base branch is a PR branch |
| [#109](https://github.com/StrategyG/RusaAi/pull/109) | Ready | feat/architecture-v2-cpu-first-alignment / 9ecd2570 | aa3fda1b | 2 success, 1 cancelled | Stacked PostgreSQL admission/inference work; not a fully green check set |
| [#108](https://github.com/StrategyG/RusaAi/pull/108) | Ready | feat/architecture-v2-cpu-first-alignment / 117da0ef | 8e79cc8e | 0 check runs found for this exact head | Mergeable=false; stacked inference-gateway work |
| [#107](https://github.com/StrategyG/RusaAi/pull/107) | Ready | main / 31a2bb74 (old main) | 9ecd2570 | 3/3 success | Mergeable=false; CPU-first architecture branch is based on stale main |
| [#106](https://github.com/StrategyG/RusaAi/pull/106) | Draft | main / 31a2bb74 (old main) | aaa6be1e | 5/5 success | Mergeable=false; production hardening/release pipeline on stale base |
| [#103](https://github.com/StrategyG/RusaAi/pull/103) | Draft | main / 31a2bb74 (old main) | ee3020bc | 3/3 success | Proposed Model Plane contract; provider/consumer conformance not established |
| [#82](https://github.com/StrategyG/RusaAi/pull/82) | Draft | main / 00aa9e4a (old main) | c882928e | 3/3 success | Mergeable=false; older RAG/knowledge implementation with migration overlap risk |

### RusaModels

| PR | Status | Base branch / base SHA | Head SHA | Exact-head CI | Mergeability / relevance |
|---|---|---|---|---|---|
| [#25](https://github.com/StrategyG/RusaModels/pull/25) | Draft | main / 12c49b13 | 9cf2816d | 2/2 success | Local device inventory and runtime profiles |
| [#24](https://github.com/StrategyG/RusaModels/pull/24) | Ready | main / 12c49b13 | 128343fe | Latest run 2/2 success | CHUNK 01 terminology alignment; PR-only OpenAPI description clarification |
| [#20](https://github.com/StrategyG/RusaModels/pull/20) | Draft | carbon-cpu/from-scratch-v0.1 / 9256597b | 74ab40bd | 2/2 success | Stacked neural architecture/tokenizer work |
| [#18](https://github.com/StrategyG/RusaModels/pull/18) | Ready | main / c5ee2f0b (old main) | eedd2a52 | 1/1 success | Benchmark engine; stale base |
| [#17](https://github.com/StrategyG/RusaModels/pull/17) | Ready | main / c5ee2f0b (old main) | cb4e7b80 | 1/1 success | Dataset engine; stale base; mergeable=false |
| [#14](https://github.com/StrategyG/RusaModels/pull/14) | Ready | main / c5ee2f0b (old main) | 9256597b | 2/2 success | CPU research trainer; stale base; mergeable=false |

### RAG_Cerdas

| PR | Status | Base branch / base SHA | Head SHA | Exact-head CI | Mergeability / relevance |
|---|---|---|---|---|---|
| [#30](https://github.com/StrategyG/RAG_Cerdas/pull/30) | Draft | main / 1ead461e | b7e314de | 1/1 success | External RAG contract proposal; does not expose IM19 as a public service |
| [#29](https://github.com/StrategyG/RAG_Cerdas/pull/29) | Draft | main / 1ead461e | b8f4ae5d | 1/1 success | Precision RAG Agent/Worker architecture proposal |
| [#28](https://github.com/StrategyG/RAG_Cerdas/pull/28) | Ready | main / 1ead461e | 84e9a134 | 1/1 success | CHUNK 01 canonical-store alignment; IM19 remains isolated |
| [#16](https://github.com/StrategyG/RAG_Cerdas/pull/16) | Draft | main / 18f65b00 (old main) | 8e8bd2fc | 3/3 success | Provisional Cataran event contract; stale base |

Additional older RusaAi PRs and RusaModels dependency-update PRs remain open in the repository-wide backlog. The table above covers PRs with direct relevance to the active or near-term roadmap dependencies; it is not a merge recommendation. Several PRs have stale base SHAs or stacked bases even when their exact-head checks pass.

**Roadmap sequencing note:** RusaAi PR #121, RusaModels PR #25 and RAG_Cerdas PR #30 are a separate local-first desktop / hardware / external-RAG workstream created ahead of the roadmap dependencies that govern those areas. They remain draft and do not advance CHUNK 02, 05, 06, 07 or 08. Keep them parked; do not merge or treat their CI as end-to-end ecosystem evidence until the corresponding dependency-approved chunks are active. PR #121's local preview explicitly disables model inference; it is not a consumer installer or a verified local model runtime.

### Review state for the CHUNK 00/01 PR set

At inspection, RusaAi #119, RusaAi #117, RusaModels #24, and RAG_Cerdas #28 had no submitted reviews and no inline review threads. GitHub reported these PRs as mergeable at the time of inspection. Mergeability and green CI are not owner approval. None of these PRs was merged by this audit.

## 4. Current mainline documentation and architecture blockers

1. RusaAi main contains two distinct files numbered ADR-0002: the canonical shared Agent/Worker knowledge decision and the RusaModels Model Plane boundary decision. The collision remains an OPEN_DECISION; do not silently renumber.
2. RusaAi main docs/PRODUCTION_READINESS.md still contains migration-state wording that the CHUNK 01 PR #117 proposes to correct. Current main contains migrations 001–012; migration 013 remains proposed in PR #115. File presence does not prove that a target database has applied a migration.
3. RAG_Cerdas main ADR-0001 still contains the historical no-selection proposal. PR #28 proposes an alignment ADR that supersedes it for ecosystem-level canonical-store selection while retaining IM19 as an isolated pilot.
4. RusaModels main ADR-001 uses “shared control plane” to refer to its internal PostgreSQL registry store shared by Model Server and Quant Server. PR #24 proposes terminology clarification so it is not confused with the RusaAi ecosystem Control Plane.
5. The canonical shared Agent/Worker store remains RusaAi rusa_knowledge for implementation planning only. Production activation is not authorized.
6. No IM19 cross-read, cross-write, dual-write, mirroring, bulk copy, or synchronization path is authorized.
7. The optional RusaAi Worker KnowledgeService embedding path can call RusaModels /openai/v1/embeddings directly when RUSA_RAG_ENABLED=true, using RUSA_EMBEDDING_API_KEY or the RUSA_MODEL_API_KEY fallback. This bypasses the RusaAi chat gateway. The optional MCP service also constructs KnowledgeService.from_env() and requires a separately reviewed service-credential/egress boundary if deployed. Keep production Worker RAG disabled until a tenant-aware RusaAi embedding gateway or approved brokered contract exists.
8. RusaAi's generic environment defaults worker chat-gateway mode off and supports a direct provider fallback. The CPU Compose profile configures the gateway path, but production-wide fail-closed enforcement is NOT_VERIFIED.
9. The non-Qwen RusaAi-Edge checkpoint, tokenizer/chat template, immutable GGUF artifact digest, target-hardware benchmark, and production model-release evidence remain absent.
10. Reviewed retrieval qrels, held-out agent/system-building evaluation, staging E2E, backup/restore drills, incident alerting, and release-owner certification remain absent.

## 5. Historical FIFO admission failure — updated status

Earlier exact PR #117 heads had repeated failures in the cross-process PostgreSQL FIFO waiter test. The latest inspected PR #117 head, 5f6b7c150aa234a852e413749412a0f7929ef2c6, completed 3/3 CI checks; its test log reports 337 passed, 6 skipped, and the cross-process FIFO test passed.

A newer independent reproduction appeared on RusaAi PR #121 head f12642d77442256b26da0b0f075d9510a6812435 in workflow 43684334340: 348 passed, 1 failed, 6 skipped. The same cross-process FIFO test timed out waiting for two PostgreSQL waiters. Captured child-process stderr identifies a PostgreSQL DeadlockDetected: one process waits for an advisory lock while the other waits for a RowExclusiveLock during PostgresInferenceAdmissionController.initialize() / _lock_scheduler(). This is stronger evidence of an unresolved lock-ordering/concurrency defect than a mere waiter-poll timeout.

Disposition: the latest PR #117 head is green, but the separate PR #121 run reproduces the same failure class with a deadlock trace. Keep RSK-016 High and CHUNK 06/11 blocked from PASS until the lock-ordering root cause is fixed and repeatable multi-process tests pass. Do not weaken the assertion or increase its timeout to mask the deadlock.

## 6. Score and evidence policy

- The previous baseline's rounded ecosystem production-readiness score remains 45/100. This refresh does not increase it: mainline SHAs are unchanged, PR-only documentation does not change deployed behavior, and no new staging/production, model-artifact, tenant-isolation, recovery, or release-certification evidence was added.
- The previous engineering-maturity estimates (RusaAi 71, RusaModels 78, RAG_Cerdas 70, ecosystem 58) remain historical estimates and are not re-scored here.
- CI success is scoped to the exact SHA and checks. It does not certify a production deployment, zero vulnerabilities, target-device performance, cross-tenant isolation, or end-to-end ecosystem integration.

## 7. CHUNK 00 acceptance and next action

The original 2026-11-14 CHUNK 00 artifact package remains intact. This refresh corrects live PR counts, exact PR heads, CI statuses, and current blockers. The refresh itself is documentation-only.

**Refresh status:** PARTIAL until GitHub Actions completes on the final updated head of RusaAi PR #119. Once that exact head has 3/3 required checks passing, the refreshed CHUNK 00 baseline can be marked PASS.

Next action: verify the final PR #119 head SHA and exact-head CI. Then proceed with CHUNK 01 review of RusaAi #117, RusaModels #24, and RAG_Cerdas #28. Do not merge any PR without the required project-owner approval.
