"""FastAPI routes for tenant-scoped Predictive DevOps jobs and read models."""
from __future__ import annotations

import json
import math
import os
import re
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .a2a import agent_card
from .store import PostgresDevOpsStore

METRIC_PATTERN = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]{0,199}$")
RESOURCE_PATTERN = re.compile(r"^[A-Za-z0-9_.:/-]{1,200}$")


class DevOpsJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_type: Literal[
        "synthetic_ingest", "prometheus_collect", "anomaly_scan", "capacity_recommendation"
    ]
    payload: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = Field(default=3, ge=1, le=10)


class DevOpsTrainingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric_name: str = Field(min_length=1, max_length=200)
    resource_id: str = Field(min_length=1, max_length=200)
    validation_horizon: int = Field(default=120, ge=30, le=20000)
    test_horizon: int = Field(default=120, ge=30, le=20000)
    max_train_rows: int = Field(default=20000, ge=300, le=100000)

    @field_validator("metric_name")
    @classmethod
    def validate_metric(cls, value: str) -> str:
        if not METRIC_PATTERN.fullmatch(value):
            raise ValueError("metric_name is invalid")
        return value

    @field_validator("resource_id")
    @classmethod
    def validate_resource(cls, value: str) -> str:
        if not RESOURCE_PATTERN.fullmatch(value):
            raise ValueError("resource_id is invalid")
        return value


def _queue(request: Request):
    queue = getattr(request.app.state, "devops_queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="Predictive DevOps queue is disabled or unavailable")
    return queue


def _store():
    try:
        dsn = os.getenv("RUSA_API_DATABASE_URL") or os.getenv("RUSA_DATABASE_URL")
        return PostgresDevOpsStore(dsn)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="DevOps persistence is not configured") from exc


