# ADR-0003 — NEXUS Foundation and Separate Agent Model Architecture

- Date: 2026-11-15
- Status: **Accepted architecture direction; implementation and training are gated**
- Decision owner: RUSA AI Project Owner
- Scope: NEXUS model family across RusaAi (Control Plane) and RusaModels (Model Plane)
- Supersedes: the pretrained-checkpoint selection direction in `ADR-TRAIN-001`; it does not approve any model artifact or production release

## 1. Context

RUSA AI is intended to be an independently controlled AI product and ecosystem with its own model family. The repository split remains authoritative: RusaAi owns orchestration and policy; RusaModels owns model identities, training/artifacts, inference, conversion, quantization, and release lifecycle.

The supplied NEXUS v5.8.2 package contains a specialist DevOps/SRE system and an Argon LLM checkpoint. Its model card states an advisory DevOps/SRE intended use. The package reports 887,434 Argon parameters, 2,100 unique incident records, 50,000 derived chunks, 3.2 million memmap tokens, 80 Argon LLM optimizer steps, 160 memmap samples consumed, and 235,360 tokens processed by the optimizer. These are package-reported figures and have not been independently reproduced in this ADR. The 50,000 DevOps chunks do not establish a general-purpose Foundation pretraining corpus or a general-purpose Foundation model.

The package's independent-assurance disposition records external assessor sign-off and external live-cluster attestation as `NOT_VERIFIED`, and limits the current authorized mode to controlled staging / supervised pilot. Argon must therefore remain a specialist candidate and must not be relabeled as a Foundation or Agent model.

The current RusaModels mainline has a legacy CUDA-only QLoRA SFT path with Qwen configuration. That workflow is not the NEXUS model path. RusaModels also has a from-scratch CPU Tiny trainer, dataset-engine, benchmark-engine, and tokenizer/architecture work in open PRs; those PRs are not merged into main and do not prove a production-scale NEXUS training pipeline. In particular, the Carbon-Tiny reference is a 3.2M-parameter engineering prototype, not evidence that a larger Foundation can be pretrained on the same compute profile.

## 2. Decision

### 2.1 NEXUS Foundation LLM

1. Build NEXUS Foundation **from scratch**, using random initialization and causal next-token pretraining.
2. Do not load, continue-pretrain, distill from, or derive Foundation weights from Qwen or another unapproved pretrained checkpoint.
3. Foundation receives its own model ID, architecture configuration, tokenizer identity, corpus lineage, training runs, checkpoints, evaluation suite, model card, and immutable release manifest.
4. Its exact parameter budget, layer/hidden dimensions, context length, tokenizer vocabulary, corpus token target, precision, and compute profile remain open until a separate architecture and capacity review freezes them.
5. Do not silently adopt the planned 96-layer / 12,288-hidden blueprint or the separate RusaAi-Edge 1.54B reference as the NEXUS Foundation architecture. Any adoption requires an explicit, versioned model-design decision and a resource-feasibility record.

### 2.2 NEXUS Agent LLM

1. Build and operate the Agent LLM as a **separate model**, not as a LoRA-only adapter, alias, or shared-weight variant of Foundation.
2. Give it a distinct model ID, architecture/tokenizer compatibility record, checkpoint, training lineage, evaluation suite, model card, and release manifest.
3. Its initialization strategy remains `OPEN_DECISION`: a separate from-scratch model or a separately approved non-Qwen starting point may be considered only after model identity, lineage, license, data, compute, and evaluation review.
4. Qwen-derived weights, tokenizer, teacher model, or hidden dependency are prohibited.
5. The Agent LLM may propose plans and structured tool calls. It never grants permissions or authorizes execution. RusaAi identity, Policy Engine, approvals, Tool Gateway, sandbox, and audit remain authoritative.

### 2.3 Model-plane and control-plane ownership

- RusaModels owns both NEXUS model identities, training pipelines, checkpoints, tokenizer artifacts, manifests, evaluation records, inference runtimes, quantization, and release lifecycle.
- RusaAi owns agent/task orchestration, trusted identity, policy, tool authorization, human approval, sandboxing, memory/context management, and audit.
- RusaAi consumes the two model roles through the authenticated Model Access Gateway and versioned RusaModels API. Workers do not receive model-store credentials or direct private-runtime access.
- Foundation and Agent are promoted independently. Passing one model's gates does not approve the other.

### 2.4 Existing NEXUS Argon assets

