"""End-to-end CPU quantitative training pipeline."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
from datetime import datetime, timezone
from pathlib import Path

import joblib
import pandas as pd

from rusa_quant import __version__
from rusa_quant.data import chronological_split, load_time_series, make_future_index
from rusa_quant.evaluation.metrics import regression_metrics
from rusa_quant.indicators import bollinger_bands, moving_averages
from rusa_quant.models.arima import fit_best_arima
from rusa_quant.models.prophet import fit_prophet, predict_prophet


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_pipeline(
    data_path: str | Path,
    output_dir: str | Path,
    date_col: str = "date",
    value_col: str = "close",
    horizon: int = 30,
    test_size: float = 0.2,
    skip_prophet: bool = False,
    source_branch: str | None = None,
) -> dict:
    """Fit/evaluate models, refit final artifacts, and write all outputs."""
    data_path = Path(data_path)
    output_dir = Path(output_dir)
    if not data_path.is_file():
        raise FileNotFoundError(f"Dataset not found: {data_path}")
    if horizon < 1:
        raise ValueError("horizon must be >= 1.")

    model_dir = output_dir / "models"
    result_dir = output_dir / "results"
    report_dir = output_dir / "reports"
    for directory in (model_dir, result_dir, report_dir):
        directory.mkdir(parents=True, exist_ok=True)

    series = load_time_series(data_path, date_col=date_col, value_col=value_col)
    train, test = chronological_split(series, test_size=test_size)

    indicators = pd.concat(
        [
            series.rename(value_col),
            moving_averages(series, windows=(5, 10, 30)),
            bollinger_bands(series, window=20, num_std=2.0),
        ],
        axis=1,
    )
    indicators.to_csv(result_dir / "technical_indicators.csv", index_label=date_col)

    holdout = pd.DataFrame({"actual": test.to_numpy()}, index=test.index)
    metrics: dict[str, dict] = {}
    arima_train_model, best_order, best_aic, arima_selection = fit_best_arima(train)
    arima_test = arima_train_model.forecast(steps=len(test))
    holdout["ARIMA"] = arima_test.to_numpy()
    metrics["ARIMA"] = regression_metrics(test.to_numpy(), arima_test.to_numpy())

    arima_final, _, _, _ = fit_best_arima(series, candidates=(best_order,))
    joblib.dump(arima_final, model_dir / "arima_model.joblib")

    prophet_final = None
    if not skip_prophet:
        prophet_eval = fit_prophet(train)
        prophet_test_frame = predict_prophet(prophet_eval, test.index)
        prophet_test = prophet_test_frame["yhat"].to_numpy()
        holdout["Prophet"] = prophet_test
        metrics["Prophet"] = regression_metrics(test.to_numpy(), prophet_test)

        prophet_final = fit_prophet(series)
        from prophet.serialize import model_to_json

        (model_dir / "prophet_model.json").write_text(
            model_to_json(prophet_final), encoding="utf-8"
        )

    # Simple, transparent constant-level moving-average baselines.
    for window in (5, 10, 30):
        baseline = float(train.tail(window).mean())
        prediction = [baseline] * len(test)
        holdout[f"MA{window}_baseline"] = prediction
        metrics[f"MA{window}_baseline"] = regression_metrics(test.to_numpy(), prediction)

    if "Prophet" in holdout.columns:
        holdout["ARIMA_Prophet_ensemble"] = (
            holdout["ARIMA"] + holdout["Prophet"]
        ) / 2.0
        metrics["ARIMA_Prophet_ensemble"] = regression_metrics(
            test.to_numpy(), holdout["ARIMA_Prophet_ensemble"].to_numpy()
        )
    holdout.to_csv(result_dir / "holdout_predictions.csv", index_label=date_col)

    future_index = make_future_index(series.index, periods=horizon)
    arima_future = arima_final.forecast(steps=horizon)
    future = pd.DataFrame(
        {"ARIMA_forecast": arima_future.to_numpy()},
        index=future_index,
    )
    if prophet_final is not None:
        prophet_future = predict_prophet(prophet_final, future_index)
        future["Prophet_forecast"] = prophet_future["yhat"].to_numpy()
        future["Prophet_lower"] = prophet_future["yhat_lower"].to_numpy()
        future["Prophet_upper"] = prophet_future["yhat_upper"].to_numpy()
        future["Ensemble_forecast"] = (
            future["ARIMA_forecast"] + future["Prophet_forecast"]
        ) / 2.0
    future.to_csv(result_dir / "future_forecasts.csv", index_label=date_col)

    metrics_frame = pd.DataFrame.from_dict(metrics, orient="index")
    metrics_frame.index.name = "model"
    metrics_frame.to_csv(report_dir / "model_metrics.csv")

    manifest = {
        "project": "RUSA-AI CPU Quant Training",
        "quant_version": __version__,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "execution_device": "CPU",
        "gpu_required": False,
        "source_repository": "https://github.com/StrategyG/RusaAi",
        "source_branch": source_branch,
        "dataset_sha256": _sha256(data_path),
        "dataset_rows": int(len(series)),
        "date_column": date_col,
        "value_column": value_col,
        "date_start": series.index.min().isoformat(),
        "date_end": series.index.max().isoformat(),
        "train_rows": int(len(train)),
        "test_rows": int(len(test)),
        "forecast_horizon": int(horizon),
        "selected_arima_order": list(best_order),
        "selected_arima_aic": float(best_aic),
        "arima_candidate_results": arima_selection,
        "metrics": metrics,
        "python_version": platform.python_version(),
        "artifacts": [
            "models/arima_model.joblib",
            *([] if skip_prophet else ["models/prophet_model.json"]),
            "results/technical_indicators.csv",
            "results/holdout_predictions.csv",
            "results/future_forecasts.csv",
            "reports/model_metrics.csv",
        ],
        "limitations": [
            "MA and Bollinger Bands are indicators/baselines, not separately fitted ML models.",
            "Game theory requires explicit payoff matrices and is exposed as a separate engine.",
            "Hedge calculations require user-supplied odds and do not guarantee profit.",
            "Forecast quality depends on data quality, frequency, and regime stability.",
        ],
    }
    (report_dir / "training_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    return {
        "output_dir": str(output_dir),
        "metrics": metrics,
        "selected_arima_order": list(best_order),
        "manifest_path": str(report_dir / "training_manifest.json"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="RUSA-AI CPU quantitative training.")
    parser.add_argument("--data", required=True, help="CSV path with historical observations.")
    parser.add_argument("--output", required=True, help="Output directory for model artifacts.")
    parser.add_argument("--date-col", default="date")
    parser.add_argument("--value-col", default="close")
    parser.add_argument("--horizon", type=int, default=30)
    parser.add_argument("--test-size", type=float, default=0.2)
    parser.add_argument("--skip-prophet", action="store_true")
    parser.add_argument("--source-branch", default=None)
    args = parser.parse_args()

    summary = run_pipeline(
        data_path=args.data,
        output_dir=args.output,
        date_col=args.date_col,
        value_col=args.value_col,
        horizon=args.horizon,
        test_size=args.test_size,
        skip_prophet=args.skip_prophet,
        source_branch=args.source_branch,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
