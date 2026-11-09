"""Typed contracts shared by the DevOps API, queue, and worker."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class TelemetrySample:
    tenant_id: str
    metric_name: str
    resource_id: str
    observed_at: datetime
    value: float
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DevOpsJob:
    id: int
    task_id: str
    tenant_id: str
    job_type: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    lease_owner: str | None = None
    lease_token: str | None = None


@dataclass(frozen=True)
class AnomalyAssessment:
    observed_at: str
    value: float
    z_score: float
    posterior_probability: float
    is_anomaly: bool


@dataclass(frozen=True)
class CapacityRecommendation:
    action: str
    current_capacity: float
    recommended_capacity: float
    predicted_peak: float
    quantile_level: float
    approval_required: bool
    execution_enabled: bool = False
