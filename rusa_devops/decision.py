"""Cost-sensitive capacity recommendations; this module never applies infrastructure changes."""
from __future__ import annotations

import math

from .contracts import CapacityRecommendation


def asymmetric_squared_loss(actual, predicted, downtime_cost: float, overprovision_cost: float) -> float:
    if len(actual) != len(predicted) or len(actual) == 0:
        raise ValueError("actual and predicted must be non-empty and equal in length")
    if downtime_cost <= 0 or overprovision_cost <= 0:
        raise ValueError("cost weights must be positive")
    losses = []
    for observed, forecast in zip(actual, predicted, strict=True):
        error = float(observed) - float(forecast)
        losses.append((downtime_cost if error > 0 else overprovision_cost) * error * error)
    return sum(losses) / len(losses)


def target_quantile(downtime_cost: float, overprovision_cost: float) -> float:
    if downtime_cost <= 0 or overprovision_cost <= 0:
        raise ValueError("cost weights must be positive")
    return downtime_cost / (downtime_cost + overprovision_cost)


def calibrate_offset(actual, predicted, downtime_cost: float, overprovision_cost: float) -> float:
    """Choose a validation-only additive offset minimizing asymmetric squared loss."""
    if len(actual) != len(predicted) or len(actual) == 0:
        raise ValueError("actual and predicted must be non-empty and equal in length")
    residuals = [float(a) - float(p) for a, p in zip(actual, predicted, strict=True)]
    low, high = min(residuals), max(residuals)
    if math.isclose(low, high):
        return low
    candidates = [low + (high - low) * index / 500.0 for index in range(501)]
    return min(
        candidates,
        key=lambda offset: asymmetric_squared_loss(
            actual, [float(value) + offset for value in predicted], downtime_cost, overprovision_cost
        ),
    )


def recommend_capacity(
    predicted_values, current_capacity: float, *, downtime_cost: float = 100.0,
    overprovision_cost: float = 1.0, scale_buffer: float = 1.15,
    min_capacity: float = 1.0, max_capacity: float = 1_000_000.0,
) -> CapacityRecommendation:
    if len(predicted_values) == 0 or current_capacity <= 0:
        raise ValueError("forecast values and positive current capacity are required")
    if not 1.0 <= scale_buffer <= 3.0 or min_capacity <= 0 or max_capacity < min_capacity:
        raise ValueError("capacity bounds or scale buffer are invalid")
    peak = max(float(value) for value in predicted_values)
    quantile = target_quantile(downtime_cost, overprovision_cost)
    target = min(max(math.ceil(max(peak, 0.0) * scale_buffer), min_capacity), max_capacity)
    if target > current_capacity:
        action = "SCALE_OUT_RECOMMENDATION"
    elif peak < current_capacity * 0.55:
        action = "SCALE_IN_REVIEW"
    else:
        action = "HOLD"
        target = current_capacity
    return CapacityRecommendation(
        action=action, current_capacity=float(current_capacity),
        recommended_capacity=float(target), predicted_peak=peak,
        quantile_level=quantile, approval_required=action != "HOLD",
        execution_enabled=False,
    )
