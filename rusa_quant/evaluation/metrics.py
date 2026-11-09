"""Forecast error metrics."""
from __future__ import annotations

import numpy as np
from sklearn.metrics import mean_absolute_error, mean_squared_error


def regression_metrics(actual, predicted) -> dict[str, float | None]:
    """Calculate MAE, RMSE, MAPE (zero-safe), and symmetric MAPE."""
    y_true = np.asarray(actual, dtype=float).reshape(-1)
    y_pred = np.asarray(predicted, dtype=float).reshape(-1)
    if y_true.size == 0 or y_true.size != y_pred.size:
        raise ValueError("actual and predicted must have the same non-zero length.")
    if not np.isfinite(y_true).all() or not np.isfinite(y_pred).all():
        raise ValueError("actual and predicted must contain only finite values.")

    mae = float(mean_absolute_error(y_true, y_pred))
    rmse = float(np.sqrt(mean_squared_error(y_true, y_pred)))
    nonzero = np.abs(y_true) > 1e-12
    mape = (
        float(np.mean(np.abs((y_true[nonzero] - y_pred[nonzero]) / y_true[nonzero])) * 100)
        if nonzero.any()
        else None
    )
    denominator = np.abs(y_true) + np.abs(y_pred)
    valid = denominator > 1e-12
    smape = (
        float(np.mean(2 * np.abs(y_true[valid] - y_pred[valid]) / denominator[valid]) * 100)
        if valid.any()
        else 0.0
    )
    return {"MAE": mae, "RMSE": rmse, "MAPE_percent": mape, "sMAPE_percent": smape}
