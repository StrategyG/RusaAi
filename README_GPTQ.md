# RUSA-AI GPTQ INT4 Quantization

This module adds a separate post-training GPTQ quantization workflow. It does not replace or modify the existing QLoRA/SFT training pipeline.

## Defaults

- GPTQ weight quantization: 4-bit, group size 128, symmetric
- Compute dtype: FP16 (W4A16)
- Calibration input: JSONL text rows or chat-message rows
- Output: a GPTQModel checkpoint

## Requirements

Use Python 3.10+ and a supported NVIDIA GPU for the CUDA workflow. The model architecture and selected GPTQ kernels must be supported by the installed GPTQModel release. Installing the package does not guarantee that a model will fit in available VRAM.

Install dependencies from the repository root:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements-quant.txt
python -m pip check
```

Check that PyTorch can see the GPU before starting a large model download:

```bash
python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
```

Do not replace a working Colab PyTorch/CUDA build without checking driver compatibility.

## Configure the model

Edit `rusa_quant/config.yaml` and set `model.id` to the exact Hugging Face model ID or a local dense-model directory. For reproducibility, set `model.revision` to an immutable model commit SHA. Keep `trust_remote_code: false` unless custom model code is required and has been reviewed.

You can also set `RUSA_QUANT_MODEL_ID` and `RUSA_QUANT_MODEL_REVISION` as environment variables. For gated Hugging Face repositories, provide `HF_TOKEN` through a secret or environment variable. Never commit access tokens.

## Calibration data

Create `data/calibration/calibration.jsonl` with one JSON object per line. Each row may contain a string `text` field or a `messages` array for models with a usable chat template.

The starter configuration requires at least 32 usable rows and loads up to 128 samples. `data/calibration/calibration.example.jsonl` contains only five schema examples; it is not sufficient for a run. Replace or expand it with representative, authorized data from the intended deployment domain. Do not include private data, secrets, or evaluation examples.

## Run

From the repository root:

```bash
python -m rusa_quant.pipeline --config rusa_quant/config.yaml
```

The default model ID is intentionally a placeholder. Set `model.id` in the YAML file or pass `--model-id ORG/MODEL`. The pipeline writes the quantized checkpoint under the configured artifacts directory and records run metadata under the configured log directory. It reloads the saved checkpoint and performs a small generation smoke test.

A successful smoke test verifies basic loading and generation only; it does not establish model quality. Evaluate the dense and quantized models on the same held-out evaluation set before deployment.

## Serving caveat

Creating a GPTQ checkpoint does not automatically integrate GPTQ loading into the current RUSA-AI API. The existing Transformers backend uses `AutoModelForCausalLM.from_pretrained`; add and test a compatible GPTQModel loader or use a separately validated serving engine before deploying the artifact through the API.

Do not guess model revisions, tokenizer revisions, or licenses when creating a model manifest.