Keep NEXUS Argon v5.8.2 on a separate DevOps/SRE specialist track. Preserve its checkpoints and evidence; do not overwrite, rename, or promote those artifacts as either NEXUS Foundation or NEXUS Agent. Reuse of its data or code requires a new dataset/source review, model-identity check, and evaluation.

## 3. Logical model identities

The following are proposed logical registry IDs only; they are not production runtime IDs:

| Logical ID | Role | Initialization | Release status |
|---|---|---|---|
| `nexus-foundation` | General-purpose Foundation LLM | Random initialization / from scratch | Design direction accepted; architecture and artifacts pending |
| `nexus-agent` | Agent planning and tool-use LLM | `OPEN_DECISION`, but separate and non-Qwen | Design direction accepted; architecture and artifacts pending |
| `nexus-argon-devops` | DevOps/SRE specialist | Existing v5.8.2 lineage, subject to re-verification | Specialist candidate only; not Foundation/Agent |

Each immutable release version must bind model ID/version, architecture-config digest, tokenizer digest, dataset-release hashes, training source revision, dependency/SBOM digest, run configuration, seed, compute profile, checkpoint SHA-256 values, signatures, model card, and evaluation report digests.

## 4. Training and data strategy

### 4.1 Foundation

1. Freeze architecture, parameter count, context length, tokenizer interface, and compute envelope.
2. Build or train the tokenizer only from the approved Foundation training split; freeze its files and digests before model pretraining.
3. Pretrain from random initialization on a diverse, rights-cleared, quality-reviewed general-purpose corpus. Measure tokens actually consumed by the optimizer.
4. Run separately governed instruction-tuning and safety/alignment stages only after the base checkpoint has its own frozen evaluation.
5. Keep specialist DevOps/SRE adaptation and retrieval distinct from general Foundation pretraining.

### 4.2 Agent

The Agent corpus must represent agent behavior: task interpretation, decomposition, planning, tool selection, structured arguments, observation handling, bounded retry, recovery, verification, refusal/abstention, escalation, and human-approval requests. Include safe and adversarial tool-use cases. Training examples must not teach the model that generating a tool call grants permission to execute it.

SFT is the initial candidate method. Preference/alignment optimization may be proposed later only with an approved objective, preference provenance, independent evaluation, and a separate authorization. Any adapter used internally for Agent training must attach to the Agent's own approved base and must not replace the separate Agent checkpoint identity.

### 4.3 Corpus separation and the 50K DevOps set

Maintain separate immutable releases for:
- general Foundation pretraining;
- Foundation instruction/safety post-training;
- Agent trajectories and tool-use behavior;
- DevOps/SRE knowledge and specialist tasks.

The reported 50,000 DevOps chunks may support governed retrieval and, after curation, specialist Agent training/evaluation. They are not the sole Foundation pretraining corpus. Report raw sources, unique records, unique examples, chunks, tokens, approved examples, and optimizer-consumed tokens separately. Generated filler, repeated chunks, embeddings, and vector rows are not unique approved examples.

## 5. Dataset governance

Every dataset release must include:
- immutable source IDs and source revisions, collection authority, rights/license basis, and source hashes;
- sensitivity classification, privacy/secret screening, redaction rules, and access restrictions;
- transformation, extraction, chunking, tokenization, and lineage records;
- language and quality distributions, malformed/empty/truncated-record checks, exact and near-duplicate reports;
- source/group-aware train, validation, and sealed-test splits with no related-record leakage;
- frozen evaluation-set custody and contamination checks;
- record, chunk, and token accounting, including what the optimizer actually consumed;
- retention, withdrawal, deletion, and downstream-artifact impact procedures.

A declared license is not legal approval. No unapproved, confidential, credential-bearing, or evaluation data may enter a training release. Rights withdrawal must be traceable to affected dataset releases and model versions, with an approved remediation decision before further release.

## 6. Training Authorization Gate (TAG)

No tokenizer training, Foundation pretraining, Agent training, or fine-tuning may start until every prerequisite is recorded as `PASS` against exact immutable revisions:

