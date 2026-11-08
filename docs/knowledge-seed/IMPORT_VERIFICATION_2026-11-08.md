# RUSA AI Knowledge and SFT Import Verification — 2026-11-08

## Executive result

The supplied RAG bundle's file integrity and 20-agent extraction map were independently checked against the repository's pinned manifests. The separate SFT candidate was structurally screened but deliberately not promoted.

| Check | Result | Meaning |
|---|---:|---|
| Original source files | 4/4 hashes and byte counts match | Integrity only; not factual or implementation verification |
| Agent section profiles | 20/20 raw section hashes match | Exact source-heading extraction |
| Agent pack previews | 20/20 provenance frontmatters match | Preview artifacts are consistent with their manifest |
| Expected shared-source chunks | 81 | Current 1200-character / 180-character overlap chunker |
| Expected normalized agent-pack chunks | 60 | Current miner's normalized section text and chunker |
| SFT candidate records | 48 | 8 per currently supported runtime role |
| SFT candidate action-contract screen | 0 violations found | Manual screen against current role tool allowlists, arguments, check profiles and protected paths |
| SFT candidate duplicate IDs | 0 | ID uniqueness only; human semantic deduplication remains required |
| Limited common-secret-pattern scan | 0 matches | Not a substitute for full DLP/secret scanning |

**No database ingestion, embedding generation, model training, or adapter export occurred as part of this verification.**

## RAG source archive

- Archive: `RUSA_AI_RAG_Source_and_20_Agent_Packs.zip`
- Archive SHA-256: `1568fd0603f2a5ee348069375b5fe5027b010244d6cc3aafa1747eef495954e3`
- Project: `rusa-ai`
- Source manifest version: `1.0.0`

| Source ID | File | Bytes | SHA-256 |
|---|---|---:|---|
| RUSA-ARCH-001 | `RUSA_AI_Architecture.md` | 390 | `e47b9d89c672d326563d581f97f2bdbaa0b1e4b4b0f42df898fadef390d59766` |
| RUSA-AGENT-001 | `RUSA_AI_All_Agents-2.md` | 29,837 | `43da306c109b1f768586fc2ff518a13f246a4d6543bce449dab8fef7e309e8e5` |
| RUSA-COMM-001 | `ADR-RUSA-AI-Komunikasi-Agent(1).md` | 26,490 | `1679cbf22c1d0d2ad5647ac2183f845fd10a5d8b3b8a658bee7b8dd2f4feab35` |
| RUSA-DEVOPS-001 | `RUSA_AI_AGENTIC_DEVOPS_LIFECYCLE(1).md` | 31,527 | `8a059de15bcb144163f365c794f5642de0e6b15ecadd4a545452939a61548ada` |

All four source files also match the uploaded originals byte-for-byte. The architecture overview is only 390 bytes; it is intentionally treated as a short overview, not a detailed specification.

## Agent pack preflight estimate

The archive contains ten canonical agent packs, eight lifecycle-agent packs, and two cross-cutting DevOps packs. Counts below use the current RUSA 1200/180 chunking strategy after the same whitespace normalization used by the miner.

| Agent ID | Chunks |
|---|---:|
| supreme-orchestrator | 4 |
| system-architect | 3 |
| planner | 3 |
| task-decomposer | 3 |
| software-engineer | 3 |
| devops | 3 |
| qa-testing | 3 |
| security-policy | 3 |
| memory-knowledge | 3 |
| evaluation-reporting | 3 |
| rusa_plan_agent | 2 |
| rusa_code_agent | 2 |
| rusa_build_agent | 2 |
| rusa_test_agent | 2 |
| rusa_release_agent | 2 |
| rusa_deploy_agent | 2 |
| rusa_operate_agent | 2 |
| rusa_monitor_agent | 2 |
| rusa_devops_orchestrator | 8 |
| rusa_security_guard_agent | 5 |
| **Total** | **60** |

The archive's `section_sha256` records hash the raw extracted Markdown section. The current repository miner normalizes whitespace before computing the ingestion section hash. The new `rusa-knowledge-bundle` verifier reports both digests explicitly; they must not be conflated.

## Source status is unchanged

The four source documents remain marked `UNVERIFIED` and `NOT_VERIFIED` for implementation. The ADR retains its proposed status (`Diusulkan`), and the Agentic DevOps lifecycle retains `Proposed Architecture`. Hash verification proves file integrity, not that the design is correct, approved, or deployed.

## SFT candidate screening

- Archive: `RUSA_AI_Engineering_SFT_Candidate_v1.zip`
- Archive SHA-256: `3ff123c7c391648b18ea353976d37b9f699ab6a4bdedfa6a3b81390e9a00c333`
- JSONL: `engineering_candidate.jsonl`
- JSONL SHA-256: `c16c1eaf4e072a77715b4a9310f772c7b8984a0942d9b21a3fd9641358bc3ad1`
- Records: 48 (8 each for analyst, architect, developer, debugger, tester, security)
- Runtime tool-contract screen: no violations found
- Duplicate IDs: none
- Limited common credential-pattern scan: no matches

The supplied provenance explicitly labels this dataset `CANDIDATE / SYNTHETIC / NOT HUMAN-REVIEWED / NOT APPROVED FOR TRAINING PROMOTION`. It is not production telemetry, is not a held-out evaluation set, and must not be merged into the current 120/36 baseline until every record is reviewed and a new versioned split is approved.

The repository's QLoRA config remains pinned to `Qwen/Qwen3-4B-Instruct-2507` revision `eb25fbe4f35f7147763bc24445679d1c00588d89`. Training requires a CUDA GPU. No model weights are produced by this review.

## Next controlled steps

1. Run `rusa-knowledge-bundle --archive <zip>` in the Codespace.
2. Run the same command with `--extract` after verification.
3. Run `python -m rusa_knowledge.seed` and `python -m rusa_knowledge.agent_mining` in dry-run mode.
4. Review the credential scan, source statuses, chunk counts, tenant scope, and embedding cost.
5. Apply shared and agent-specific documents separately using an existing authorized non-production tenant.
6. Run tenant/agent/project isolation tests and a human-reviewed retrieval gold set before enabling worker RAG.
7. Keep the SFT candidate separate until human review, deduplication against the baseline, and an approved new train/eval split are complete.
