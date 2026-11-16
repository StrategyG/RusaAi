# ADR-0003: NEXUS-1 v5.8.2 specialist-model intake boundary

- Date: 2026-11-15
- Status: Proposed — review required
- Scope: RusaAi Control Plane integration boundary
- Assessment source: `NEXUS1_v5_8_2_Deep_Institutional_Production_System.zip`

## Context

The NEXUS-1 v5.8.2 package was assessed as a compact hybrid DevOps/SRE system containing an Argon specialist model, deterministic diagnosis and policy logic, forecasting components, and institutional controls.

The package reports an Argon model with 887,434 parameters and 50,000 derived chunks from 2,100 synthetic incident records. The assessment identified material limitations in the evidence supporting those claims:

- The 64-token memmap path retains the tail of longer examples and loses the incident prompt context.
- Random windows over a flattened memmap can cross record boundaries.
- The optimizer consumes only a small subset of the derived memmap samples in the supplied short training run; derived chunk count is not equivalent to unique examples consumed.
- The model token-accuracy report uses a small sampled subset, while hybrid diagnostic results are not attributable to Argon independently.
- The reported OOD families are represented in the training taxonomy and do not establish open-world generalization.
- The quantization comparison does not establish parity between the trained FP32 checkpoint and the deployed INT8 artifact.
- Trust-root, reviewer-identity, MFA secret lifecycle, RPO, and rollback evidence require stronger independent verification.

These findings prevent the supplied Argon artifact from being treated as a validated RusaAi foundation checkpoint or production specialist.

## Decision

1. **RusaAi remains the Control Plane.** Do not copy the NEXUS runtime, model weights, tokenizer payloads, or training artifacts into this repository's production application or API image.
2. **RusaModels remains the Model Plane.** Any future Argon candidate intake, artifact registry, quantization, inference, embedding, and model lifecycle must be handled through the authenticated RusaModels boundary and its model-artifact contract.
3. **Argon v5.8.2 is research-only and unapproved.** It is not the RusaAi-Edge-1.54B foundation model, does not replace the non-Qwen foundation-checkpoint decision, and must not be exposed as a production model ID.
4. **Model-level and system-level results must remain separate.** Argon-only generation/reasoning metrics must be measured using the exact candidate checkpoint. Deterministic diagnosis, forecasting, policy, and hybrid end-to-end outcomes must be reported as system-level metrics and must not be attributed to the LLM.
5. **Dataset partitions must be scenario-safe.** Training/evaluation partitions must not share record IDs, exact examples, identical role/prompt pairs, or declared scenario groups. Derived chunks/windows must retain record boundaries and preserve prompt/target alignment.
6. **Training must fail rather than silently truncate.** Prompt and target tokenization must be measured against the configured sequence length. Completion-only loss must mask prompt tokens, preserve the end-of-turn token, and reject overlength examples unless an explicitly reviewed chunking strategy preserves semantics and record boundaries.
7. **Production certification remains evidence-bound.** A dictionary field or self-asserted reviewer status is not an independent attestation. External trust anchors, cryptographic artifact/provenance verification, verified reviewer identity, operational RPO/RTO drills, and real rollback evidence remain mandatory where applicable.
8. **Knowledge ownership is unchanged by this ADR.** RAG_Cerdas remains a separate Knowledge Base repository. This change does not authorize dual-write, mirroring, bulk synchronization, or cross-read, and does not supersede the existing canonical Agent/Worker knowledge-store ADR.
9. **No deployment is authorized.** This ADR creates an intake and validation boundary only. Production promotion requires the existing production-certification contract, exact-commit evidence, staging verification, and accountable human approval.

## Consequences

- The current RusaAi QLoRA pipeline remains a separate six-role AICode worker-contract experiment; it is not Argon training and is not the RusaAi-Edge foundation checkpoint.
- The current RusaAi SFT builder already preserves the full prompt, masks prompt tokens from completion loss, appends EOS, and raises on sequence overflow. Regression tests are added to protect these properties.
- Dataset validation is strengthened to detect prompt-level split overlap and optional scenario-group leakage.
- A future Argon intake must provide an immutable model revision, license and data provenance, tokenizer revision, complete training/resume state, artifact hashes/signatures, exact model-only evaluation, and deployment-hardware evidence before it can be considered by RusaModels.
- RusaAi continues to call approved models through the authenticated Model Access Gateway. The Control Plane does not acquire model-serving ownership.

## Validation

- Unit tests verify completion-only labels, EOS retention, no silent truncation, and one independent training example per source record.
- Dataset tests verify prompt-level leakage and scenario-group leakage are rejected.
- The repository CI must pass on the exact pull-request head.
- These tests do not certify Argon, a foundation checkpoint, production deployment, or AGI/ASI capability.
