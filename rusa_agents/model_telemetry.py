"""Authenticated client for the Model Plane memory telemetry endpoint."""

from __future__ import annotations

import math
import time
from typing import Any

import httpx


class ModelTelemetryError(RuntimeError):
    """Raised when the Model Plane cannot provide trustworthy memory telemetry."""


async def fetch_model_telemetry(
    url: str,
    api_key: str | None,
    expected_model_id: str,
    *,
    timeout_seconds: float = 5.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, float | str]:
    if not url:
        raise ModelTelemetryError("RUSA_MODEL_TELEMETRY_URL is not configured")
    if not api_key:
        raise ModelTelemetryError("RUSA_MODEL_TELEMETRY_API_KEY is not configured")

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 2.0)),
            follow_redirects=False,
            transport=transport,
        ) as client:
            response = await client.get(
                url,
                headers={"X-API-Key": api_key, "Accept": "application/json"},
            )
            response.raise_for_status()
            payload: Any = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ModelTelemetryError("Model Plane telemetry endpoint is unavailable") from exc

    if not isinstance(payload, dict):
        raise ModelTelemetryError("Model Plane telemetry response must be a JSON object")
    if payload.get("schema_version") != "1.0":
        raise ModelTelemetryError("Model Plane telemetry schema version is unsupported")
    if payload.get("model_id") != expected_model_id:
        raise ModelTelemetryError("Model Plane telemetry model identity does not match the request")

    sampled_at = payload.get("sampled_at_unix")
    if (
        isinstance(sampled_at, bool)
        or not isinstance(sampled_at, (int, float))
        or not math.isfinite(sampled_at)
        or sampled_at > time.time() + 30
        or time.time() - sampled_at > 60
    ):
        raise ModelTelemetryError("Model Plane telemetry sample is stale or has an invalid timestamp")

    peak_ram_mb = payload.get("peak_ram_mb")
    current_ram_mb = payload.get("current_ram_mb")
    for field, value in (
        ("peak_ram_mb", peak_ram_mb),
        ("current_ram_mb", current_ram_mb),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ModelTelemetryError(f"Model Plane telemetry {field} is missing or invalid")

    return {
        "schema_version": "1.0",
        "model_id": expected_model_id,
        "peak_ram_mb": float(peak_ram_mb),
        "current_ram_mb": float(current_ram_mb),
    }