1. CHUNK 00 baseline and CHUNK 01 ADR reconciliation are closed `PASS`.
2. Relevant CHUNK 02 identity/security and CHUNK 03 data-governance dependencies are accepted.
3. Both model identities are distinct and documented; Foundation architecture/tokenizer plan is frozen; Agent initialization method is explicitly decided; no Qwen lineage exists.
4. Dataset releases have immutable manifests, source/rights approval, privacy and secret screening, deduplication/quality reports, group/source-aware partitions, and sealed test sets.
5. Trainer source and dependencies are pinned. Tests prove sample/token consumption, target masking, gradient updates, checkpoint integrity, resume behavior, and no train/evaluation leakage.
6. A compute feasibility plan records parameter count, corpus token target, sequence length, batch and accumulation, optimizer state, activation/checkpoint memory, hardware, storage, expected duration/cost, and recovery plan. A Colab T4 or CPU Tiny profile is not presumed sufficient for Foundation pretraining.
7. The training environment is isolated with least privilege, controlled egress, secret-free logs, SBOM, vulnerability disposition, artifact access controls, and backup.
8. Foundation and Agent evaluation suites, baseline, human-review process, metrics, and acceptance thresholds are frozen before training.
9. Model owner, data owner, security owner, and compute/budget owner approve the exact run plan and digests.

Any missing, stale, or unverifiable prerequisite makes the run `BLOCKED` or `NOT_VERIFIED`. No real training job or bulk-ingestion job may be dispatched.

## 7. Model and training lifecycle

Keep training-run state separate from registry release state.

**Training-run lifecycle:**

`PLANNED → AUTHORIZED → RUNNING → COMPLETED | FAILED | CANCELLED → EVALUATED`

**Model-release lifecycle:** use the RusaModels registry lifecycle (`draft → candidate → evaluated → approved → deprecated → archived`). NEXUS-specific identity/data/training gates must be mapped into this lifecycle without bypassing registry approval.

A completed optimizer run is not a model-quality pass. A model-quality pass is not a runtime pass. A runtime pass is not a production approval. Foundation and Agent require independent evidence and approvals. Rollback selects a previously approved immutable release; it never overwrites artifacts.

## 8. Acceptance gates

| Gate | Required evidence | Pass condition |
|---|---|---|
| N0 — Architecture/identity | Separate IDs, architecture records, tokenizer plan, initialization policy, owner | No ambiguous identity; dimensions and initialization are frozen; Qwen excluded |
| N1 — Dataset | Dataset cards, rights approvals, immutable manifests, quality/dedup/split reports, hashes | No unapproved data or cross-split leakage; sealed tests protected |
| N2 — Trainer | Pinned source/dependencies, smoke tests, sample/token accounting, resume/checkpoint tests | Actual consumption reconciles with manifest; no silent truncation or false coverage claims |
| N3 — Authorization | TAG checklist and named approvals | Every prerequisite passes before real training |
| N4 — Training | Reproducible logs, seeds, hardware, tokens/samples consumed, checkpoints/digests | Run is reproducible within declared limits; no hidden data/model lineage |
| N5 — Quality | Frozen independent Foundation and Agent evaluations, human review, baseline comparison | Predeclared thresholds pass; no critical safety blocker |
| N6 — Runtime | Model-specific hardware, quantization, latency, throughput, memory, concurrency, restart/cancellation | Predeclared targets met on the actual target profile |
| N7 — Release | Signed manifests, SBOM, model cards, security review, rollback, owner approvals | Each model independently approved; no unresolved Critical finding |
| N8 — Production | CHUNK 13 staging E2E and CHUNK 14 certification | All P0 gates pass; independent and owner sign-offs; explicit GO |

Quality/performance thresholds must be set before final evaluation for the declared workload and hardware. Do not invent universal thresholds or change them after observing results.

## 9. Open decisions and consequences

The following remain `OPEN_DECISION` and block training until resolved:
- Foundation parameter budget and exact architecture dimensions;
- Foundation tokenizer algorithm/vocabulary and context target;
- size, source, and token target for the general-purpose Foundation corpus;
- Foundation training hardware, compute budget, storage, and schedule;
- Agent initialization method, architecture/size, and tokenizer compatibility with Foundation;
- frozen benchmark definitions and numerical acceptance thresholds.

Consequences:
- Existing Qwen QLoRA configurations remain legacy isolated experiments and are not NEXUS dependencies.
- Existing Carbon-Tiny and research-100M work are prototypes/research candidates, not approved NEXUS Foundation artifacts.
- The NEXUS training program remains blocked until TAG passes.
- No production model is approved by this ADR.

## 10. References

- RUSA AI Ecosystem Roadmap v3.1 — NEXUS dual-model architecture and Training Authorization Gate.
- RusaModels Model Plane architecture and strict model-manifest/release lifecycle.
- RusaModels Dataset Engine documentation and its immutable release/verification contracts.
- Supplied NEXUS v5.8.2 release package; its reports are treated as package claims pending independent reproduction.
