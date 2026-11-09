"""Unit tests for the CPU quant modules."""
import importlib
import json

import pytest

np = pytest.importorskip("numpy", reason="Install the RUSA-AI quant extras to run this suite.")
pd = pytest.importorskip("pandas", reason="Install the RUSA-AI quant extras to run this suite.")
pytest.importorskip("scipy", reason="Install the RUSA-AI quant extras to run this suite.")
pytest.importorskip("sklearn", reason="Install the RUSA-AI quant extras to run this suite.")
pytest.importorskip("statsmodels", reason="Install the RUSA-AI quant extras to run this suite.")

data_module = importlib.import_module("rusa_quant.data")
game_module = importlib.import_module("rusa_quant.engines.game_theory")
hedge_module = importlib.import_module("rusa_quant.engines.hedge_bet")
backtest_module = importlib.import_module("rusa_quant.evaluation.backtesting")
metrics_module = importlib.import_module("rusa_quant.evaluation.metrics")
indicators_module = importlib.import_module("rusa_quant.indicators")
pipeline_module = importlib.import_module("rusa_quant.pipeline")

chronological_split = data_module.chronological_split
load_time_series = data_module.load_time_series
make_future_index = data_module.make_future_index
game_theory_engine = game_module.game_theory_engine
solve_zero_sum_game = game_module.solve_zero_sum_game
hedge_two_outcomes = hedge_module.hedge_two_outcomes
expanding_window_backtest = backtest_module.expanding_window_backtest
regression_metrics = metrics_module.regression_metrics
bollinger_bands = indicators_module.bollinger_bands
moving_averages = indicators_module.moving_averages
run_pipeline = pipeline_module.run_pipeline


def test_moving_averages_and_bollinger():
    series = pd.Series(np.arange(1.0, 41.0))
    ma = moving_averages(series)
    bands = bollinger_bands(series)
    assert ma.loc[4, "MA5"] == pytest.approx(3.0)
    assert ma.loc[9, "MA10"] == pytest.approx(5.5)
    assert ma.loc[29, "MA30"] == pytest.approx(15.5)
    assert bands.loc[19, "BB_UPPER"] > bands.loc[19, "BB_MIDDLE"]
    assert bands.loc[19, "BB_LOWER"] < bands.loc[19, "BB_MIDDLE"]


def test_invalid_indicator_parameters():
    with pytest.raises(ValueError):
        moving_averages(pd.Series([1, 2]), windows=(0,))
    with pytest.raises(ValueError):
        bollinger_bands(pd.Series([1, 2]), window=1)


def test_metrics_zero_safe():
    result = regression_metrics([0, 2, 4], [0, 3, 5])
    assert result["MAE"] == pytest.approx(2 / 3)
    assert result["MAPE_percent"] == pytest.approx(37.5)
    assert result["sMAPE_percent"] is not None


def test_chronological_split_and_future_index():
    index = pd.date_range("2024-01-01", periods=100, freq="D")
    series = pd.Series(np.arange(100.0), index=index)
    train, test = chronological_split(series, test_size=0.2)
    assert train.index.max() < test.index.min()
    future = make_future_index(series.index, periods=3)
    assert len(future) == 3
    assert future[0] > series.index[-1]


def test_load_time_series(tmp_path):
    path = tmp_path / "series.csv"
    pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=65, freq="D"),
            "close": np.arange(65.0),
        }
    ).to_csv(path, index=False)
    result = load_time_series(path)
    assert len(result) == 65
    assert result.name == "close"
    assert result.index.tz is None


def test_pure_nash_and_zero_sum_mixed():
    result = game_theory_engine([[3, 0], [5, 1]], [[3, 0], [5, 1]])
    assert result["pure_nash_equilibria"]
    matching_pennies = solve_zero_sum_game([[1, -1], [-1, 1]])
    assert matching_pennies["row_strategy"] == pytest.approx([0.5, 0.5])
    assert matching_pennies["column_strategy"] == pytest.approx([0.5, 0.5])
    assert matching_pennies["value_to_row_player"] == pytest.approx(0.0)


def test_hedge_two_outcomes_balances_gross_payout():
    result = hedge_two_outcomes(2.0, 2.0, 100.0)
    assert result["stake_b"] == pytest.approx(100.0)
    assert result["gross_payout_a"] == pytest.approx(result["gross_payout_b"])
    assert result["net_if_a_wins"] == pytest.approx(result["net_if_b_wins"])


def test_hedge_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        hedge_two_outcomes(1.0, 2.0, 100.0)


def test_expanding_window_backtest():
    index = pd.date_range("2024-01-01", periods=20, freq="D")
    series = pd.Series(np.arange(20.0), index=index)
    predictions, metrics = expanding_window_backtest(
        series,
        fit_predict=lambda train, horizon: np.repeat(train.iloc[-1], horizon),
        min_train_size=10,
        horizon=2,
        step=2,
    )
    assert not predictions.empty
    assert metrics["MAE"] > 0


def test_cpu_pipeline_without_prophet(tmp_path):
    dates = pd.date_range("2023-01-01", periods=100, freq="D")
    values = 100 + np.linspace(0, 8, 100) + np.sin(np.arange(100) / 4)
    csv_path = tmp_path / "history.csv"
    output_dir = tmp_path / "quant-output"
    pd.DataFrame({"date": dates, "close": values}).to_csv(csv_path, index=False)

    summary = run_pipeline(
        data_path=csv_path,
        output_dir=output_dir,
        horizon=5,
        test_size=0.2,
        skip_prophet=True,
        source_branch="feature/quant-training-cpu",
    )

    assert summary["selected_arima_order"]
    assert (output_dir / "models" / "arima_model.joblib").is_file()
    assert (output_dir / "results" / "technical_indicators.csv").is_file()
    assert (output_dir / "results" / "holdout_predictions.csv").is_file()
    assert (output_dir / "results" / "future_forecasts.csv").is_file()
    assert (output_dir / "reports" / "model_metrics.csv").is_file()
    manifest = json.loads(
        (output_dir / "reports" / "training_manifest.json").read_text()
    )
    assert manifest["execution_device"] == "CPU"
    assert manifest["gpu_required"] is False
    assert "ARIMA" in manifest["metrics"]
