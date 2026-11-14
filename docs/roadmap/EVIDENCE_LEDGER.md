# RUSA AI Ecosystem — Evidence Ledger

**Snapshot:** 2026-11-14  
**Audit mode:** remote read-only GitHub inspection; no local test execution or production access.

## 1. Exact-main CI evidence

| Repository | Exact main SHA | Check run | Check names / outcome | Evidence |
|---|---|---|---|---|
| RusaAi | `1adac8d340e5870570910168b6626fe285dd2cf2` | `43354051026` | RAG/MCP integration tests: success; Test/lint/security: success; production image build/test/scan: success | [Workflow run](https://github.com/StrategyG/RusaAi/actions/runs/43354051026) |
| RusaAi | `1adac8d340e5870570910168b6626fe285dd2cf2` | `43354050892` | Web build: success | [Workflow run](https://github.com/StrategyG/RusaAi/actions/runs/43354050892) |
| RusaModels | `12c49b1372b230f3656be6e1badeabad870bcfa1` | `43358929126` | Test: success; Windows RUSA-CPU-16 script validation: success | [Workflow run](https://github.com/StrategyG/RusaModels/actions/runs/43358929126) |
| RAG_Cerdas | `1ead461e76d563445d179e7d5e80c25a9ca9f059` | `43312458157` | Tests: success | [Workflow run](https://github.com/StrategyG/RAG_Cerdas/actions/runs/43312458157) |
| RAG_Cerdas | `1ead461e76d563445d179e7d5e80c25a9ca9f059` | `43481726892` | Audit repository and request agent work: success | [Workflow run](https://github.com/StrategyG/RAG_Cerdas/actions/runs/43481726892) |

The RAG_Cerdas exact-main SHA also has successful audit runs `43409058308`, `43351420481` and `43312710333`. The total exact-main check-run counts returned by the Checks API were RusaAi 4, RusaModels 2 and RAG_Cerdas 5.

## 2. Relevant PR-head CI evidence

These checks were queried against the exact PR head SHA returned by GitHub PR metadata.

| Repository / PR | Exact head SHA | Result | Evidence |
|---|---|---|---|
| RusaAi #118 | `202a1c16148753a87114d49085aa052e516fdf52` | 3/3 successful | [Run 43498738170](https://github.com/StrategyG/RusaAi/actions/runs/43498738170) |
| RusaAi #117 | `20087de34f5b8e739fa5a64aae5572ee80aca6a2` | 3/3 successful | [Run 43466465769](https://github.com/StrategyG/RusaAi/actions/runs/43466465769) |
| RusaAi #116 | `faf686ac85875a930221ed92a357a9520a5947e5` | 3/3 successful | [Run 43403193633](https://github.com/StrategyG/RusaAi/actions/runs/43403193633) |
| RusaAi #115 | `d8e7a7c50f1a91a84c75aceb3a139d76eb3168e9` | 3/3 successful | [Run 43358407119](https://github.com/StrategyG/RusaAi/actions/runs/43358407119) |
| RusaAi #82 | `c882928ede520b0e3a8713981f4c956a0d12fece` | 3/3 successful | [Run 42503669112](https://github.com/StrategyG/RusaAi/actions/runs/42503669112) |
| RusaModels #24 | `66a3fab5ea52aa31abfd88f69974938e3db3ec5a` | 2/2 successful | [Run 43466716089](https://github.com/StrategyG/RusaModels/actions/runs/43466716089) |
| RAG_Cerdas #28 | `d2ffed40f3a80be39fdcaf6dbd098da81088b6d6` | 1/1 successful | [Run 43466283245](https://github.com/StrategyG/RAG_Cerdas/actions/runs/43466283245) |
| RAG_Cerdas #16 | `8e8bd2fc40f2d4e609e0d3d3e2bb81181d4e7133` | 3/3 successful | [Conformance run 43179555114](https://github.com/StrategyG/RAG_Cerdas/actions/runs/43179555114), [test run 43179563236](https://github.com/StrategyG/RAG_Cerdas/actions/runs/43179563236), [additional conformance run 43179562994](https://github.com/StrategyG/RAG_Cerdas/actions/runs/43179562994) |

RusaAi #107's PR description reports successful exact-head CI on `9ecd25709e482ce21490ddcb3fbbbb1160562e27` in [run 43100849792](https://github.com/StrategyG/RusaAi/actions/runs/43100849792). Its base is an older main SHA, so that historical result does not establish current-main compatibility.

PRs without a check-run lookup in this audit are marked “not verified” in `BASELINE_INVENTORY.md`. Do not infer CI success from a PR description or from another PR in a stacked branch.

## 3. Source inspection evidence

| Repository | File / source | Git blob SHA | Evidence level | Finding supported |
|---|---|---|---|---|
| RusaAi | `README.md` | `5d62804f8bdb6e85b47ed4082499f14abb489223` | Static inspection | Control Plane, Model Plane boundary, model checkpoint and production-status declarations |
| RusaAi | `.github/copilot-instructions.md` | `1398060c432e51d49e4ae185531c85afb0854233` | Static inspection | P0–P6 gates, no direct main pushes, security and benchmark requirements |
| RusaAi | `rusa_agents/orchestrator.py` | `57978d0fc189977e7734351bc9d29aadcba245fe` | Static inspection | Fixed six-role orchestration pipeline and manager hierarchy |
| RusaAi | `rusa_agents/runtime.py` | `35f82db0a5a859b706e80b1141bf6fff5864116d` | Static inspection | PostgreSQL task runtime and optional KnowledgeService wiring |
| RusaAi | `rusa_agents/autonomous.py` | `e1816fce216dc3935b464fe924f1c468d002b21b` | Static inspection | Bounded JSON tool loop and role allowlist use |
| RusaAi | `rusa_agents/contracts.py` | `520825fb5028773e6e17a747849f357153e6b206` | Static inspection | Current task/result contract shape |
| RusaAi | `rusa_agents/memory.py` | `b390a59acf535be615e4aeb5ffeb0b87ad2b439d` | Static inspection | Tenant-aware task/checkpoint memory |
| RusaAi | `rusa_agents/policy.py` | `78176d15da88c8bc0ba9b47295799ebf7b29c068` | Static inspection | Protected/denied action policy |
| RusaAi | `rusa_agents/tool_gateway.py` | `60edf6afd134921854ac47410813ad1c9fba2b65` | Static inspection | Named tools and bounded Docker sandbox profiles |
| RusaAi | `rusa_knowledge/service.py` | `cc8b0f6dffa0054afd0eb504cd2f2e86730dd26c` | Static inspection | Optional embedding/ingestion/retrieval service and supported vector dimensions |
| RusaAi | `rusa_knowledge/store.py` | `df38bb78238001af169fd4583d478c8391d83ced` | Static inspection | PostgreSQL/pgvector store and tenant/project scope validation |
| RusaAi | `migrations/011_rag_agent_project_scope.sql` | `a719bc1f6c4ab0356bcf302aae76780b3f564f84` | Static inspection | Agent/project scope indexes |
| RusaAi | `migrations/012_rag_knowledge_version_history.sql` | `917792a5f2d08e4df25b81e1b2bd6905d4f7a21c` | Static inspection | Append-only knowledge version snapshots and tenant RLS |
| RusaAi | `docs/PRODUCTION_READINESS.md` | `4b83e3f2c3d7c9356a1c5662fc2e4116a4909692` | Static inspection | Production NO-GO gates and documentation drift on migration status |
| RusaModels | `README.md` | `ebd17c76f6dde7e3047c661939f28578dd2a2b41` | Static inspection | Model Server/Quant Server responsibilities and current model limitations |
| RusaModels | `docs/PRODUCTION_READINESS.md` | `98734073422851a26df31410815b217ca71aac22` | Static inspection | Exact-release evidence requirements and explicit not-ready rule |
| RusaModels | `docs/architecture.md` | `5e4e6ee7fc63a35d6ebb447f281ad5a37731bc48` | Static inspection | Model Plane, registry, runtime and artifact ownership |
| RusaModels | `.github/CODEOWNERS` | Inspected from current main | Static inspection | Owner review requirements for repository and sensitive paths |
| RAG_Cerdas | `README.md` | `22174494c5e12979c4b650e2f58ad0ac9e603e9b` | Static inspection | IM19 pilot status, source-rights and evaluation limitations |
| RAG_Cerdas | `.github/copilot-instructions.md` | `f851e9aa12f6c0882b2caba451eb143ad8328c71` | Static inspection | Read-only/approval constraints and pilot validation requirements |
| RAG_Cerdas | `docs/ADR_0001_CANONICAL_KNOWLEDGE_STORE.md` | `f5498076d120c68b37ae4b41a5958f65c09ca592` | Static inspection | Current no-selection ADR that conflicts with RusaAi's accepted decision |
| RAG_Cerdas | `docs/IM19_VALIDATION_REPORT.md` | `56623f505316df38d5f83d8d293776a6a00beae4` | Static inspection | Historical test evidence and remaining external/production blockers |
| RAG_Cerdas | `docs/IM19_RUNBOOK.md` | `9290d58973902a405e2d0077ee69c1dceab9254a` | Static inspection | Staging, reviewer identity, rights, worker and rollback requirements |

## 4. Evidence limitations

- No local clone or working tree was available; local modifications are not verified.
- No local tests were executed by ChatGPT during CHUNK 00.
- No live database, Dropbox, Supabase, S3, secret manager, model runtime, staging host or production host was accessed.
- CI success is not a substitute for staging, target hardware, human-reviewed quality evaluation, security review, backup/restore or rollback evidence.
- Branch protection was read from the GitHub branch metadata; all three `main` branches returned `protected=false`.
- Combined commit-status lookup returned no status entries; GitHub Checks API results above are the authoritative CI evidence used in this ledger.

## 5. CHUNK 00 evidence decision

**Status: PASS for the read-only baseline gate.** The baseline records exact main SHAs, check-run evidence, source blob SHAs, repository structure, relevant PRs and explicit unknowns. No source code or production state was changed.

## 6. CHUNK 01 branch verification snapshots — 2026-11-14

The following statuses were queried against the exact current PR head SHA. A running check is not counted as PASS.

PR #119 is the self-updating tracker PR. Its row below records an earlier exact-head snapshot (`fded768e`); subsequent tracker commits advanced its head. Always read the latest PR SHA and checks from live metadata before review or merge.

| Repository / PR | Exact head SHA | Current check result | Workflow evidence |
|---|---|---|---|
| RusaAi #117 | `251a7d1f345e82c8b6400185ce5160dac5f4cdbd` | 3/3 PASS; raw Trivy HIGH findings remain | [Run 43552929727](https://github.com/StrategyG/RusaAi/actions/runs/43552929727) |
| RusaAi #119 | `fded768e80f1b0bc0257249f60a5b2b56a4cfe6c` | Test/lint/security PASS; RAG/MCP PASS; image build/scan IN_PROGRESS | [Run 43552911148](https://github.com/StrategyG/RusaAi/actions/runs/43552911148) |
| RusaAi #120 | `ba04a0539f88dcdb91b3b63246353f72fac99d97` | 3/3 PASS on workflow attempt 2 | [Run 43504174547](https://github.com/StrategyG/RusaAi/actions/runs/43504174547) |
| RusaModels #24 | `128343fee6973fe19158182a4c8b40c5be5a72c2` | 2/2 PASS; raw Trivy HIGH findings remain | [Run 43553234278](https://github.com/StrategyG/RusaModels/actions/runs/43553234278) |
| RAG_Cerdas #28 | `65752d94c93f8c098792bf4ec46a36f269d3de93` | 1/1 PASS | [Run 43551700624](https://github.com/StrategyG/RAG_Cerdas/actions/runs/43551700624) |
| RusaAi #116 | `faf686ac85875a930221ed92a357a9520a5947e5` | 3/3 PASS; raw HIGH Trivy findings remain | [Run 43403193633](https://github.com/StrategyG/RusaAi/actions/runs/43403193633) |

### Intermittent FIFO test evidence

- RusaAi PR #120 exact head `ba04a0539f88dcdb91b3b63246353f72fac99d97`: workflow 43504174547 attempt 1 failed `test_fifo_admission_is_shared_across_separate_os_processes`; attempt 2 passed 337 tests with 6 skipped.
- RusaAi PR #117 superseded head `e377c268e7356ece82ccd1dbc9c5ad1735e61070`: workflow 43552467511 had the same test failure (the image job was cancelled after the test failure).
- RusaAi PR #117 current head `251a7d1f345e82c8b6400185ce5160dac5f4cdbd`: workflow 43552929727 passed 337 tests with 6 skipped. This is evidence of a passing run, not proof that the concurrency race is eliminated.

Skipped tests in the passing Python run: optional document parser dependencies unavailable; `RUSA_TEST_DATABASE_URL` not configured for the separate knowledge PostgreSQL test; optional MCP runtime unavailable; PyTorch unavailable for tokenization; optional local-model dependencies unavailable. These are not silently counted as executed tests.

### Current source inspection for the cross-repository contract inventory

| Repository | Source at ref | Blob SHA | Verified detail |
|---|---|---|---|
| RusaAi | `llm-project/serve/app.py` on main | `9058de7699c0a1312f4c0e8beb9a2b8e408b3bcc` | Tenant-authenticated `GET /v1/models` and `POST /v1/chat/completions` gateway routes |
| RusaAi | `.env.example` on main | `50408bde27cb6a620bdf9fa1859f9dd939881a6a` | Generic default `RUSA_WORKER_INFERENCE_GATEWAY=false`; direct Model Server base URL is configurable |
| RusaAi | `docker-compose.cpu.yml` on main | `a190706606b961b39632d1cf76568a440a2c99c6` | CPU profile enables gateway mode and clears direct worker Model Server URL/key |
| RusaModels | `rusamodels/api.py` on main | `bd5e660c35c66b96e7e0733d89c4439f75bf2c9e` | Authenticated `/openai/v1/models`, `/openai/v1/chat/completions`, `/openai/v1/completions`, `/openai/v1/embeddings`; inference role accepted |
| RusaModels | `rusamodels/api.py` on PR #24 head | `69f55d6769d4844c8b78294caa49fe436a160294` | FastAPI description corrected to identify RusaModels as Model Plane and RusaAi as ecosystem Control Plane |
| RusaAi | `docs/architecture/ECOSYSTEM_ARCHITECTURE_RECONCILIATION_v1.0.md` on PR #117 head | `909f11556754d44bef668ffa33d5a8fddf2f4515` | Service ownership, data-flow contracts, cross-repository interface inventory, data classifications, ADR reconciliation and release boundaries |
| RAG_Cerdas | `docs/ADR_0002_RUSAAI_CANONICAL_KNOWLEDGE_STORE_ALIGNMENT.md` on PR #28 head | `a3679667aeb29bb5c3e7b98c9a292ac01f1a34ba` | IM19 remains isolated; no cross-read/write, dual-write, mirroring or bulk synchronization authorized |
| RusaModels | `.trivy/setuptools-vex.openvex.json` on main | `f39d7163fd1fe9d4156b90b6a0235db0e88c912e` | Narrow VEX statement for `setuptools@70.3.0` / CVE-2025-47273; runtime package is asserted as 84.2.1 |


RusaModels PR #24 workflow 43553234278 completed 2/2 checks. Its raw Trivy scan reports 47 HIGH OS-package findings plus 1 HIGH Python-package finding in each of the Model Server and worker images, with 0 CRITICAL. The VEX statement is scoped only to the stale `setuptools@70.3.0` SBOM component; CI verifies runtime `setuptools` 84.2.1 before applying it. The gate also uses `--ignore-unfixed`, so the green result does not establish zero HIGH findings. See RSK-019 in `OPEN_RISKS.md`.

PR #117's image scan in workflow 43552929727 completed with raw Trivy HIGH counts 103/118/117/103 across the API, agent-sandbox, worker and RAG/MCP images respectively, with 0 CRITICAL. The blocking gate passed with `--ignore-unfixed`; these findings remain security debt and are not an all-clear result.

### Relative-link validation

A repository-tree-based check of changed Markdown files found no broken relative links on the inspected PR heads:
- RusaAi PR #117: 6 Markdown files, 8 relative links, 0 broken.
- RusaModels PR #24: 3 Markdown files, 12 relative links, 0 broken.
- RAG_Cerdas PR #28: 6 Markdown files, 34 relative links, 0 broken.

This check validates relative file targets only; it does not prove external HTTP links are reachable or semantically correct.


## 7. Latest exact-head refresh — 2026-11-14

| Repository / PR | Exact head SHA | Exact-head CI | Evidence |
|---|---|---|---|
| RusaAi #119 | `5772c23c1cb7c6ec738925ce5b4663c79e17e029` | 3/3 PASS: Test/lint/security, production image build/scan, and RAG/MCP integration | [Workflow 43554368639](https://github.com/StrategyG/RusaAi/actions/runs/43554368639) |
| RusaAi #117 | `251a7d1f345e82c8b6400185ce5160dac5f4cdbd` | 3/3 PASS | [Workflow 43552929727](https://github.com/StrategyG/RusaAi/actions/runs/43552929727) |
| RusaModels #24 | `128343fee6973fe19158182a4c8b40c5be5a72c2` | 2/2 PASS in latest run | [Workflow 43554181866](https://github.com/StrategyG/RusaModels/actions/runs/43554181866) |
| RAG_Cerdas #28 | `65752d94c93f8c098792bf4ec46a36f269d3de93` | 1/1 PASS | [Workflow 43551700624](https://github.com/StrategyG/RAG_Cerdas/actions/runs/43551700624) |

### PR #119 latest image-scan detail

The completed image-scan job is job `132323826446` in workflow `43554368639`. Trivy reported raw HIGH findings by image: API 103, agent-sandbox 118, worker 117, and RAG/MCP 103; CRITICAL 0. The job's blocking command uses `--ignore-unfixed`. A green gate therefore does not mean zero HIGH findings or a clean security certification. These counts are scanner findings and are not, by themselves, confirmation of exploitability. The images were tagged with source revision `fa73d8ab89f17712e0064389a565ff32aa99ab5d`.

### Current PR review state

At the inspected snapshot, RusaAi #117, RusaModels #24, and RAG_Cerdas #28 are all open, marked ready for review, and reported mergeable. No review submissions or inline review threads were present. None has been merged. The exact base SHAs remain the three recorded main SHAs in Section 1; the candidate changes are not yet mainline.

### CHUNK 00 PR scope verification

RusaAi PR #119 changes exactly six files: `docs/roadmap/ARCHITECTURE_DECISIONS.md`, `BASELINE_INVENTORY.md`, `EVIDENCE_LEDGER.md`, `EXECUTION_STATE.md`, `OPEN_RISKS.md`, and `REPOSITORY_DEPENDENCY_MAP.md`. The changed-file list contains no source-code, migration, test, model-artifact, deployment, or production-data path.


## 8. CHUNK 01 documentation-status refresh — 2026-11-14

RAG_Cerdas PR #28 advanced to head `7480bb2fcf6139f283fada2c4a068747549fc709` after correcting the parent RusaAi PR #117 status in `docs/RUSA_AI_ECOSYSTEM_ARCHITECTURE_RECONCILIATION_v1.0.md`. The corrected text now says PR #117 is open and ready for review but not merged. The PR remains documentation-only; its exact-head test workflow 43557286413 completed 1/1 PASS. The six changed files remain the same six documentation/instruction files; no runtime or data operation was introduced.

Immediately before this tracker refresh, RusaAi PR #119 head `e41ea1d9fb2304296edbd7fa2be650ae08b458be` had two checks complete successfully and its production-image build/scan job still in progress in workflow 43557157903. Because PR #119 is a self-updating tracker, this ledger records that pre-refresh observation; live PR metadata is authoritative after the new commit.
