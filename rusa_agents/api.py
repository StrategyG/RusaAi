"""Tenant-scoped task submission API for the autonomous agent runtime."""

from __future__ import annotations

import hmac
import asyncio
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
from .queue import IdempotencyConflictError, PostgresTaskQueue

MAX_AGENT_REQUEST_BYTES = int(os.getenv("RUSA_AGENT_MAX_REQUEST_BYTES", "262144"))

class RequestGuardMiddleware:
    """Bound and buffer every HTTP request body, including chunked bodies."""

    def __init__(self, app, max_request_bytes, request_timeout_seconds: float = 15.0):
        if request_timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        self.app = app
        self.max_request_bytes = max_request_bytes
        self.request_timeout_seconds = request_timeout_seconds

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        limit = self.max_request_bytes() if callable(self.max_request_bytes) else self.max_request_bytes
        if not isinstance(limit, int) or limit <= 0:
            raise RuntimeError("request body size limit must be a positive integer")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in {
                        b"x-request-id", b"x-content-type-options", b"cache-control",
                    }
                ]
                headers.extend([
                    (b"x-request-id", request_id.encode("ascii")),
                    (b"x-content-type-options", b"nosniff"),
                    (b"cache-control", b"no-store"),
                ])
                message["headers"] = headers
            await send(message)

        async def send_error(code, detail):
            response = JSONResponse(status_code=code, content={"detail": detail})
            await response(scope, receive, send_with_headers)

        raw_length = dict(scope.get("headers", [])).get(b"content-length")
        if raw_length is not None:
            try:
                declared_length = int(raw_length.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                await send_error(400, "Invalid Content-Length header")
                return
            if declared_length < 0:
                await send_error(400, "Invalid Content-Length header")
                return
            if declared_length > limit:
                await send_error(413, "Request body exceeds the configured size limit")
                return

        buffered_messages = []
        received_bytes = 0
        deadline = asyncio.get_running_loop().time() + self.request_timeout_seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await send_error(408, "Request body was not received before the timeout")
                return
            try:
                message = await asyncio.wait_for(receive(), timeout=remaining)
            except asyncio.TimeoutError:
                await send_error(408, "Request body was not received before the timeout")
                return
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            received_bytes += len(message.get("body", b""))
            if received_bytes > limit:
                await send_error(413, "Request body exceeds the configured size limit")
                return
            buffered_messages.append(message)
            if not message.get("more_body", False):
                break

        async def replay_receive():
            if buffered_messages:
                return buffered_messages.pop(0)
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send_with_headers)


def tenant_repository_allowlist(tenant_id: str) -> frozenset[str]:
    raw = os.getenv("RUSA_AGENT_TENANT_REPOSITORIES", "")
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Per-tenant repository allowlists are not configured",
        )
    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tenant repository allowlist configuration is invalid",
        ) from exc
    if not isinstance(mapping, dict) or not mapping:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tenant repository allowlist configuration is invalid",
        )
    for key, repositories in mapping.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", key)
            or not isinstance(repositories, list)
            or not repositories
            or any(
                not isinstance(repository, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                for repository in repositories
            )
            or len(set(repositories)) != len(repositories)
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Tenant repository allowlist configuration is invalid",
            )
    if tenant_id not in mapping:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant has no repository access")
    return frozenset(mapping[tenant_id])



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

app.add_middleware(RequestGuardMiddleware, max_request_bytes=lambda: MAX_AGENT_REQUEST_BYTES)





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
    if body.repository not in tenant_repository_allowlist(tenant_id):
        raise HTTPException(status_code=403, detail="Repository is not allowlisted for this tenant")
    try:
        RepositoryTarget(
            full_name=body.repository,
            base_branch=body.base_branch,
            work_branch=body.work_branch,
        )
        task_id_seed = uuid.uuid4().hex
        work_branch = CodingTask.expected_work_branch_for(
            tenant_id, idempotency_key or "", task_id_seed,
        )
        task = CodingTask(
            task_id=task_id_seed,
            tenant_id=tenant_id,
            objective=body.objective,
            repository=RepositoryTarget(
                full_name=body.repository,
                base_branch=body.base_branch,
                work_branch=work_branch,
            ),
            acceptance_criteria=tuple(body.acceptance_criteria),
            context=body.context,
            idempotency_key=idempotency_key or "",
        )
        task_id = request.app.state.queue.enqueue(task)
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "task_id": task_id,
        "status": "queued",
        "tenant_id": tenant_id,
        "work_branch": work_branch,
    }


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
