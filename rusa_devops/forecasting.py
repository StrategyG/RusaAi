"""Prophet + ARIMA residual forecaster with expanding-window residual training."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class ProphetARIMAForecaster:
    def __init__(self, *, oof_folds: int = 3, arima_order: tuple[int, int, int] = (2, 0, 2)):
        if not 2 <= oof_folds <= 5:
            raise ValueError("oof_folds must be between 2 and 5")
        self.oof_folds = oof_folds
        self.arima_order = arima_order
        self.prophet_model: Any | None = None
        self.arima_model: Any | None = None
        self.residual_history: list[float] = []
        self.safety_offset = 0.0

    @staticmethod
    def _dependencies():
        try:
            import numpy as np
            import pandas as pd
            from prophet import Prophet
            from statsmodels.tsa.arima.model import ARIMA
        except ImportError as exc:
            raise RuntimeError("Install requirements-devops.txt to train Prophet + ARIMA") from exc
        return np, pd, Prophet, ARIMA

    @staticmethod
    def _new_prophet(Prophet, train):
        model = Prophet(
            yearly_seasonality=False,
            weekly_seasonality=len(train) >= 14 * 24 * 60,
            daily_seasonality=False,
            changepoint_prior_scale=0.05,
            seasonality_prior_scale=10.0,
            interval_width=0.95,
        )
        model.add_seasonality(name="hourly", period=1 / 24, fourier_order=5)
        model.add_seasonality(name="daily", period=1.0, fourier_order=5)
        return model

    def fit(self, frame):
        np, pd, Prophet, ARIMA = self._dependencies()
        if not {"ds", "y"}.issubset(frame.columns):
            raise ValueError("forecast frame requires ds and y columns")
        train = frame[["ds", "y"]].copy()
        train["ds"] = pd.to_datetime(train["ds"], utc=True).dt.tz_localize(None)
        train["y"] = pd.to_numeric(train["y"], errors="coerce")
        train = train.dropna().sort_values("ds").drop_duplicates("ds").reset_index(drop=True)
        if len(train) < 300:
            raise ValueError("at least 300 clean observations are required for hybrid training")
        if not np.isfinite(train["y"].to_numpy()).all():
            raise ValueError("training values must be finite")

        # Expanding-window out-of-fold residuals avoid optimistic in-sample Prophet residuals.
        initial = max(120, int(len(train) * 0.55))
        boundaries = np.linspace(initial, len(train), self.oof_folds + 1, dtype=int)
        residuals: list[float] = []
        for index in range(self.oof_folds):
            start, end = int(boundaries[index]), int(boundaries[index + 1])
            if end <= start:
                continue
            prefix, validation = train.iloc[:start], train.iloc[start:end]
            fold_model = self._new_prophet(Prophet, prefix)
            fold_model.fit(prefix)
            predicted = fold_model.predict(validation[["ds"]])["yhat"].to_numpy(dtype=float)
            residuals.extend((validation["y"].to_numpy(dtype=float) - predicted).tolist())
        if len(residuals) < 30:
            raise ValueError("out-of-fold residual history is too short for ARIMA")

        self.prophet_model = self._new_prophet(Prophet, train)
        self.prophet_model.fit(train)
        self.residual_history = [float(value) for value in residuals]
        self.arima_model = ARIMA(
            np.asarray(self.residual_history, dtype=float),
            order=self.arima_order,
            enforce_stationarity=False,
            enforce_invertibility=False,
        ).fit()
        return self

    def predict_components(self, timestamps):
        if self.prophet_model is None or self.arima_model is None:
            raise RuntimeError("forecaster must be fitted before prediction")
        np, pd, _, _ = self._dependencies()
        future_dates = pd.DatetimeIndex(pd.to_datetime(timestamps, utc=True)).tz_localize(None)
        future = pd.DataFrame({"ds": future_dates})
        if future.empty:
            raise ValueError("prediction timestamps cannot be empty")
        base = self.prophet_model.predict(future[["ds"]])
        residual = np.asarray(self.arima_model.forecast(steps=len(future)), dtype=float)
        return pd.DataFrame({
            "ds": future["ds"],
            "prophet_yhat": base["yhat"].to_numpy(dtype=float),
            "residual_yhat": residual,
            "hybrid_yhat": base["yhat"].to_numpy(dtype=float) + residual + self.safety_offset,
            "yhat_lower": base["yhat_lower"].to_numpy(dtype=float) + residual + self.safety_offset,
            "yhat_upper": base["yhat_upper"].to_numpy(dtype=float) + residual + self.safety_offset,
        })

    def save(self, path: str | Path) -> str:
        if self.prophet_model is None:
            raise RuntimeError("cannot save an unfitted forecaster")
        from prophet.serialize import model_to_json
        payload = {
            "schema_version": 1,
            "prophet_model": model_to_json(self.prophet_model),
            "residual_history": self.residual_history,
            "arima_order": list(self.arima_order),
            "oof_folds": self.oof_folds,
            "safety_offset": self.safety_offset,
        }
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        target.write_bytes(encoded)
        try:
            target.chmod(0o600)
        except OSError:
            pass
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def load(cls, path: str | Path, expected_sha256: str):
        from prophet.serialize import model_from_json
        from statsmodels.tsa.arima.model import ARIMA

        encoded = Path(path).read_bytes()
        if hashlib.sha256(encoded).hexdigest() != expected_sha256:
            raise ValueError("model artifact checksum mismatch")
        payload = json.loads(encoded.decode("utf-8"))
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported model artifact schema")
        instance = cls(
            oof_folds=int(payload["oof_folds"]),
            arima_order=tuple(int(value) for value in payload["arima_order"]),
        )
        instance.prophet_model = model_from_json(payload["prophet_model"])
        instance.residual_history = [float(value) for value in payload["residual_history"]]
        if len(instance.residual_history) < 30:
            raise ValueError("model artifact residual history is incomplete")
        instance.arima_model = ARIMA(
            instance.residual_history, order=instance.arima_order,
            enforce_stationarity=False, enforce_invertibility=False,
        ).fit()
        instance.safety_offset = float(payload.get("safety_offset", 0.0))
        return instance
