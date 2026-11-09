"""Validated configuration for the optional Predictive DevOps runtime."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class DevOpsConfig:
    enabled: bool = False
    autonomy_mode: str = "controlled"
    max_train_rows: int = 20_000
    validation_horizon: int = 120
    test_horizon: int = 120
    forecast_horizon: int = 30
    oof_folds: int = 3
    downtime_cost: float = 100.0
    overprovision_cost: float = 1.0
    scale_buffer: float = 1.15
    anomaly_prior: float = 0.01
    anomaly_posterior_threshold: float = 0.90
    max_attempts: int = 3
    lease_seconds: int = 180
    poll_seconds: float = 2.0
    artifact_root: str = "/var/lib/rusa/devops-artifacts"
    prometheus_url: str | None = None
    prometheus_token: str | None = None

    @classmethod
    def from_env(cls) -> "DevOpsConfig":
        mode = os.getenv("RUSA_DEVOPS_AUTONOMY_MODE", "controlled").strip().lower()
        if mode != "controlled":
            raise ValueError("RUSA_DEVOPS_AUTONOMY_MODE currently supports only 'controlled'")
        return cls(
            enabled=_bool("RUSA_DEVOPS_ENABLED", False),
            autonomy_mode=mode,
            max_train_rows=_int("RUSA_DEVOPS_MAX_TRAIN_ROWS", 20_000, 300, 500_000),
            validation_horizon=_int("RUSA_DEVOPS_VALIDATION_HORIZON", 120, 30, 20_000),
            test_horizon=_int("RUSA_DEVOPS_TEST_HORIZON", 120, 30, 20_000),
            forecast_horizon=_int("RUSA_DEVOPS_FORECAST_HORIZON", 30, 1, 10_080),
            oof_folds=_int("RUSA_DEVOPS_OOF_FOLDS", 3, 2, 5),
            downtime_cost=_float("RUSA_DEVOPS_DOWNTIME_COST", 100.0, 0.01, 1_000_000.0),
            overprovision_cost=_float("RUSA_DEVOPS_OVERPROVISION_COST", 1.0, 0.001, 1_000_000.0),
            scale_buffer=_float("RUSA_DEVOPS_SCALE_BUFFER", 1.15, 1.0, 3.0),
            anomaly_prior=_float("RUSA_DEVOPS_ANOMALY_PRIOR", 0.01, 0.000001, 0.5),
            anomaly_posterior_threshold=_float("RUSA_DEVOPS_ANOMALY_THRESHOLD", 0.90, 0.5, 0.999999),
            max_attempts=_int("RUSA_DEVOPS_MAX_ATTEMPTS", 3, 1, 10),
            lease_seconds=_int("RUSA_DEVOPS_LEASE_SECONDS", 180, 15, 3600),
            poll_seconds=_float("RUSA_DEVOPS_POLL_SECONDS", 2.0, 0.1, 60.0),
            artifact_root=os.getenv("RUSA_DEVOPS_ARTIFACT_ROOT", "/var/lib/rusa/devops-artifacts"),
            prometheus_url=os.getenv("RUSA_DEVOPS_PROMETHEUS_URL") or None,
            prometheus_token=os.getenv("RUSA_DEVOPS_PROMETHEUS_TOKEN") or None,
        )
