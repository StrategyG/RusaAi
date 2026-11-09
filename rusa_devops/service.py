"""Leakage-aware hybrid forecasting experiment and artifact lifecycle."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd

from .decision import calibrate_offset
from .evaluation import forecast_metrics
from .forecasting import ProphetARIMAForecaster


def run_forecast_experiment(
    frame,
    *,
    metric_name: str,
    resource_id: str,
    validation_horizon: int = 120,
    test_horizon: int = 120,
    oof_folds: int = 3,
    downtime_cost: float = 100.0,
    overprovision_cost: float = 1.0,
    artifact_root: str = "/var/lib/rusa/devops-artifacts",
    tenant_id: str = "development",
) -> dict[str, Any]:
    data = frame[["ds", "y"]].copy()
    data["ds"] = pd.to_datetime(data["ds"], utc=True).dt.tz_localize(None)
    data["y"] = pd.to_numeric(data["y"], errors="coerce")
    data = data.dropna().sort_values("ds").drop_duplicates("ds").reset_index(drop=True)
    required = validation_horizon + test_horizon + 300
    if len(data) < required:
        raise ValueError(f"need at least {required} clean rows; received {len(data)}")
    train_end = len(data) - validation_horizon - test_horizon
    validation_end = len(data) - test_horizon
    train = data.iloc[:train_end].copy()
    validation = data.iloc[train_end:validation_end].copy()
    test = data.iloc[validation_end:].copy()

    model = ProphetARIMAForecaster(oof_folds=oof_folds).fit(train)
    future = pd.concat([validation[["ds"]], test[["ds"]]], ignore_index=True)
    components = model.predict_components(future["ds"])
    validation_pred = components.iloc[:len(validation)]["hybrid_yhat"].tolist()
    offset = calibrate_offset(
        validation["y"].tolist(), validation_pred, downtime_cost, overprovision_cost
    )
    model.safety_offset = offset
    components = model.predict_components(future["ds"])
    hybrid_pred = components["hybrid_yhat"].to_numpy(dtype=float)
    prophet_pred = components["prophet_yhat"].to_numpy(dtype=float)
    validation_slice = slice(0, len(validation))
    test_slice = slice(len(validation), len(validation) + len(test))
    baseline_pred = [float(train["y"].iloc[-1])] * len(test)
    metrics = {
        "baseline_last_value": forecast_metrics(
            test["y"].tolist(), baseline_pred,
            downtime_cost=downtime_cost, overprovision_cost=overprovision_cost,
        ),
        "prophet_only": forecast_metrics(
            test["y"].tolist(), prophet_pred[test_slice].tolist(),
            downtime_cost=downtime_cost, overprovision_cost=overprovision_cost,
        ),
        "prophet_arima_hybrid": forecast_metrics(
            test["y"].tolist(), hybrid_pred[test_slice].tolist(),
            downtime_cost=downtime_cost, overprovision_cost=overprovision_cost,
        ),
        "validation": forecast_metrics(
            validation["y"].tolist(), hybrid_pred[validation_slice].tolist(),
            downtime_cost=downtime_cost, overprovision_cost=overprovision_cost,
        ),
        "calibration": {
            "validation_offset": offset,
            "cost_quantile_target": downtime_cost / (downtime_cost + overprovision_cost),
        },
    }
    actual = test["y"].to_numpy(dtype=float)
    lower = components.iloc[test_slice]["yhat_lower"].to_numpy(dtype=float)
    upper = components.iloc[test_slice]["yhat_upper"].to_numpy(dtype=float)
    metrics["prophet_arima_hybrid"]["prediction_interval_coverage"] = float(
        np.mean((actual >= lower) & (actual <= upper))
    )

    run_id = str(uuid4())
    tenant_key = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:24]
    artifact_path = Path(artifact_root) / tenant_key / run_id / "model.json"
    artifact_sha = model.save(artifact_path)
    return {
        "run_id": run_id, "metric_name": metric_name, "resource_id": resource_id,
        "train_rows": len(train), "validation_rows": len(validation), "test_rows": len(test),
        "metrics": metrics, "artifact_uri": str(artifact_path),
        "artifact_sha256": artifact_sha,
    }
