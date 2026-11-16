# RusaAI Adaptive Learning Engine — Component Map

**Version:** 1.0  
**Status:** Proposed target mapped to observed mainline components  
**Baseline date:** 2026-11-14

This map distinguishes **observed code** from **target components**. A target component is not implemented merely because it appears in this document.

## 1. Ownership map

| Capability | Canonical owner | Existing evidence | ALE target |
|---|---|---|---|
| Task checkpoint memory | RusaAi | `rusa_agents/memory.py`, worker runtime/orchestrator | Keep as execution state; do not call it long-term learning memory |
| Persistent episodic/semantic/preference memory | RusaAi | Not established by the inspected task-memory schema | Add scoped, governed memory lifecycle |
| Canonical Agent/Worker knowledge | RusaAi | `rusa_knowledge`, migrations 007/009/011/012, ADR-0002 | Extend rights, review, publication, retention, deletion, and evidence gates |
| Learning policy/approval | RusaAi | `rusa_agents/policy.py` covers action risk/approval | Extend to learning candidates and release authority |
| Experience ledger | RusaAi | Audit/task records exist; no dedicated learning outcome ledger identified | Add minimal, redacted, append-oriented task outcome events |
| Learning candidate registry | RusaAi | Not identified on main | Add candidate types and state machine |
| Agent skills/workflow versions | RusaAi | Existing worker/tool/runtime contracts; new Agent Runtime work is PR-only | Versioned skill registry, test, approve, activate, rollback |
| Source discovery and knowledge mining | RAG_Cerdas | IM19 modules and source-rights/reviewer migrations | Keep isolated; emit immutable reviewed export package only after ADR |
| Retrieval evaluation labels | RAG_Cerdas | Human-attestation evaluator and three unreviewed cases | Build human-reviewed qrels and frozen benchmark |
| Dataset eligibility and export | RusaAi | Existing SFT validation code; no complete approved export manifest observed | Own rights/consent/scope filtering and immutable export manifest |
| Training execution | RusaModels | QLoRA trainer, validator, evaluator | Single canonical offline training plane |
| Model registry/artifacts | RusaModels | Immutable manifests and SHA-256 artifact checks | Extend release lineage and signed evidence |
| Inference/embedding runtime | RusaModels | Model and embedding services | Serve only approved release IDs/digests |
| Model promotion policy | RusaAi + RusaModels | Existing model lifecycle and RusaAi action policy; end-to-end ALE approval not established | RusaAi authorizes; RusaModels enforces immutable release transition |

## 2. Proposed trust flow

```text
Client
  |
  v
RusaAi identity + trusted tenant/project context
  |
  v
Policy / Agent / Task Queue
  |
  +--> scoped memory and canonical rusa_knowledge
  |
  +--> Model Access Gateway --> RusaModels inference/embedding
  |
  +--> Tool Gateway (server-side permission checks)
  |
  v
Response, citations, audit event
  |
  v
Asynchronous Experience Ledger
  |
  v
Sanitize / redact / deduplicate / classify
  |
  v
Learning candidate registry
  |
  +--> memory candidate --> approval --> RusaAi memory
  |
  +--> knowledge candidate --> rights/review/publication --> rusa_knowledge
  |
  +--> skill candidate --> tests + approval --> versioned Agent skill
  |
  +--> training example --> rights approval + immutable dataset snapshot
                                 |
                                 v
                          RusaModels offline training
                                 |
                                 v
                          frozen evaluation + safety
                                 |
                          explicit release approval
                                 |
                          canary + rollback
```

IM19 remains a separate source/knowledge-intelligence pilot. It may produce an immutable export package, but the RusaAi import adapter must remain disabled until a separate ADR, security review, rights revalidation, schema contract, and rollback tests pass.

## 3. Runtime versus learning boundary

**Online inference path**
- Authenticate and derive trusted scope.
- Retrieve only approved, active, scoped knowledge/memory.
- Call an exact approved RusaModels release.
- Authorize every tool call in RusaAi independently of model output.
- Return response and required citations.
- Enqueue a minimal allowed outcome event asynchronously.

**Offline learning path**
- Process only policy-eligible events.
- Produce candidates; never auto-publish by default.
- Validate rights, scope, provenance, and quality.
- Require approval for shared or high-impact changes.
- Train only from immutable, approved dataset snapshots.
- Evaluate, approve, canary, and preserve rollback.

No model weights change during inference.

## 4. Contract boundaries to define in ALE-01

| Contract | Provider | Consumer | Required identity |
|---|---|---|---|
| Memory read/write | RusaAi | RusaAi agents/workers | Trusted actor + tenant/project |
| Experience event | RusaAi | ALE workers/evaluators | Actor, tenant, task, trace, retention |
| Learning candidate | RusaAi | Reviewer/policy engine | Candidate ID, scope, source digest, policy version |
| Knowledge export | IM19 | RusaAi import adapter | Exact source revision/hash, rights decision, reviewer, export digest |
| Dataset manifest | RusaAi | RusaModels | Immutable dataset digest, rights approvals, scope, splits |
| Training job | RusaModels | RusaAi orchestrator | Authorized job ID, budget, base model digest |
| Model release | RusaModels | RusaAi Model Gateway | Immutable manifest digest, approval, rollback target |
| Skill release | RusaAi | Agent Runtime | Version, tests, approval, compatibility |

Every write contract needs a schema version, request/trace IDs, idempotency key, deadline/cancellation behavior, typed errors, and a deprecation policy. Tenant scope must come from trusted identity claims and server-side policy, not an arbitrary request field.

## 5. Non-negotiable boundaries

- RusaAi owns the canonical `rusa_knowledge` writer and operational memory policy.
- RusaModels owns model weights, artifact registry, inference, embedding, quantization, and training execution.
- RAG_Cerdas owns IM19 source discovery, rights evidence, extraction, mining, review, and retrieval experiments.
- No shared database ownership.
- No direct RusaAi access to RusaModels internal DB, storage credentials, or filesystem.
- No IM19 cross-read/cross-write/dual-write without formal approval.
- No raw conversation auto-training.
- No model-generated fact is trusted knowledge without independent evidence and required review.
- No agent may self-approve or grant itself permissions.
