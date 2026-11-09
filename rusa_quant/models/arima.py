"""CPU-compatible ARIMA model selection and forecasting."""
from __future__ import annotations

import warnings

import pandas as pd
from statsmodels.tsa.arima.model import ARIMA

DEFAULT_ORDERS = ((1, 1, 0), (2, 1, 0), (1, 1, 1), (2, 1, 1), (3, 1, 0))


def fit_best_arima(
    series: pd.Series,
    candidates: tuple[tuple[int, int, int], ...] = DEFAULT_ORDERS,
):
    """Fit candidate ARIMA orders and return the lowest-AIC fitted result."""
    if len(series) < 20:
        raise ValueError("ARIMA requires at least 20 observations.")
    if not candidates:
        raise ValueError("At least one ARIMA candidate order is required.")

    fitted_candidates = []
    failures = []
    for order in candidates:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                fitted = ARIMA(
                    series.astype(float),
                    order=order,
                    enforce_stationarity=False,
                    enforce_invertibility=False,
                ).fit()
            fitted_candidates.append((float(fitted.aic), order, fitted))
        except Exception as exc:  # candidate-specific failure should not stop selection
            failures.append({"order": order, "error": str(exc)})

    if not fitted_candidates:
        raise RuntimeError(f"All ARIMA candidates failed: {failures}")

    fitted_candidates.sort(key=lambda item: item[0])
    best_aic, best_order, best_model = fitted_candidates[0]
    selection = [
        {"order": list(order), "aic": aic}
        for aic, order, _ in fitted_candidates
    ]
    return best_model, best_order, best_aic, selection
