"""Tenant-scoped task submission API for the autonomous agent runtime."""

from __future__ import annotations

import hmac
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .contracts import CodingTask, RepositoryTarget
from .queue import PostgresTaskQueue

MAX_AGENT_REQUEST_BYTES = int(os.getenv("RUSA_AGENT_MAX_REQUEST_BYTES", "262144"))


class CreateAgentTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=4000)
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_branch: str = Field(default="main", min_length=1, max_length=128)
    work_branch: str = Field(min_length=1, max_length=128)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    context: str = Field(default="", max_length=16000)


def authenticate_tenant(
    api_key: str | None = Header(default=None, alias="X-API-Key"),
    tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
) -> str:
    if not tenant_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", tenant_id):
        raise HTTPException(status_code=400, detail="A valid X-Tenant-ID header is required")
    raw_keys = os.getenv("RUSA_AGENT_TENANT_KEYS", "")
    if not raw_keys:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Per-tenant agent API credentials are not configured",
        )
    try:
        tenant_keys = json.loads(raw_keys)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent tenant credential configuration is invalid",
        ) from exc
    if not isinstance(tenant_keys, dict) or not tenant_keys:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent tenant credential configuration is invalid",
        )
    if any(
        not isinstance(key, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", key)
        or not isinstance(value, str)
        or len(value) < 32
        or not value.isascii()
        for key, value in tenant_keys.items()
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent tenant credentials must use valid tenant IDs and ASCII keys of at least 32 characters",
        )
    expected = tenant_keys.get(tenant_id)
    if (
        not expected
        or not api_key
        or not api_key.isascii()
        or not hmac.compare_digest(api_key, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials for the requested tenant",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return tenant_id


@asynccontextmanager
async def lifespan(application: FastAPI):
    dsn = os.getenv("RUSA_AGENT_API_DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("RUSA_AGENT_API_DATABASE_URL is required")
    queue = PostgresTaskQueue(dsn)
    if not queue.check_ready():
        raise RuntimeError("Agent task schema is not initialized")
    application.state.queue = queue
    yield


app = FastAPI(
    title="RUSA-AI Agent API",
    description="Tenant-scoped task submission and status API.",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def request_guard(request: Request, call_next):
    request_id = uuid.uuid4().hex
    request.state.request_id = request_id
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            size = int(content_length)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"detail": "Invalid Content-Length header"},
                headers={"X-Request-ID": request_id},
            )
        if size < 0:
            return JSONResponse(
                status_code=400,
                content={"detail": "Invalid Content-Length header"},
                headers={"X-Request-ID": request_id},
            )
        if size > MAX_AGENT_REQUEST_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": "Request body exceeds the configured size limit"},
                headers={"X-Request-ID": request_id},
            )
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok", "service": "rusa-agent-api"}


@app.get("/health/ready")
def ready(request: Request) -> dict[str, str]:
    queue = getattr(request.app.state, "queue", None)
    try:
        if not queue or not queue.check_ready():
            raise HTTPException(status_code=503, detail="Task queue is unavailable")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Task queue is unavailable") from exc
    return {"status": "ready", "service": "rusa-agent-api"}


@app.post("/v1/agent/tasks", status_code=status.HTTP_202_ACCEPTED)
def create_task(
    body: CreateAgentTaskRequest,
    request: Request,
    tenant_id: str = Depends(authenticate_tenant),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    if idempotency_key is not None and not re.fullmatch(
        r"[A-Za-z0-9_.:-]{1,128}", idempotency_key
    ):
        raise HTTPException(status_code=400, detail="Invalid Idempotency-Key")
    try:
        task = CodingTask(
            tenant_id=tenant_id,
            objective=body.objective,
            repository=RepositoryTarget(
                full_name=body.repository,
                base_branch=body.base_branch,
                work_branch=body.work_branch,
            ),
            acceptance_criteria=tuple(body.acceptance_criteria),
            context=body.context,
            idempotency_key=idempotency_key or "",
        )
        task_id = request.app.state.queue.enqueue(task)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"task_id": task_id, "status": "queued", "tenant_id": tenant_id}


@app.get("/v1/agent/tasks/{task_id}")
def get_task(
    task_id: str,
    request: Request,
    tenant_id: str = Depends(authenticate_tenant),
) -> dict[str, Any]:
    result = request.app.state.queue.get(tenant_id, task_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task_id": task_id, "tenant_id": tenant_id, **result}
