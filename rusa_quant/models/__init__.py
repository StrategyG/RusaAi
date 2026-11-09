"""Forecast model adapters."""

from rusa_quant.models.arima import fit_best_arima
from rusa_quant.models.prophet import fit_prophet, predict_prophet

__all__ = ["fit_best_arima", "fit_prophet", "predict_prophet"]
