"""RUSA-AI API. Inference fails closed until a real backend is injected."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import Response
from fastapi.security import APIKeyHeader
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field, field_validator

from rusa_agents.audit import PostgresTaskPolicyAudit, record_task_policy
from rusa_agents.config import RuntimeConfig
from rusa_agents.http_limits import BodyLimitMiddleware
from rusa_agents.metrics import API_REQUEST_DURATION, API_REQUESTS
from rusa_agents.providers import OpenAIChatInferenceProvider
from rusa_agents.queue import PostgresTaskQueue
from rusa_agents.rate_limit import PostgresRateLimiter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("rusa-ai-serve")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class InferenceProvider(Protocol):
    async def check_ready(self) -> bool: ...

    async def complete(self, messages: list[dict[str, str]], **parameters: Any) -> str: ...


inference_provider: InferenceProvider | None = None


def set_inference_provider(provider: InferenceProvider | None) -> None:
    """Inject the real model-serving adapter during application composition."""
    global inference_provider
    inference_provider = provider


@asynccontextmanager
async def lifespan(application: FastAPI):
    config = RuntimeConfig.from_env()
    config.validate_api()
    application.state.config = config
    application.state.queue = None
    application.state.rate_limiter = None
    application.state.task_policy_audit = None
    if config.model_base_url:
        set_inference_provider(
            OpenAIChatInferenceProvider(
                config.model_base_url,
                default_model=config.model_name,
                api_key=os.getenv("RUSA_MODEL_API_KEY"),
            )
        )
    else:
        set_inference_provider(None)
    if config.api_database_url:
        queue = PostgresTaskQueue(config.api_database_url)
        queue.initialize()
        application.state.queue = queue
        limiter = PostgresRateLimiter(config.api_database_url)
        limiter.initialize()
        application.state.rate_limiter = limiter
        task_policy_audit = PostgresTaskPolicyAudit(config.api_database_url)
        if config.environment == "production":
            task_policy_audit.verify_ready("rusa_api_runtime")
        application.state.task_policy_audit = task_policy_audit
    yield
    set_inference_provider(None)


app = FastAPI(
    title="RUSA-AI Serving and Agent API",
    description="Authenticated inference, task intake, and A2A validation API.",
    version="1.1.0",
    lifespan=lifespan,
)

def _request_body_limit() -> int:
    try:
        limit = int(os.getenv("RUSA_MAX_REQUEST_BYTES", "1048576"))
    except ValueError as exc:
        raise RuntimeError("RUSA_MAX_REQUEST_BYTES must be an integer") from exc
    if not 1024 <= limit <= 50_000_000:
        raise RuntimeError("RUSA_MAX_REQUEST_BYTES must be between 1024 and 50000000")
    return limit


def _request_body_timeout() -> float:
    try:
        timeout = float(os.getenv("RUSA_REQUEST_BODY_TIMEOUT_SECONDS", "15"))
    except ValueError as exc:
        raise RuntimeError("RUSA_REQUEST_BODY_TIMEOUT_SECONDS must be numeric") from exc
    if not 0.1 <= timeout <= 60:
        raise RuntimeError("RUSA_REQUEST_BODY_TIMEOUT_SECONDS must be between 0.1 and 60")
    return timeout


app.add_middleware(
    BodyLimitMiddleware,
    max_bytes=_request_body_limit,
    timeout=_request_body_timeout(),
)


@app.middleware("http")
async def collect_request_metrics(request: Request, call_next):
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        route_name = getattr(route, "path", "unmatched")
        API_REQUESTS.labels(request.method, route_name, str(status_code)).inc()
        API_REQUEST_DURATION.labels(request.method, route_name).observe(
            time.perf_counter() - started
        )


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=100_000)


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(default="rusa-ai", min_length=1, max_length=128)
    messages: list[ChatMessage] = Field(min_length=1, max_length=128)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=0.9, gt=0.0, le=1.0)
    max_tokens: int = Field(default=1024, ge=1, le=4096)
    stream: bool = False


class ChatChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatChoice]


class TaskValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    envelope: dict[str, Any]


class TaskValidationResponse(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)


class RepositoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_branch: str = Field(
        default="main", pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,180}$"
    )

    @field_validator("full_name")
    @classmethod
    def validate_repository_segments(cls, value: str) -> str:
        owner, repo = value.split("/", 1)
        if owner in {".", ".."} or repo in {".", ".."}:
            raise ValueError("Repository owner/name segments are invalid")
        return value

    @field_validator("base_branch")
    @classmethod
    def validate_base_branch(cls, value: str) -> str:
        if ".." in value or "//" in value or value.endswith("/"):
            raise ValueError("Base branch name is invalid")
        return value


class AICodeTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=8000)
    context: str = Field(default="", max_length=20000)
    repository: RepositoryInput
    acceptance_criteria: list[str] = Field(min_length=1, max_length=30)

    @field_validator("objective")
    @classmethod
    def validate_objective(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Objective cannot be blank")
        return value

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_acceptance_criteria(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("Acceptance criteria cannot contain blank items")
        if any(len(item) > 1000 for item in value):
            raise ValueError("Each acceptance criterion must be 1000 characters or fewer")
        return value


class AICodeTaskResponse(BaseModel):
    task_id: str
    status: str
    tenant_id: str
    work_branch_prefix: str


def verify_api_key(request: Request, api_key: str | None = Depends(api_key_header)) -> str:
    return authenticate_api_key(request, api_key)


def authenticate_api_key(request: Request, api_key: str | None, *, require_key: bool = False) -> str:
    config = RuntimeConfig.from_env()
    if not config.api_key_principals and not config.api_keys:
        if require_key or config.environment == "production":
            raise HTTPException(status_code=503, detail="API authentication is not configured")
        return os.getenv("RUSA_DEFAULT_TENANT", "development")
    if not api_key or len(api_key.encode("utf-8")) < 32:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")

    supplied_digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    principal = None
    for stored_digest, candidate in config.api_key_principals.items():
        if hmac.compare_digest(supplied_digest, stored_digest):
            principal = candidate
            break

    # Raw-key mappings are retained only as a development compatibility path.
    if principal is None and config.environment != "production":
        for candidate, mapped_tenant in config.api_keys.items():
            if hmac.compare_digest(api_key.encode("utf-8"), candidate.encode("utf-8")):
                from rusa_agents.config import ApiPrincipal
                principal = ApiPrincipal(
                    subject=f"legacy:{mapped_tenant}",
                    tenant_id=mapped_tenant,
                    roles=("tenant_admin",),
                )
                break
    if principal is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")

    request.state.principal = principal
    tenant_id = principal.tenant_id

    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is not None:
        if request.url.path == "/v1/chat/completions":
            limit = int(os.getenv("RUSA_RATE_LIMIT_CHAT_PER_MINUTE", "60"))
        elif request.method == "POST" and request.url.path == "/v1/aicode/tasks":
            limit = int(os.getenv("RUSA_RATE_LIMIT_TASKS_PER_MINUTE", "10"))
        else:
            limit = int(os.getenv("RUSA_RATE_LIMIT_DEFAULT_PER_MINUTE", "120"))
        try:
            route = request.scope.get("route")
            route_key = getattr(route, "path", request.url.path)
            allowed, retry_after = limiter.hit(
                tenant_id, f"{request.method}:{route_key}", limit, 60
            )
        except Exception:
            logger.exception("Tenant rate limiter is unavailable")
            raise HTTPException(status_code=503, detail="API rate limiter is unavailable") from None
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Tenant request rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )
    return tenant_id


TASK_ROLE_PERMISSIONS = {
    "platform_admin": frozenset({"task:create", "task:read", "task:retry", "task:cancel"}),
    "tenant_admin": frozenset({"task:create", "task:read", "task:retry", "task:cancel"}),
    "developer": frozenset({"task:create", "task:read"}),
    "read_only": frozenset({"task:read"}),
}


def _request_task_id(request: Request) -> str | None:
    try:
        return str(UUID(request.path_params.get("task_id", "")))
    except (TypeError, ValueError):
        return None


def audit_api_task(
    request: Request,
    tenant_id: str | None,
    task_id: str | None,
    action: str,
    allowed: bool,
) -> None:
    principal = getattr(request.state, "principal", None)
    actor = f"api:{principal.subject}" if principal is not None else "unauthenticated"
    config = getattr(request.app.state, "config", None) or RuntimeConfig.from_env()
    sink = getattr(request.app.state, "task_policy_audit", None)
    record_task_policy(
        tenant_id, task_id, actor, action, allowed,
        sink=sink, required=config.environment == "production",
    )


def require_task_permission(permission: str):
    def dependency(
        request: Request,
        api_key: str | None = Depends(api_key_header),
    ) -> str:
        try:
            tenant_id = authenticate_api_key(request, api_key, require_key=True)
        except HTTPException:
            try:
                audit_api_task(
                    request, None, _request_task_id(request), "task.authenticate", False
                )
            except RuntimeError:
                raise HTTPException(
                    status_code=503, detail="Task policy audit is unavailable"
                ) from None
            raise

        principal = request.state.principal
        permissions = frozenset().union(
            *(TASK_ROLE_PERMISSIONS[role] for role in principal.roles)
        )
        if permission not in permissions:
            try:
                audit_api_task(
                    request, tenant_id, _request_task_id(request),
                    permission.replace(":", "."), False,
                )
            except RuntimeError:
                raise HTTPException(
                    status_code=503, detail="Task policy audit is unavailable"
                ) from None
            raise HTTPException(status_code=403, detail="Insufficient task permissions")
        return tenant_id

    return dependency


def verify_metrics_key(x_api_key: str | None = Header(default=None)) -> None:
    """Metrics are operator-only and never accept tenant-scoped API keys."""
    expected = os.getenv("RUSA_METRICS_API_KEY")
    if not expected or not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid metrics API key")


@app.get("/metrics", dependencies=[Depends(verify_metrics_key)])
def metrics_endpoint():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    return {
        "status": "healthy",
        "service": "rusa-ai-serve",
        "version": "1.1.0",
        "environment": os.getenv("ENVIRONMENT", "development"),
    }


@app.get("/health/ready")
async def readiness_check():
    if inference_provider is None:
        raise HTTPException(status_code=503, detail="Inference backend is not configured")
    try:
        await inference_provider.check_ready()
    except Exception:
        logger.exception("Inference readiness probe failed")
        raise HTTPException(status_code=503, detail="Inference backend is unavailable") from None

    config = getattr(app.state, "config", None)
    if config is None:
        config = RuntimeConfig.from_env()
    queue = getattr(app.state, "queue", None)
    task_policy_audit = getattr(app.state, "task_policy_audit", None)
    if config.environment == "production" and queue is None:
        raise HTTPException(status_code=503, detail="Durable task queue is not configured")
    if config.environment == "production" and task_policy_audit is None:
        raise HTTPException(status_code=503, detail="Task policy audit is not configured")
    if task_policy_audit is not None:
        try:
            if not await asyncio.to_thread(task_policy_audit.ping):
                raise RuntimeError("Task policy audit ping failed")
        except Exception:
            logger.exception("Task policy audit readiness probe failed")
            raise HTTPException(status_code=503, detail="Task policy audit store is unavailable") from None
    if queue is not None:
        try:
            if not await asyncio.to_thread(queue.ping):
                raise RuntimeError("Database ping returned an unexpected result")
        except Exception:
            logger.exception("Database readiness probe failed")
            raise HTTPException(status_code=503, detail="Task database is unavailable") from None
    return {"status": "ready", "inference": "reachable", "database": "reachable" if queue else "not_configured"}


@app.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse,
    dependencies=[Depends(verify_api_key)],
)
async def chat_completions(request: ChatCompletionRequest):
    if request.stream:
        raise HTTPException(status_code=501, detail="Streaming is not enabled by this deployment")
    if inference_provider is None:
        raise HTTPException(status_code=503, detail="Inference backend is not configured")
    try:
        output = await inference_provider.complete(
            [message.model_dump() for message in request.messages],
            model=request.model,
            temperature=request.temperature,
            top_p=request.top_p,
            max_tokens=request.max_tokens,
        )
        now = int(time.time())
        return ChatCompletionResponse(
            id=f"rusa-chatcmpl-{uuid4().hex}",
            created=now,
            model=request.model,
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content=output),
                    finish_reason="stop",
                )
            ],
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Inference backend request failed")
        raise HTTPException(status_code=502, detail="Inference backend request failed") from None


@app.post(
    "/v1/validate-task",
    response_model=TaskValidationResponse,
    dependencies=[Depends(verify_api_key)],
)
def validate_task_envelope(request: TaskValidationRequest):
    import jsonschema

    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "a2a" / "task-envelope.schema.json"
    if not schema_path.is_file():
        raise HTTPException(status_code=503, detail="Task validation schema is unavailable")
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        )
        errors = [
            f"{'/'.join(str(part) for part in error.absolute_path)}: {error.message}"
            for error in validator.iter_errors(request.envelope)
        ]
        return TaskValidationResponse(valid=not errors, errors=errors)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Task schema validation failed")
        raise HTTPException(status_code=500, detail="Task schema validation failed") from None


@app.post("/v1/aicode/tasks", response_model=AICodeTaskResponse, status_code=202)
def create_aicode_task(
    request: AICodeTaskRequest,
    http_request: Request,
    tenant_id: str = Depends(require_task_permission("task:create")),
):
    config = RuntimeConfig.from_env()
    task_id = str(uuid4())
    allowed_repositories = config.tenant_repositories.get(tenant_id)
    if config.environment == "production" and not allowed_repositories:
        audit_api_task(http_request, tenant_id, task_id, "task.create", False)
        raise HTTPException(status_code=403, detail="Tenant has no repository permissions")
    if allowed_repositories is not None and request.repository.full_name not in allowed_repositories:
        audit_api_task(http_request, tenant_id, task_id, "task.create", False)
        raise HTTPException(status_code=403, detail="Repository is not authorized for this tenant")
    if not config.aicode_enabled:
        audit_api_task(http_request, tenant_id, task_id, "task.create", False)
        raise HTTPException(status_code=503, detail="AICode task intake is disabled")
    queue = getattr(app.state, "queue", None)
    if queue is None:
        audit_api_task(http_request, tenant_id, task_id, "task.create", False)
        raise HTTPException(status_code=503, detail="Durable task queue is not configured")
    work_branch_prefix = f"feature/aicode/{task_id[:12]}"
    payload = {
        "objective": request.objective,
        "context": request.context,
        "repository": {
            **request.repository.model_dump(),
            "work_branch_prefix": work_branch_prefix,
        },
        "acceptance_criteria": request.acceptance_criteria,
    }
    try:
        created = queue.enqueue(task_id, tenant_id, payload)
    except Exception:
        logger.exception("Could not enqueue AICode task")
        audit_api_task(http_request, tenant_id, task_id, "task.create", False)
        raise HTTPException(status_code=503, detail="Task queue is unavailable") from None
    audit_api_task(http_request, tenant_id, task_id, "task.create", created)
    if not created:
        raise HTTPException(status_code=409, detail="Task identifier conflict")
    return AICodeTaskResponse(
        task_id=task_id,
        status="queued",
        tenant_id=tenant_id,
        work_branch_prefix=work_branch_prefix,
    )


@app.get("/v1/aicode/tasks/{task_id}")
def get_aicode_task(
    task_id: UUID,
    http_request: Request,
    tenant_id: str = Depends(require_task_permission("task:read")),
):
    queue = getattr(app.state, "queue", None)
    if queue is None:
        audit_api_task(http_request, tenant_id, str(task_id), "task.read", False)
        raise HTTPException(status_code=503, detail="Durable task queue is not configured")
    try:
        result = queue.get(str(task_id), tenant_id)
    except Exception:
        logger.exception("Could not read AICode task")
        audit_api_task(http_request, tenant_id, str(task_id), "task.read", False)
        raise HTTPException(status_code=503, detail="Task queue is unavailable") from None
    audit_api_task(http_request, tenant_id, str(task_id), "task.read", result is not None)
    if result is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return result



@app.post("/v1/aicode/tasks/{task_id}/retry", status_code=202)
def retry_aicode_task(
    task_id: UUID,
    http_request: Request,
    tenant_id: str = Depends(require_task_permission("task:retry")),
):
    """Require a tenant-authorized retry after failure or human review."""
    if not RuntimeConfig.from_env().aicode_enabled:
        audit_api_task(http_request, tenant_id, str(task_id), "task.retry", False)
        raise HTTPException(status_code=503, detail="Task intake is disabled")
    queue = getattr(app.state, "queue", None)
    if queue is None:
        audit_api_task(http_request, tenant_id, str(task_id), "task.retry", False)
        raise HTTPException(status_code=503, detail="Durable task queue is not configured")
    try:
        retried = queue.retry(str(task_id), tenant_id)
    except Exception:
        logger.exception("Could not retry AICode task")
        audit_api_task(http_request, tenant_id, str(task_id), "task.retry", False)
        raise HTTPException(status_code=503, detail="Task queue is unavailable") from None
    audit_api_task(http_request, tenant_id, str(task_id), "task.retry", retried)
    if not retried:
        raise HTTPException(status_code=409, detail="Task is unavailable or not in a retryable state")
    return {"task_id": str(task_id), "status": "queued", "tenant_id": tenant_id}


@app.delete("/v1/aicode/tasks/{task_id}", status_code=202)
def cancel_aicode_task(
    task_id: UUID,
    http_request: Request,
    tenant_id: str = Depends(require_task_permission("task:cancel")),
):
    queue = getattr(app.state, "queue", None)
    if queue is None:
        audit_api_task(http_request, tenant_id, str(task_id), "task.cancel", False)
        raise HTTPException(status_code=503, detail="Durable task queue is not configured")
    try:
        cancelled = queue.cancel(str(task_id), tenant_id)
    except Exception:
        logger.exception("Could not cancel AICode task")
        audit_api_task(http_request, tenant_id, str(task_id), "task.cancel", False)
        raise HTTPException(status_code=503, detail="Task queue is unavailable") from None
    audit_api_task(http_request, tenant_id, str(task_id), "task.cancel", cancelled)
    if not cancelled:
        raise HTTPException(status_code=404, detail="Task not found or no longer cancellable")
    return {"task_id": str(task_id), "status": "cancelled"}
