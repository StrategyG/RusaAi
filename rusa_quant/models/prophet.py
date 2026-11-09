"""Prophet adapter. Prophet is imported lazily to keep indicators lightweight."""
from __future__ import annotations

import pandas as pd


def fit_prophet(series: pd.Series):
    """Fit Prophet on a datetime-indexed numeric Series."""
    try:
        from prophet import Prophet
    except ImportError as exc:
        raise ImportError(
            "Prophet is not installed. Install requirements-quant.txt."
        ) from exc

    frame = pd.DataFrame({"ds": series.index, "y": series.to_numpy(dtype=float)})
    model = Prophet(
        yearly_seasonality="auto",
        weekly_seasonality="auto",
        daily_seasonality=False,
        uncertainty_samples=500,
    )
    model.fit(frame)
    return model


def predict_prophet(model, dates: pd.DatetimeIndex) -> pd.DataFrame:
    """Predict supplied timestamps and return Prophet's standard forecast columns."""
    return model.predict(pd.DataFrame({"ds": dates}))
