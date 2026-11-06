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
from .http_limits import BodyLimitMiddleware
from .queue import IdempotencyConflictError, PostgresTaskQueue

MAX_AGENT_REQUEST_BYTES = int(os.getenv("RUSA_AGENT_MAX_REQUEST_BYTES", "262144"))
if MAX_AGENT_REQUEST_BYTES <= 0:
    raise RuntimeError("RUSA_AGENT_MAX_REQUEST_BYTES must be positive")


class CreateAgentTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=4000)
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_branch: str = Field(default="main", min_length=1, max_length=128)
    work_branch: str | None = Field(default=None, min_length=1, max_length=128)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    context: str = Field(default="", max_length=16000)


def configured_repositories() -> set[str]:
    raw = os.getenv("RUSA_AGENT_ALLOWED_REPOSITORIES", "")
    repositories = {item.strip().lower() for item in raw.split(",") if item.strip()}
    if not repositories or any(
        not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", item)
        for item in repositories
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RUSA_AGENT_ALLOWED_REPOSITORIES is missing or invalid",
        )
    return repositories


def configured_tenant_repository_map() -> dict[str, set[str]]:
    global_repositories = configured_repositories()
    raw = os.getenv("RUSA_AGENT_TENANT_REPOSITORIES", "")
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RUSA_AGENT_TENANT_REPOSITORIES is missing or invalid JSON",
        ) from exc
    if not isinstance(configured, dict) or not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RUSA_AGENT_TENANT_REPOSITORIES must map tenants to repositories",
        )
    tenant_map: dict[str, set[str]] = {}
    for tenant_id, values in configured.items():
        if (
            not isinstance(tenant_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", tenant_id)
            or not isinstance(values, list)
            or not values
            or not all(isinstance(item, str) for item in values)
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Tenant repository mapping contains an invalid tenant or repository list",
            )
        repositories = {item.strip().lower() for item in values}
        if (
            len(repositories) != len(values)
            or any(not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", item)
                   for item in repositories)
            or not repositories.issubset(global_repositories)
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Tenant repository mapping must use unique repositories from the global allowlist",
            )
        tenant_map[tenant_id] = repositories
    raw_keys = os.getenv("RUSA_AGENT_TENANT_KEYS", "")
    try:
        keys = json.loads(raw_keys)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent tenant credential configuration is invalid",
        ) from exc
    if (
        not isinstance(keys, dict)
        or set(tenant_map) != set(keys)
        or any(
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", key)
            or not isinstance(value, str)
            or len(value) < 32
            or not value.isascii()
            for key, value in keys.items()
        )
        or len(set(keys.values())) != len(keys)
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Every tenant must have one unique API credential and one repository assignment",
        )
    return tenant_map


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
    ) or len(set(tenant_keys.values())) != len(tenant_keys):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent tenant credentials must be unique ASCII keys of at least 32 characters",
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
    configured_tenant_repository_map()
    queue = PostgresTaskQueue(dsn)
    if not queue.check_ready():
        raise RuntimeError("Agent task schema or tenant RLS policy is not initialized")
    application.state.queue = queue
    yield


app = FastAPI(
    title="RUSA-AI Agent API",
    lifespan=lifespan,
    description="Tenant-scoped task submission and status API.",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(BodyLimitMiddleware, max_bytes=lambda: MAX_AGENT_REQUEST_BYTES)


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
    tenant_repositories = configured_tenant_repository_map().get(tenant_id)
    if tenant_repositories is None or body.repository.lower() not in tenant_repositories:
        raise HTTPException(
            status_code=403,
            detail="Repository is not assigned to the authenticated tenant",
        )
    task_id = uuid.uuid4().hex
    expected_branch = CodingTask.expected_work_branch_for(
        tenant_id,
        idempotency_key or "",
        task_id,
    )
    if body.work_branch is not None and body.work_branch != expected_branch:
        raise HTTPException(
            status_code=422,
            detail="work_branch is server-generated and must use the tenant-scoped task namespace",
        )
    try:
        task = CodingTask(
            tenant_id=tenant_id,
            objective=body.objective,
            repository=RepositoryTarget(
                full_name=body.repository,
                base_branch=body.base_branch,
                work_branch=expected_branch,
            ),
            acceptance_criteria=tuple(body.acceptance_criteria),
            context=body.context,
            idempotency_key=idempotency_key or "",
            task_id=task_id,
        )
        task_id = request.app.state.queue.enqueue(task)
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Agent task queue is temporarily unavailable",
        ) from exc
    return {
        "task_id": task_id,
        "status": "queued",
        "tenant_id": tenant_id,
        "work_branch": expected_branch,
    }


@app.get("/v1/agent/tasks/{task_id}")
def get_task(
    task_id: str,
    request: Request,
    tenant_id: str = Depends(authenticate_tenant),
) -> dict[str, Any]:
    try:
        result = request.app.state.queue.get(tenant_id, task_id)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Agent task queue is temporarily unavailable",
        ) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task_id": task_id, "tenant_id": tenant_id, **result}
