# ADR-TRAIN-001 — Foundation Checkpoint Decision

- **Status:** PILOT_ONLY
- **Date:** 2026-11-12
- **Owner:** RusaAi model release process
- **Product model identity:** `rusaai-edge-derived`
- **Architecture reference target:** `rusaai-edge-1.54b` / RusaAi-Edge-1.54B
- **Research-only identity:** `rusaai-research-100m`

## Decision

Do not approve a foundation checkpoint for production in this ADR.

Retain `RusaAi-Edge-1.54B` as the v2.0 architecture reference target, not as a claim that a compatible checkpoint exists. Until a compatible non-Qwen checkpoint passes provenance, license, tokenizer, architecture, code, CPU-runtime, and quality review, the product model identity remains `rusaai-edge-derived` with status `CHECKPOINT_CANDIDATE_PENDING_AUDIT`.

The only current candidate recorded for an 8-GB research/pilot evaluation is:

- Repository: `BhinekaIntiLabs/bhineka-gpt`
- Source: https://huggingface.co/BhinekaIntiLabs/bhineka-gpt
- Decision: `PILOT_ONLY`; not a production dependency and not a replacement for the 1.541B architecture target.

The historical RUSA AI CPU Deployment Configuration v1.0 uses Qwen3-4B GGUF Q4_K_M. Architecture v2.0 supersedes that model target with a non-Qwen checkpoint requirement. Do not silently restore the Qwen baseline.

## Candidate facts recorded from the published model card

These are publisher/model-card claims, not independent certification:

| Field | Published value |
|---|---|
| Model family | Bhineka-GPT-500M, Llama-style decoder-only Transformer |
| Parameters | 556,269,696 |
| Languages | Indonesian and English |
| Context | 2,048 tokens |
| Tokenizer | BPE, 64,000 vocabulary |
| Architecture | 28 layers, hidden size 1,152, 16 attention heads, 8 KV heads, SwiGLU, RoPE, RMSNorm |
| License metadata | Apache-2.0 |
| Reported validation | 53,678,481 tokens; loss 2.5355; perplexity 12.6227 |
| Reported validation device | CUDA, float32 |
| GGUF/llama.cpp path | Described in the model card |
| Custom code | The model card warns that the custom `bhineka` architecture may require `trust_remote_code=True` |

The reported validation loss is not evidence of CPU throughput, instruction-following quality, tool-use reliability, safety, or production suitability. The model card itself recommends broader instruction-following, factuality, and safety evaluation.

## Required evidence before promotion

The following items are deliberately unresolved:

- [ ] Record an immutable Hugging Face commit/revision; mutable `main` is not acceptable.
- [ ] Download candidate files into a controlled staging area and record exact file names, sizes, and SHA-256 digests.
- [ ] Audit complete model lineage and verify the checkpoint is non-Qwen.
- [ ] Review the full license file, acceptable-use terms, training-data provenance, and dataset license compatibility.
- [ ] Inspect `config.json`, `auto_map`, tokenizer files, special tokens, and the exact chat template.
- [ ] Review every custom modeling/tokenization file. Do not enable `trust_remote_code=True` before the pinned code has passed review.
- [ ] Verify architecture and parameter count from the actual checkpoint, not only the model card.
- [ ] Verify PEFT/LoRA target modules if adaptation is required.
- [ ] Verify an existing GGUF artifact or perform a reproducible conversion; record quantization metadata and artifact SHA-256.
- [ ] Run CPU-only load and inference tests on the actual target machine, with GPU offload disabled.
- [ ] Measure peak process RAM, minimum free RAM, model load time, TTFT, tokens/sec, queue wait, and error rate.
- [ ] Keep the candidate at 2K context until 4K/8K extension is independently validated.
- [ ] Evaluate Indonesian and English instruction following, factuality, summarization, translation, code generation, JSON/tool-call correctness, prompt injection, and safety.
- [ ] Compare the quantized artifact against the original checkpoint and frozen evaluation set.
- [ ] Publish a model card and release manifest with limitations and measured results.

## Promotion rules

- `PILOT_ONLY`: may be used for isolated research and acceptance experiments after the code and artifact have been safely pinned and reviewed.
- `APPROVED`: requires all applicable evidence above, signed release manifest, and target-hardware acceptance.
- `REJECTED`: required if lineage, license, custom code, runtime compatibility, or quality fails review.

No production model artifact, model quality certification, CPU benchmark, or checkpoint approval is claimed by this ADR.
