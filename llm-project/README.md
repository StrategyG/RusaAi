# RUSA-AI LLM Project Blueprint

This directory is a planning scaffold. It does not contain a trained RUSA-AI model, implemented tokenizer or training pipeline, or validated model architecture.

Status: PLANNED (scaffold only; serving adapters are implemented separately)

## Structure

- `data/`: planned only; README scaffold, no ingestion pipeline
- `tokenizer/`: planned only; no tokenizer trainer or vocabulary artifact
- `model/`: metadata-only blueprint; no neural-network implementation or weights
- `train/`: planned only; no pretraining, SFT, RL, or checkpoint pipeline
- `eval/`: planned only; no model benchmark or safety-evaluation harness
- `serve/`: implemented FastAPI gateway and optional Transformers/vLLM adapters
- `tools/`: planned only; agent tool execution lives in `rusa_agents/`
- `configs/`: non-executable planning metadata; the serving runtime uses environment configuration and verified artifacts

## Model summary

The blueprint currently records no approved layer count, hidden size, context window, modality, or MoE configuration. These values remain unset until a model architecture is implemented and supported by evidence. The optional inference runtime can load an externally supplied compatible artifact; it does not create or train RUSA-AI weights.

## Validation rule

Each artifact in this project should maintain evidence records for:

- dataset provenance
- model revision/hash
- training command and metrics
- benchmark and safety results
- serving metrics and hardware notes

Missing evidence keeps the component in `PLANNED` or `IMPLEMENTED`, never `VERIFIED`.