def _actor(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    return f"api:{principal.subject}" if principal is not None else "api:development"


def _validate_job_payload(job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(payload, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="DevOps job payload must be finite JSON data") from exc
    if len(encoded) > 64_000:
        raise HTTPException(status_code=413, detail="DevOps job payload exceeds 64 KiB")
    if job_type == "synthetic_ingest":
        rows = payload.get("rows", 2000)
        metric = payload.get("metric_name", "cpu_utilization")
        resource = payload.get("resource_id", "demo-local")
        if type(rows) is not int or not 300 <= rows <= 20_000:
            raise HTTPException(status_code=422, detail="synthetic rows must be between 300 and 20000")
        if not isinstance(metric, str) or not METRIC_PATTERN.fullmatch(metric):
            raise HTTPException(status_code=422, detail="metric_name is invalid")
        if not isinstance(resource, str) or not RESOURCE_PATTERN.fullmatch(resource):
            raise HTTPException(status_code=422, detail="resource_id is invalid")
    elif job_type in {"prometheus_collect", "anomaly_scan"}:
        metric, resource = payload.get("metric_name"), payload.get("resource_id")
        if not isinstance(metric, str) or not METRIC_PATTERN.fullmatch(metric):
            raise HTTPException(status_code=422, detail="metric_name is required and must be valid")
        if not isinstance(resource, str) or not RESOURCE_PATTERN.fullmatch(resource):
            raise HTTPException(status_code=422, detail="resource_id is required and must be valid")
        if job_type == "prometheus_collect":
            from datetime import datetime
            try:
                start = datetime.fromisoformat(str(payload["start"]).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(payload["end"]).replace("Z", "+00:00"))
            except (KeyError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="Prometheus start/end must be ISO-8601 timestamps") from exc
            if start.tzinfo is None or end.tzinfo is None or end <= start:
                raise HTTPException(status_code=422, detail="Prometheus start/end must be ordered timezone-aware timestamps")
            step = payload.get("step_seconds", 60)
            if type(step) is not int or not 15 <= step <= 3600:
                raise HTTPException(status_code=422, detail="step_seconds must be between 15 and 3600")
        if job_type == "anomaly_scan" and "limit" in payload:
            if type(payload["limit"]) is not int or not 40 <= payload["limit"] <= 100000:
                raise HTTPException(status_code=422, detail="anomaly scan limit must be between 40 and 100000")
    elif job_type == "capacity_recommendation":
        predicted, current = payload.get("predicted_values"), payload.get("current_capacity")
        if not isinstance(predicted, list) or not 1 <= len(predicted) <= 10080:
            raise HTTPException(status_code=422, detail="predicted_values must contain 1-10080 numbers")
        if any(type(value) not in {int, float} or not math.isfinite(float(value)) for value in predicted):
            raise HTTPException(status_code=422, detail="predicted_values must contain finite numbers")
        if type(current) not in {int, float} or not math.isfinite(float(current)) or current <= 0:
            raise HTTPException(status_code=422, detail="current_capacity must be positive and finite")
    return payload


def create_devops_router(permission_dependency_factory) -> APIRouter:
    router = APIRouter(prefix="/v1/devops", tags=["Predictive DevOps"])

    @router.get("/agent-card")
    def read_agent_card(tenant_id: str = Depends(permission_dependency_factory("read"))):
        return agent_card()

    @router.get("/overview")
    def overview(request: Request, tenant_id: str = Depends(permission_dependency_factory("read"))):
        result = _queue(request).overview(tenant_id)
        store = _store()
        result["latest_models"] = store.list_model_runs(tenant_id, limit=5)
        result["recent_incidents"] = store.list_incidents(tenant_id, limit=5)
        result["pending_actions"] = store.list_action_proposals(tenant_id, limit=10)
        return result

    @router.post("/jobs", status_code=202)
    def create_job(
        body: DevOpsJobRequest, request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        tenant_id: str = Depends(permission_dependency_factory("operate")),
    ):
        task_id, created, status = _queue(request).enqueue(
            str(uuid4()), tenant_id, body.job_type, _validate_job_payload(body.job_type, body.payload),
            max_attempts=body.max_attempts, idempotency_key=idempotency_key, audit_actor=_actor(request),
        )
        return {"task_id": task_id, "tenant_id": tenant_id, "status": status, "created": created}

    @router.post("/training-runs", status_code=202)
    def create_training_run(
        body: DevOpsTrainingRequest, request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        tenant_id: str = Depends(permission_dependency_factory("admin")),
    ):
        task_id, created, status = _queue(request).enqueue(
            str(uuid4()), tenant_id, "train_forecaster", body.model_dump(),
            max_attempts=2, idempotency_key=idempotency_key, audit_actor=_actor(request),
        )
        return {"task_id": task_id, "tenant_id": tenant_id, "status": status, "created": created}

    @router.get("/jobs")
    def list_jobs(
        request: Request,
        status_filter: Literal["queued", "running", "succeeded", "failed", "cancelled"] | None = Query(default=None, alias="status"),
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=100000),
        tenant_id: str = Depends(permission_dependency_factory("read")),
    ):
        return _queue(request).list(tenant_id, status=status_filter, limit=limit, offset=offset)

    @router.get("/jobs/{task_id}")
    def get_job(task_id: str, request: Request, tenant_id: str = Depends(permission_dependency_factory("read"))):
        result = _queue(request).get(task_id, tenant_id)
        if result is None:
            raise HTTPException(status_code=404, detail="DevOps job not found")
        return result

    @router.post("/jobs/{task_id}/retry", status_code=202)
    def retry_job(task_id: str, request: Request, tenant_id: str = Depends(permission_dependency_factory("admin"))):
        if not _queue(request).retry(task_id, tenant_id, _actor(request)):
            raise HTTPException(status_code=409, detail="DevOps job is not retryable")
        return {"task_id": task_id, "status": "queued"}

    @router.delete("/jobs/{task_id}", status_code=202)
    def cancel_job(task_id: str, request: Request, tenant_id: str = Depends(permission_dependency_factory("admin"))):
        if not _queue(request).cancel(task_id, tenant_id, _actor(request)):
            raise HTTPException(status_code=409, detail="DevOps job cannot be cancelled")
        return {"task_id": task_id, "status": "cancelled"}

    @router.get("/models")
    def list_models(limit: int = Query(default=20, ge=1, le=100), tenant_id: str = Depends(permission_dependency_factory("read"))):
        return {"items": _store().list_model_runs(tenant_id, limit=limit)}

    @router.get("/incidents")
    def list_incidents(limit: int = Query(default=50, ge=1, le=200), tenant_id: str = Depends(permission_dependency_factory("read"))):
        return {"items": _store().list_incidents(tenant_id, limit=limit)}

    @router.get("/actions")
    def list_actions(limit: int = Query(default=50, ge=1, le=200), tenant_id: str = Depends(permission_dependency_factory("read"))):
        return {"items": _store().list_action_proposals(tenant_id, limit=limit)}

    @router.post("/actions/{proposal_id}/approve")
    def approve_action(proposal_id: str, request: Request, body: dict[str, str] | None = None,
                       tenant_id: str = Depends(permission_dependency_factory("admin"))):
        reason = (body or {}).get("reason", "")
        if not _store().decide_action_proposal(tenant_id, proposal_id, approved=True, actor=_actor(request), reason=reason):
            raise HTTPException(status_code=409, detail="Action proposal is not pending approval")
        return {"proposal_id": proposal_id, "status": "approved", "execution_enabled": False}

    @router.post("/actions/{proposal_id}/reject")
    def reject_action(proposal_id: str, request: Request, body: dict[str, str] | None = None,
                      tenant_id: str = Depends(permission_dependency_factory("admin"))):
        reason = (body or {}).get("reason", "")
        if not _store().decide_action_proposal(tenant_id, proposal_id, approved=False, actor=_actor(request), reason=reason):
            raise HTTPException(status_code=409, detail="Action proposal is not pending approval")
        return {"proposal_id": proposal_id, "status": "rejected", "execution_enabled": False}

    return router
