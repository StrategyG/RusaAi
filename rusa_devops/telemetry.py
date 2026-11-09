"""Synthetic and Prometheus telemetry ingestion adapters."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

from .contracts import TelemetrySample

METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]{0,199}$")
RESOURCE_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,200}$")


def validate_metric_identity(metric_name: str, resource_id: str) -> None:
    if not isinstance(metric_name, str) or not METRIC_NAME.fullmatch(metric_name):
        raise ValueError("metric_name is invalid")
    if not isinstance(resource_id, str) or not RESOURCE_ID.fullmatch(resource_id):
        raise ValueError("resource_id is invalid")


def generate_synthetic_telemetry(
    tenant_id: str,
    *,
    metric_name: str = "cpu_utilization",
    resource_id: str = "demo-local",
    rows: int = 2_000,
    seed: int = 42,
    anomaly_every: int = 500,
) -> list[TelemetrySample]:
    """Generate reproducible minute-level telemetry for smoke tests and benchmarks."""
    validate_metric_identity(metric_name, resource_id)
    if not 300 <= rows <= 100_000:
        raise ValueError("rows must be between 300 and 100000")
    if anomaly_every < 0 or anomaly_every > rows:
        raise ValueError("anomaly_every must be zero or between 1 and rows")
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("Install requirements-devops.txt for synthetic telemetry generation") from exc

    rng = np.random.default_rng(seed)
    index = np.arange(rows, dtype=float)
    trend = 35.0 + 20.0 * index / max(rows - 1, 1)
    hourly = 7.0 * np.sin(2.0 * np.pi * index / 60.0)
    daily = 4.0 * np.sin(2.0 * np.pi * index / 1440.0)
    values = trend + hourly + daily + rng.normal(0.0, 2.0, rows)
    anomalies = np.zeros(rows, dtype=bool)
    if anomaly_every:
        for start in range(anomaly_every // 2, rows, anomaly_every):
            end = min(start + 8, rows)
            values[start:end] += rng.uniform(25.0, 40.0)
            anomalies[start:end] = True
    values = np.clip(values, 0.0, 100.0)
    from datetime import timedelta
    start_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    result = []
    for position, value in enumerate(values):
        result.append(
            TelemetrySample(
                tenant_id=tenant_id,
                metric_name=metric_name,
                resource_id=resource_id,
                observed_at=start_time + timedelta(minutes=position),
                value=float(value),
                labels={"source": "synthetic", "is_anomaly": str(bool(anomalies[position])).lower()},
            )
        )
    return result


def rolling_windows(samples: list[TelemetrySample], window_size: int, overlap: int = 0):
    """Yield bounded rolling windows without copying the full telemetry history."""
    if window_size < 2 or overlap < 0 or overlap >= window_size:
        raise ValueError("window_size must be >= 2 and overlap must be in [0, window_size)")
    step = window_size - overlap
    ordered = sorted(samples, key=lambda item: item.observed_at)
    for start in range(0, len(ordered), step):
        window = ordered[start : start + window_size]
        if len(window) < 2:
            break
        yield window


class PrometheusTelemetrySource:
    """Read-only Prometheus HTTP API adapter; exporters normalize Docker/K8s/cloud metrics."""

    def __init__(self, base_url: str, bearer_token: str | None = None, timeout_seconds: float = 15.0):
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Prometheus URL must use HTTP or HTTPS")
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("Prometheus timeout must be between 1 and 60 seconds")
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.timeout_seconds = timeout_seconds

    def query_range(
        self, tenant_id: str, metric_name: str, resource_id: str,
        start: datetime, end: datetime, step_seconds: int = 60,
    ) -> list[TelemetrySample]:
        validate_metric_identity(metric_name, resource_id)
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("start/end must be timezone-aware and end must be after start")
        if not 15 <= step_seconds <= 3600:
            raise ValueError("step_seconds must be between 15 and 3600")
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("Install httpx to use Prometheus telemetry") from exc
        query = f'{metric_name}{{resource_id="{resource_id}"}}'
        headers = {"Authorization": f"Bearer {self.bearer_token}"} if self.bearer_token else {}
        response = httpx.get(
            f"{self.base_url}/api/v1/query_range",
            params={"query": query, "start": start.timestamp(), "end": end.timestamp(), "step": step_seconds},
            headers=headers, timeout=self.timeout_seconds, follow_redirects=False,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        if payload.get("status") != "success":
            raise RuntimeError("Prometheus query did not return success")
        samples: list[TelemetrySample] = []
        for series in payload.get("data", {}).get("result", []):
            labels = {
                str(key): str(value) for key, value in series.get("metric", {}).items()
                if key not in {"__name__", "resource_id"}
            }
            for timestamp, raw_value in series.get("values", []):
                value = float(raw_value)
                if not math.isfinite(value):
                    continue
                samples.append(TelemetrySample(
                    tenant_id=tenant_id, metric_name=metric_name, resource_id=resource_id,
                    observed_at=datetime.fromtimestamp(float(timestamp), tz=timezone.utc),
                    value=value, labels=labels,
                ))
        return sorted(samples, key=lambda item: item.observed_at)
