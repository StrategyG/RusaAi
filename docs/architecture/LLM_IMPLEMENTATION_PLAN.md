# RUSA-AI LLM Implementation Plan

- **Plan ID:** RUSA-LLM-PLAN-001
- **Status:** Proposed; execution evidence must be attached per phase
- **Parent architecture:** [LLM Platform Architecture](LLM_PLATFORM_ARCHITECTURE.md)

## Operating model

Use a dedicated LLM workstream. Keep changes additive until the target package, tests, Docker commands, and downstream imports are migrated. Every implementation task must be scoped to a feature branch and merged through reviewed pull requests.

### Work packages

| ID | Package | Depends on | Deliverables | Exit criteria |
|---|---|---|---|---|
| LLM-001 | Config contracts | None | Model/profile JSON Schemas, YAML profiles, contract tests | All configs validate; invalid geometry is rejected |
| LLM-002 | Package foundation | LLM-001 | `src/rusa_llm`, packaging metadata, import contract | Clean install and import smoke test |
| LLM-003 | Data contracts | LLM-002 | Dataset manifest schema, provenance interfaces | Manifest validation and immutable snapshot test |
| LLM-004 | Tokenizer baseline | LLM-002, LLM-003 | Tokenizer interface, registry, evaluation | Round-trip and multilingual regression tests |
| LLM-005 | Model components | LLM-002 | Config-driven Transformer components | Shape, gradient, numerical, and checkpoint tests |
| LLM-006 | Training foundation | LLM-003, LLM-004, LLM-005 | Fine-tuning trainer, checkpoint/resume | Small deterministic run and resume test |
| LLM-007 | Evaluation harness | LLM-004, LLM-005 | Benchmark adapters, safety and regression suites | Reproducible report bound to checkpoint hash |
| LLM-008 | Inference runtime | LLM-005, LLM-007 | Real model loader, API adapter, batching | Real checkpoint smoke test and bounded load test |
| LLM-009 | Agent model gateway | LLM-008 | Typed provider adapter for `rusa_agents` | Tool policy remains outside model runtime |
| LLM-010 | Production certification | LLM-007, LLM-008, LLM-009 | Security, capacity, recovery, operations evidence | All release gates signed off |

## Required engineering workflow

1. Architect reviews the work package and dependency map.
2. Developer changes only approved paths and adds tests with the implementation.
3. Tester runs unit, contract, integration, and regression checks.
4. Security reviewer checks data handling, dependencies, secrets, and execution boundaries.
5. CI evidence must match the exact PR head SHA.
6. Pull request remains open until all required checks and human reviews pass.
7. Merge, deployment, release, production data writes, and permission changes remain human-controlled.

## Minimum evidence bundle

Each work package must publish:

- Task ID and scope.
- Files changed and reason.
- Config/schema versions and hashes.
- Tests run, command, environment, and results.
- Security review notes.
- CI run URL and exact commit SHA.
- Known limitations and remaining risks.
- Rollback or compatibility note where applicable.

## First implementation sprint

### Sprint A — Configuration contract

- [x] Define model architecture and system boundaries.
- [x] Define target repository layout and staged migration.
- [x] Define model and runtime profile schema.
- [x] Define proposed local, single-GPU, and multi-GPU profiles.
- [x] Add contract tests for current model configuration and profile invariants.
- [ ] Verify the GitHub Actions run on the exact PR head.
- [ ] Obtain architecture review and merge approval.

### Sprint B — Importable package foundation

- [ ] Introduce `src/rusa_llm` and packaging metadata without breaking the current serving app.
- [ ] Add deterministic configuration loader and typed config models.
- [ ] Add CPU-only import, config, and shape smoke tests.
- [ ] Pin/lock runtime and development dependencies in a reviewed change.
- [ ] Update Docker and CI to install the package rather than relying on repository-root imports.

### Sprint C — Model component baseline

- [ ] Implement embedding, RoPE, RMSNorm, GQA, SwiGLU, and dense decoder block.
- [ ] Implement MoE router and expert dispatch behind an explicit feature flag.
- [ ] Add reference implementation tests before optimized kernels.
- [ ] Add gradient, numerical stability, and checkpoint round-trip tests.
- [ ] Do not attempt the full 96-layer target until the small model passes.

## Stop conditions

Stop and return to architecture review if:
- A configuration cannot be tied to a versioned schema.
- Dataset license/provenance is missing.
- A checkpoint lacks a checksum or tokenizer identity.
- Hardware budget is not established for a requested training run.
- Inference would return mock output as real output.
- A model-generated action bypasses server-side authorization.
- CI evidence is missing, stale, or references a different commit SHA.

**Readiness rule:** phase completion is based on attached evidence, not a boolean status written by an agent.
