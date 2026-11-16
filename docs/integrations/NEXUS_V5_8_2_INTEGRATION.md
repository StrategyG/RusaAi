# NEXUS-1 v5.8.2 assessment and RusaAi integration plan

## Decision summary

**Do not deploy or import NEXUS-1 v5.8.2 into the RusaAi Control Plane.** Keep the supplied Argon artifact as an unapproved research candidate. RusaAi is the Control Plane; RusaModels owns model artifacts and inference. The existing RusaAi QLoRA workflow is a separate AICode worker-contract experiment and is not Argon or the RusaAi-Edge foundation model.

RAG_Cerdas remains a separate Knowledge Base repository. This integration does not move its data, alter its lifecycle, or authorize cross-read, mirroring, or dual-write. Existing knowledge ownership decisions remain in force.

## Audit basis

The assessment is based on the uploaded `NEXUS1_v5_8_2_Deep_Institutional_Production_System.zip` and its source, training report, artifacts, tests, and evidence dossier. The audit findings below describe the supplied package; they are not claims about RusaAi runtime behavior.

## NEXUS-1 v5.8.2 findings

| Area | Finding | Integration consequence |
|---|---|---|
| Dataset accounting | 2,100 synthetic source records produce 50,000 derived chunks; derived chunks are not 50,000 independent incidents | Preserve separate counts for source records, unique scenarios, derived windows, and optimizer-consumed samples |
| Tokenization | The 64-token tail window discards incident prompt context on longer examples | Require full prompt/target alignment or reviewed boundary-aware chunking |
| Memmap sampling | Random windows over flattened data can cross record boundaries | Keep windows indexed by source record and prohibit cross-record packing without explicit sequence-boundary tokens |
| Optimizer coverage | The supplied short run consumes only a small fraction of the derived corpus | Report actual optimizer-consumed sample IDs/tokens, not corpus size as training coverage |
| Model evaluation | Token metrics use a small sampled subset; hybrid diagnosis is not Argon-only evidence | Separate exact-checkpoint model evaluation from deterministic/hybrid system evaluation |
| OOD | Test families are represented in the training taxonomy | Add blind scenarios and genuinely held-out incident families; report synthetic and real-world results separately |
| Quantization | Comparison does not establish trained FP32 versus deployed INT8 parity | Compare the exact trained and quantized artifacts under identical prompts and runtime |
| Security/governance | Packaged trust root, self-asserted reviewer fields, predictable MFA fallback, and simulated RPO/rollback claims weaken assurance | Require external trust, verified identities, persistent replay protection, and measured operational drills |
| Provenance | Runtime/default artifact identity and dossier evidence need exact binding | Bind model ID, tokenizer, source revision, artifact digest, runtime, dataset hashes, and evaluation report to one release candidate |

## Mapping to existing RusaAi ownership

| Concern | RusaAi location / owner | Boundary |
|---|---|---|
| Agent SFT dataset contract | `rusa_training/dataset.py` | Validates role/tool contract and train/eval separation |
| SFT tokenization and loss | `llm-project/train/train_sft.py` | Dedicated GPU experiment; rejects overlength examples; prompt labels are masked |
| Agent response evaluation | `llm-project/eval/evaluate_agent_sft.py` | JSON and worker-contract/safety metrics only; not semantic reasoning certification |
| Model artifacts and inference | RusaModels Model Plane | RusaAi does not own or bundle model weights |
| Release evidence | `rusa_agents/certification/` and `schemas/certification/` | Evidence-bound candidate decision; external workflow must verify signatures and human identity |
| Knowledge | RAG_Cerdas remains separate; RusaAi's existing knowledge ADR remains authoritative for Agent/Worker store boundaries | No data movement or new cross-read in this change |

## Changes in this integration

1. Extend the SFT record contract with optional `scenario_id` for explicit scenario-group partitioning.
2. Reject train/evaluation overlap on the same role/prompt pair even when target responses differ.
3. Reject scenario IDs that occur in both train and evaluation; when scenario grouping is used, require IDs on every record in both splits.
4. Add regression tests proving the SFT builder retains prompt tokens as context, applies loss only to completion tokens, retains EOS, and rejects overlength examples instead of silently truncating.
5. Record the NEXUS intake boundary and acceptance criteria in ADR-0003.

## Mandatory gates before any Argon candidate intake

- [ ] Immutable source revision, model revision, tokenizer revision, license, and data provenance are recorded.
- [ ] Source-record, unique-scenario, derived-window, and optimizer-consumed counts are reported separately.
- [ ] Train/validation/sealed-test partitions are grouped by scenario and have no prompt or scenario overlap.
- [ ] Tokenization reports prompt, completion, and total lengths; no silent truncation occurs.
- [ ] Sequence packing preserves source-record boundaries and completion-only loss masks.
- [ ] Resume test restores model, optimizer, scheduler, scaler (if used), RNG, and sampler state and reproduces the next training step within a declared tolerance.
- [ ] Model-only evaluation uses the exact trained checkpoint and exact deployed quantized artifact.
- [ ] Hybrid system metrics are reported separately from model-only metrics.
- [ ] OOD evaluation uses genuinely held-out incident families and blind examples; synthetic and authorized real-world data are reported separately.
- [ ] External trust anchor, signed artifact manifest, provenance attestation, and verified reviewer identities are validated.
- [ ] Target-hardware inference, memory, latency, concurrency, and soak evidence is produced by RusaModels.
- [ ] Production certification is completed for the exact source commit and artifact digest.

## Non-goals

- This change does not import NEXUS Python modules, Argon weights, tokenizer files, or its runtime into RusaAi.
- This change does not select or train the RusaAi-Edge-1.54B foundation checkpoint.
- This change does not change RusaModels ownership or start model training on the 4-vCPU/16-GB Control Plane host.
- This change does not modify RAG_Cerdas data, canonical knowledge-store decisions, migrations, or access policy.
- This change does not approve production deployment or make an AGI/ASI capability claim.

## Current status

The RusaAi training builder already has the correct fail-on-overlength and completion-only-loss design. This integration adds regression coverage and stronger split-leakage checks. The current curated corpus remains a small synthetic seed dataset; its provenance explicitly says no fine-tuning has been executed and no trained weights are included. Model quality, semantic task success, and target-hardware performance remain unmeasured.
