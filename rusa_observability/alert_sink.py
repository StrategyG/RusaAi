from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException

from .middleware import install_metrics
from .telemetry import instrument_fastapi

logger = logging.getLogger("rusa-alert-sink")
app = FastAPI(title="RUSA-AI Alert Sink", docs_url=None, redoc_url=None, openapi_url=None)
install_metrics(app, "rusa-alert-sink")
instrument_fastapi(app, "rusa-alert-sink")


def _sanitize_alert(alert: dict[str, Any]) -> dict[str, str]:
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    annotations = alert.get("annotations") if isinstance(alert.get("annotations"), dict) else {}
    return {
        "alertname": str(labels.get("alertname", "unknown"))[:128],
        "severity": str(labels.get("severity", "unknown"))[:32],
        "service": str(labels.get("service", "unknown"))[:64],
        "status": str(alert.get("status", "firing"))[:16],
        "summary": str(annotations.get("summary", ""))[:512],
        "description": str(annotations.get("description", ""))[:1024],
        "startsAt": str(alert.get("startsAt", ""))[:64],
        "fingerprint": str(alert.get("fingerprint", ""))[:128],
    }


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok", "service": "rusa-alert-sink"}


@app.get("/health/ready")
def ready() -> dict[str, str]:
    return {"status": "ready", "service": "rusa-alert-sink"}


@app.post("/alerts")
async def receive_alerts(payload: dict[str, Any]) -> dict[str, int]:
    alerts = payload.get("alerts")
    if not isinstance(alerts, list) or len(alerts) > 100:
        raise HTTPException(status_code=400, detail="Alert payload must contain at most 100 alerts")
    sanitized = [_sanitize_alert(item) for item in alerts if isinstance(item, dict)]
    logger.warning(
        "alertmanager_notification count=%s alerts=%s",
        len(sanitized),
        json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")),
    )

    webhook_url = os.getenv("RUSA_ALERT_WEBHOOK_URL", "").strip()
    if webhook_url:
        parsed = urlsplit(webhook_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise HTTPException(status_code=503, detail="Configured alert webhook URL is invalid")
        headers = {"Content-Type": "application/json"}
        token = os.getenv("RUSA_ALERT_WEBHOOK_BEARER_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                response = await client.post(
                    webhook_url,
                    json={"alerts": sanitized},
                    headers=headers,
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("alert webhook delivery failed exception_type=%s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="Alert webhook delivery failed") from exc
    return {"accepted": len(sanitized)}


__all__ = ["app"]
