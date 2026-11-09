# RUSA-AI CPU Quant Training

This module adds a separate, CPU-first quantitative time-series pipeline to RUSA-AI. It does not replace or modify the existing agent SFT/QLoRA workflow in \`llm-project/train\`.

## Included modules

- ARIMA candidate selection by AIC, holdout evaluation, and final refit.
- Prophet fit, holdout forecast, uncertainty bounds, and final model serialization.
- MA5, MA10, MA30 and 20-period Bollinger Bands (2 standard deviations).
- MA baselines, MAE, RMSE, MAPE, sMAPE, and expanding-window walk-forward evaluation helper.
- Game Theory: pure Nash equilibria for finite two-player games and a linear-programming mixed-strategy solver for zero-sum matrix games.
- Hedge Bet: two-outcome stake-balancing calculator. It is a calculator, not a prediction or profit guarantee.
- Training manifest with dataset SHA-256, date range, split sizes, selected ARIMA order, metrics, software version, and artifact list.

All model fitting and indicators run on CPU. No CUDA, PyTorch, bitsandbytes, or GPU runtime is required.

## Colab quick start

Open \`notebooks/RUSA_AI_QUANT_CPU_COLAB.ipynb\` in Google Colab and run the cells from top to bottom. Select a CPU runtime. The notebook clones this feature branch, installs \`requirements-quant.txt\`, mounts Google Drive, and runs the pipeline.

Expected CSV format (headers are configurable):

| date | close |
|---|---:|
| 2024-01-01 | 100.25 |
| 2024-01-02 | 101.10 |

Use chronological historical observations. A minimum of 60 valid observations is required by default. Do not upload private data to a public repository.

## CLI

From the repository root:

\`\`\`bash
python -m pip install -r requirements-quant.txt
python -m rusa_quant.pipeline \
  --data /path/to/market_data.csv \
  --date-col date \
  --value-col close \
  --output /path/to/RUSA_AI_TRAINING \
  --horizon 30 \
  --test-size 0.20 \
  --source-branch feature/quant-training-cpu
\`\`\`

To omit Prophet (for a quick ARIMA-only run), add \`--skip-prophet\`.

## Google Drive output layout

The Colab notebook writes to \`MyDrive/RUSA_AI_TRAINING/\`:

- \`models/arima_model.joblib\`
- \`models/prophet_model.json\` (unless Prophet is skipped)
- \`results/technical_indicators.csv\`
- \`results/holdout_predictions.csv\`
- \`results/future_forecasts.csv\`
- \`reports/model_metrics.csv\`
- \`reports/training_manifest.json\`

The notebook can optionally publish only the training manifest to the GitHub feature branch. To enable that optional step, add a Colab Secret named \`RUSA_GITHUB_TOKEN\` containing a fine-grained GitHub token restricted to this repository with Contents read/write permission. Raw datasets, model weights, and prediction CSVs are never pushed to GitHub by that step. Without the secret, Drive output still works.

## Interpretation and limitations

- MA and Bollinger Bands are deterministic indicators; they are calculated, not fitted as independent forecasting models.
- ARIMA and Prophet are statistical forecasting models. Their errors depend on data quality, sampling frequency, structural breaks, and the evaluation window.
- The pipeline uses a chronological holdout split. For robust model selection, run repeated walk-forward validation and inspect residuals; do not select a model from one metric alone.
- The equal-weight ARIMA/Prophet ensemble is a baseline, not a claim that it is always superior.
- Game-theory results are conditional on the supplied payoff matrices. Mixed strategies are solved only for zero-sum games.
- Hedge calculations balance gross payouts for two mutually exclusive outcomes under decimal odds. They exclude fees, taxes, limits, voids, price movement, settlement risk, and outcome probability. They do not guarantee profit or recommend a wager.
- The output is research/decision support, not financial or betting advice.

## Tests

\`\`\`bash
python -m pip install -r requirements-quant.txt
pytest -q tests/test_rusa_quant.py
\`\`\`

## Artifact and data policy

Generated model files and datasets belong in Drive or another controlled artifact store, not in Git. GitHub should contain source code, tests, configuration templates, and documentation. The optional Colab publisher uploads a sanitized run manifest only.
