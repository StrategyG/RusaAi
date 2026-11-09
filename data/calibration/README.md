# Calibration dataset

Create `calibration.jsonl` in this folder. Each line must be one JSON object containing either:

```json
{"text": "A representative text passage from the model's intended use domain."}
```

or, for an instruct/chat model with a usable chat template:

```json
{"messages": [{"role": "user", "content": "A representative user request."}, {"role": "assistant", "content": "A representative answer."}]}
```

Use at least 32 valid rows because the starter configuration enforces that minimum. For a serious quality run, begin with 128–256 representative samples and consider 512–1,024 samples if hardware and time allow. Include the languages, prompt styles, and domain vocabulary expected in deployment. Do not use private data, secrets, or evaluation/test examples.

The included example file is only a schema illustration; five examples are not enough for the configured run.
