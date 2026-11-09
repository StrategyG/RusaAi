"""Expanding-window walk-forward evaluation."""
from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

from rusa_quant.evaluation.metrics import regression_metrics


def expanding_window_backtest(
    series: pd.Series,
    fit_predict: Callable[[pd.Series, int], np.ndarray],
    min_train_size: int = 60,
    horizon: int = 1,
    step: int = 1,
) -> tuple[pd.DataFrame, dict[str, float | None]]:
    """Evaluate a caller-supplied forecasting function over chronological windows."""
    if min_train_size < 2 or horizon < 1 or step < 1:
        raise ValueError("min_train_size >= 2, horizon >= 1, and step >= 1 are required.")
    if len(series) < min_train_size + horizon:
        raise ValueError("Series is too short for the requested backtest.")

    rows = []
    for origin in range(min_train_size, len(series) - horizon + 1, step):
        train = series.iloc[:origin]
        actual = series.iloc[origin : origin + horizon]
        predicted = np.asarray(fit_predict(train, horizon), dtype=float).reshape(-1)
        if predicted.size != horizon:
            raise ValueError("fit_predict must return exactly horizon predictions.")
        for timestamp, observed, forecast in zip(actual.index, actual, predicted):
            rows.append(
                {"date": timestamp, "actual": float(observed), "predicted": float(forecast)}
            )

    result = pd.DataFrame(rows).set_index("date")
    return result, regression_metrics(result["actual"], result["predicted"])
