# RusaAI Adaptive Learning Engine — Open Risks

**Snapshot:** 2026-11-14  
**Status:** Active baseline risk register

| ID | Risk | Severity | Evidence | Required action / owner |
|---|---|---|---|---|
| ALE-R01 | Task memory is mistaken for persistent learning memory | High | `rusa_agents/memory.py` stores task/repository/state/payload; orchestrator persists task outcomes | RusaAi: define memory classes, lifecycle, provenance, expiry, revocation, deletion |
| ALE-R02 | Training implementation exists in both RusaAi and RusaModels | Critical architecture risk | RusaAi `rusa_training` and `llm-project/train`; RusaModels `rusamodels/training` | Architecture owners: make RusaModels the sole training executor; RusaAi owns eligibility/export |
| ALE-R03 | SFT seed is synthetic and no trained weights are evidenced | High | RusaModels `data/agent_sft/PROVENANCE.md`, `docs/TRAINING.md` | RusaModels: obtain authorized reviewed data; run controlled experiment; evaluate and retain digest |
| ALE-R04 | Training dataset manifest lacks full rights/consent lineage in inspected example schema | High | SFT record contract is runtime task JSON; no full rights approval envelope in record schema | RusaAi: enforce eligibility and immutable dataset manifest; RusaModels: fail closed |
| ALE-R05 | RusaAi canonical knowledge lifecycle lacks complete source-rights/review/publication/deletion gates | Critical | RusaAi ADR-0002 explicitly records missing lifecycle controls | RusaAi: implement after ADR/security acceptance |
| ALE-R06 | IM19 is not tenant-isolated and must not be exposed as a multi-tenant API | Critical | IM19 architecture/retrieval docs state private worker boundary | RAG_Cerdas: keep isolated; no public exposure or cross-tenant serving |
| ALE-R07 | IM19 rights quarantine is not erasure; purge/retention is not implemented | High | IM19 source-rights/runbook | RAG_Cerdas: approve and implement auditable retention/purge before production use |
| ALE-R08 | Retrieval benchmark has no human-reviewed qrels | High | Three `eval_cases.json` cases await human review | RAG_Cerdas: obtain genuine human labels; freeze corpus/evaluator hashes |
| ALE-R09 | IM19 deployment and endurance evidence absent | High | IM19 runbook says worker example not built/deployed or endurance-tested | RAG_Cerdas: isolated staging deployment and recovery tests |
| ALE-R10 | Main branches are unprotected | High | GitHub branch metadata reports `protected=false` for all three repos | Repository owners: configure required reviews/checks and verify rulesets |
| ALE-R11 | RusaAi and RAG_Cerdas lack CODEOWNERS in inspected main trees | Medium | Recursive tree inventory | Repository owners: add owners for architecture, security, migrations, training/export |
| ALE-R12 | RusaAi PR backlog may hide conflicting/superseded migrations and architecture | High | 82 open PRs; relevant #82/#109/#115–117 etc. | RusaAi maintainers: full diff/dependency/migration/CI disposition |
| ALE-R13 | Duplicate ADR-0002 identifiers in RusaAi | High | Two mainline documents both named ADR-0002 | Architecture owners: reconcile identifiers and references before new ADR numbering |
| ALE-R14 | Current model manifest does not explicitly bind chat-template digest | Medium | `ManifestTokenizer` records tokenizer id/version; trainer validates chat_template | RusaModels: add release lineage for chat template/preprocessing in a versioned schema |
| ALE-R15 | No end-to-end ALE candidate-to-release evidence exists | Critical | No integrated memory/experience/candidate/dataset/training/promotion path found on main | All owners: implement through ALE-01–10 and certify staging |

## Risk handling

- Critical risks block production or cross-repository activation.
- High risks block the affected learning channel or pilot scope.
- Medium risks require a tracked remediation plan and explicit disposition before production certification.
- Do not lower severity to make a chunk pass.
- Every risk closure must link a commit, test, evidence artifact, and approver.
