"""Leakage-aware chronological splitting and forecasting/classification metrics."""
from __future__ import annotations

import math
from typing import Sequence


def chronological_split(rows: Sequence[dict], validation_horizon: int, test_horizon: int):
    if validation_horizon < 1 or test_horizon < 1:
        raise ValueError("validation and test horizons must be positive")
    if len(rows) <= validation_horizon + test_horizon + 60:
        raise ValueError("not enough observations for train/validation/test chronological split")
    train_end = len(rows) - validation_horizon - test_horizon
    validation_end = len(rows) - test_horizon
    return list(rows[:train_end]), list(rows[train_end:validation_end]), list(rows[validation_end:])


def forecast_metrics(actual, predicted, *, downtime_cost: float = 100.0, overprovision_cost: float = 1.0):
    if len(actual) != len(predicted) or len(actual) == 0:
        raise ValueError("actual and predicted must be non-empty and have equal lengths")
    errors = [float(a) - float(p) for a, p in zip(actual, predicted, strict=True)]
    mae = sum(abs(error) for error in errors) / len(errors)
    rmse = math.sqrt(sum(error * error for error in errors) / len(errors))
    asymmetric = sum(
        (downtime_cost if error > 0 else overprovision_cost) * error * error
        for error in errors
    ) / len(errors)
    return {
        "mae": mae, "rmse": rmse, "asymmetric_squared_loss": asymmetric,
        "underprediction_rate": sum(error > 0 for error in errors) / len(errors),
        "samples": len(errors),
    }


def classification_metrics(actual, predicted):
    if len(actual) != len(predicted) or len(actual) == 0:
        raise ValueError("actual and predicted labels must be non-empty and equal in length")
    tp = sum(bool(a) and bool(p) for a, p in zip(actual, predicted, strict=True))
    fp = sum(not bool(a) and bool(p) for a, p in zip(actual, predicted, strict=True))
    fn = sum(bool(a) and not bool(p) for a, p in zip(actual, predicted, strict=True))
    tn = len(actual) - tp - fp - fn
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision, "recall": recall, "f1": f1,
        "true_positive": tp, "false_positive": fp, "false_negative": fn,
        "true_negative": tn, "samples": len(actual),
    }
