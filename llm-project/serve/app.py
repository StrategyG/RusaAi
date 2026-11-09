"""RUSA-AI API. Inference fails closed until a real backend is injected."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from fastapi.security import APIKeyHeader
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.background import BackgroundTask

from rusa_agents.audit import PostgresTaskPolicyAudit, record_task_policy
from rusa_agents.config import RuntimeConfig
from rusa_agents.http_limits import BodyLimitMiddleware
from rusa_agents.metrics import API_REQUEST_DURATION, API_REQUESTS
from rusa_agents.providers import InferenceStreamEvent, OpenAIChatInferenceProvider
from rusa_agents.queue import PostgresTaskQueue
from rusa_agents.rate_limit import PostgresRateLimiter
from rusa_agents.secret_scan import contains_high_confidence_secret_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("rusa-ai-serve")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class InferenceProvider(Protocol):
    async def check_ready(self) -> bool: ...

    async def complete(self, messages: list[dict[str, str]], **parameters: Any) -> str: ...

    def stream(
        self, messages: list[dict[str, str]], **parameters: Any
    ) -> AsyncIterator[InferenceStreamEvent]: ...


class LocalModelInferenceProvider:
    """Adapt the verified local runtime to the API's existing provider contract."""

    def __init__(self, backend: Any):
        self.backend = backend

    async def check_ready(self) -> bool:
        return self.backend is not None

    async def complete(self, messages: list[dict[str, str]], **parameters: Any) -> str:
        result = await self.backend.generate(
            messages,
            max_new_tokens=parameters["max_tokens"],
            temperature=parameters["temperature"],
            top_p=parameters["top_p"],
        )
        return result.content

    async def stream(
        self, messages: list[dict[str, str]], **parameters: Any
    ) -> AsyncIterator[InferenceStreamEvent]:
        async for event in self.backend.stream(
            messages,
            max_new_tokens=parameters["max_tokens"],
            temperature=parameters["temperature"],
            top_p=parameters["top_p"],
        ):
            yield InferenceStreamEvent(
                content=event.delta or None,
                finish_reason=event.finish_reason,
                prompt_tokens=event.prompt_tokens or None,
                completion_tokens=event.completion_tokens or None,
            )

    async def close(self) -> None:
        await self.backend.close()


class InferenceAdmissionController:
    """Bound active inference and the number/time of requests waiting for capacity."""

    def __init__(self, max_active: int, max_waiting: int, queue_timeout: float):
        if not 1 <= max_active <= 256 or not 0 <= max_waiting <= 4096:
            raise ValueError("Inference admission limits are outside the allowed range")
        if not 0.1 <= queue_timeout <= 120:
            raise ValueError("Inference queue timeout must be between 0.1 and 120 seconds")
        self._semaphore = asyncio.Semaphore(max_active)
        self._lock = asyncio.Lock()
        self._max_waiting = max_waiting
        self._queue_timeout = queue_timeout
        self._waiting = 0

    async def acquire(self) -> None:
        async with self._lock:
            if self._semaphore.locked() and self._waiting >= self._max_waiting:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Inference capacity is full; retry later",
                    headers={"Retry-After": "2"},
                )
            self._waiting += 1
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self._queue_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Timed out waiting for inference capacity",
                headers={"Retry-After": "2"},
            ) from exc
        finally:
            async with self._lock:
                self._waiting -= 1

    def release(self) -> None:
        self._semaphore.release()


def _new_inference_admission_controller() -> InferenceAdmissionController:
    try:
        max_active = int(os.getenv("RUSA_MAX_CONCURRENT_INFERENCES", "8"))
        max_waiting = int(os.getenv("RUSA_MAX_WAITING_INFERENCES", "32"))
        queue_timeout = float(os.getenv("RUSA_INFERENCE_QUEUE_TIMEOUT_SECONDS", "10"))
    except ValueError as exc:
        raise RuntimeError("Inference admission settings must be numeric") from exc
    return InferenceAdmissionController(max_active, max_waiting, queue_timeout)


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
    application.state.inference_admission = _new_inference_admission_controller()
    local_backend = None
    try:
        local_model_path = os.getenv("RUSA_LOCAL_MODEL_PATH") or os.getenv("RUSA_MODEL_PATH")
        if local_model_path:
            from model_runtime import load_inference_backend, verify_model_artifact

            try:
                max_model_len = int(os.getenv("RUSA_MODEL_MAX_LENGTH") or os.getenv("RUSA_MAX_MODEL_LEN", "4096"))
                manifest_digest = os.getenv("RUSA_MODEL_MANIFEST_SHA256")
                verified = verify_model_artifact(
                    local_model_path,
                    expected_manifest_sha256=manifest_digest,
                    require_pinned_manifest=config.environment == "production",
                )
                local_backend = load_inference_backend(
                    str(verified.root),
                    backend_name=os.getenv("RUSA_INFERENCE_BACKEND", "transformers"),
                    timeout_seconds=float(os.getenv("RUSA_MODEL_TIMEOUT_SECONDS") or os.getenv("RUSA_GENERATION_TIMEOUT_SECONDS", "120")),
                    max_model_len=max_model_len,
                    max_num_seqs=int(os.getenv("RUSA_VLLM_MAX_NUM_SEQS") or os.getenv("RUSA_MAX_NUM_SEQS", "8")),
                    max_num_batched_tokens=int(os.getenv("RUSA_VLLM_MAX_BATCHED_TOKENS") or os.getenv("RUSA_MAX_NUM_BATCHED_TOKENS", "8192")),
                    tensor_parallel_size=int(os.getenv("RUSA_VLLM_TENSOR_PARALLEL_SIZE") or os.getenv("RUSA_TENSOR_PARALLEL_SIZE", "1")),
                    gpu_memory_utilization=float(os.getenv("RUSA_VLLM_GPU_MEMORY_UTILIZATION") or os.getenv("RUSA_GPU_MEMORY_UTILIZATION", "0.9")),
                )
            except (ImportError, ValueError, RuntimeError, OSError) as exc:
                set_inference_provider(None)
                raise RuntimeError("Configured local model runtime failed closed during startup") from exc
            set_inference_provider(LocalModelInferenceProvider(local_backend))
        elif config.model_base_url:
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

    finally:
        if local_backend is not None:
            await local_backend.close()
        set_inference_provider(None)


app = FastAPI(
    title="RUSA-AI Serving and Agent API",
    description="Authenticated inference, task intake, and A2A validation API.",
    version="1.1.0",
    lifespan=lifespan,
)
app.state.inference_admission = InferenceAdmissionController(8, 32, 10.0)


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

    role: Literal["system", "developer", "user", "assistant"]
    content: str = Field(min_length=1, max_length=100_000)


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(
        default_factory=lambda: os.getenv("RUSA_MODEL_NAME", "rusa-ai"),
        min_length=1,
        max_length=128,
    )
    messages: list[ChatMessage] = Field(min_length=1, max_length=128)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=0.9, gt=0.0, le=1.0)
    max_tokens: int = Field(default=1024, ge=1, le=4096)
    stream: bool = False


class ChatChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str


class ChatUsage(BaseModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatChoice]
    usage: ChatUsage | None = None


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
        if contains_high_confidence_secret_text(value):
            raise ValueError("Objective contains credential-like material; use the approved secret manager")
        return value

    @field_validator("context")
    @classmethod
    def validate_context(cls, value: str) -> str:
        if contains_high_confidence_secret_text(value):
            raise ValueError("Context contains credential-like material; use the approved secret manager")
        return value

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_acceptance_criteria(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("Acceptance criteria cannot contain blank items")
        if any(len(item) > 1000 for item in value):
            raise ValueError("Each acceptance criterion must be 1000 characters or fewer")
        if any(contains_high_confidence_secret_text(item) for item in value):
            raise ValueError("Acceptance criteria contain credential-like material; use the approved secret manager")
        return value


class AICodeTaskResponse(BaseModel):
    task_id: str
    status: str
    tenant_id: str
    work_branch_prefix: str


def verify_api_key(request: Request, api_key: str | None = Depends(api_key_header)) -> str:
    return authenticate_api_key(request, api_key)



@app.get("/v1/auth/session")
def read_api_session(
    request: Request,
    tenant_id: str = Depends(verify_api_key),
):
    """Return the identity bound to the supplied tenant API key; never return the key."""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        return {
            "subject": f"local-development:{tenant_id}",
            "tenant_id": tenant_id,
            "roles": ["tenant_admin"],
        }
    return {
        "subject": principal.subject,
        "tenant_id": principal.tenant_id,
        "roles": list(principal.roles),
    }


def authenticate_api_key(request: Request, api_key: str | None, *, require_key: bool = False) -> str:
    config = RuntimeConfig.from_env()
    if not config.api_key_principals and not config.api_keys:
        if require_key or config.environment == "production":
            raise HTTPException(status_code=503, detail="API authentication is not configured")
        from rusa_agents.config import ApiPrincipal
        tenant_id = os.getenv("RUSA_DEFAULT_TENANT", "development")
        request.state.principal = ApiPrincipal(
            subject=f"local-development:{tenant_id}",
            tenant_id=tenant_id,
            roles=("tenant_admin",),
        )
        return tenant_id
    if not api_key or (
        (config.api_key_principals or config.environment == "production")
        and len(api_key.encode("utf-8")) < 32
    ):
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
    *,
    persist: bool = True,
) -> None:
    principal = getattr(request.state, "principal", None)
    actor = f"api:{principal.subject}" if principal is not None else "unauthenticated"
    config = getattr(request.app.state, "config", None) or RuntimeConfig.from_env()
    sink = getattr(request.app.state, "task_policy_audit", None) if persist else None
    try:
        record_task_policy(
            tenant_id, task_id, actor, action, allowed,
            sink=sink, required=persist and config.environment == "production",
        )
    except RuntimeError:
        raise HTTPException(
            status_code=503, detail="Task policy audit is unavailable"
        ) from None


def _api_task_actor(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise RuntimeError("Task API principal is missing after authentication")
    return f"api:{principal.subject}"


def require_task_permission(permission: str):
    def dependency(
        request: Request,
        api_key: str | None = Depends(api_key_header),
    ) -> str:
        try:
            tenant_id = authenticate_api_key(request, api_key)
        except HTTPException:
            principal = getattr(request.state, "principal", None)
            if principal is None:
                audit_api_task(
                    request, None, _request_task_id(request), "task.authenticate", False
                )
            else:
                audit_api_task(
                    request,
                    principal.tenant_id,
                    _request_task_id(request),
                    permission.replace(":", "."),
                    False,
                )
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


INFERENCE_ALLOWED_ROLES = frozenset({"platform_admin", "tenant_admin", "developer"})


def require_inference_permission(
    request: Request,
    api_key: str | None = Depends(api_key_header),
) -> str:
    tenant_id = authenticate_api_key(request, api_key)
    principal = getattr(request.state, "principal", None)
    config = getattr(request.app.state, "config", None) or RuntimeConfig.from_env()
    if principal is None:
        if config.environment == "production":
            raise HTTPException(status_code=403, detail="Inference permission is not configured")
        return tenant_id
    if not INFERENCE_ALLOWED_ROLES.intersection(principal.roles):
        raise HTTPException(status_code=403, detail="Insufficient inference permissions")
    return tenant_id


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


def _configured_model_name(chat_request: ChatCompletionRequest, http_request: Request) -> str:
    config = getattr(http_request.app.state, "config", None) or RuntimeConfig.from_env()
    if chat_request.model != config.model_name:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model '{chat_request.model}' is not available",
        )
    return config.model_name


def _sse(data: dict[str, Any] | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
    return f"data: {payload}\n\n"


@app.get("/v1/models", dependencies=[Depends(verify_api_key)])
def list_models():
    config = getattr(app.state, "config", None) or RuntimeConfig.from_env()
    if inference_provider is None:
        raise HTTPException(status_code=503, detail="Inference backend is not configured")
    return {
        "object": "list",
        "data": [{"id": config.model_name, "object": "model", "owned_by": "rusa-ai"}],
    }


@app.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse,
    dependencies=[Depends(require_inference_permission)],
)
async def chat_completions(request: ChatCompletionRequest, http_request: Request):
    provider = inference_provider
    if provider is None:
        raise HTTPException(status_code=503, detail="Inference backend is not configured")
    model_name = _configured_model_name(request, http_request)
    messages = [message.model_dump() for message in request.messages]
    controller = getattr(http_request.app.state, "inference_admission", None)
    if controller is None:
        controller = InferenceAdmissionController(8, 32, 10.0)

    if not request.stream:
        await controller.acquire()
        try:
            output = await provider.complete(
                messages,
                model=model_name,
                temperature=request.temperature,
                top_p=request.top_p,
                max_tokens=request.max_tokens,
            )
        except HTTPException:
            raise
        except Exception:
            logger.exception("Inference backend request failed")
            raise HTTPException(
                status_code=502, detail="Inference backend request failed"
            ) from None
        finally:
            controller.release()

        return ChatCompletionResponse(
            id=f"rusa-chatcmpl-{uuid4().hex}",
            created=int(time.time()),
            model=model_name,
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content=output),
                    finish_reason="stop",
                )
            ],
        )

    stream_method = getattr(provider, "stream", None)
    if stream_method is None:
        raise HTTPException(
            status_code=501,
            detail="The configured inference provider does not support streaming",
        )
    await controller.acquire()
    completion_id = f"rusa-chatcmpl-{uuid4().hex}"
    created = int(time.time())
    released = False

    def release_stream_slot() -> None:
        nonlocal released
        if not released:
            released = True
            controller.release()

    async def event_stream():
        finish_reason: str | None = None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        try:
            yield _sse({
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            })
            async for event in stream_method(
                messages,
                model=model_name,
                temperature=request.temperature,
                top_p=request.top_p,
                max_tokens=request.max_tokens,
            ):
                if event.content:
                    yield _sse({
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_name,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": event.content},
                            "finish_reason": None,
                        }],
                    })
                if event.finish_reason is not None:
                    finish_reason = event.finish_reason
                if event.prompt_tokens is not None:
                    prompt_tokens = event.prompt_tokens
                if event.completion_tokens is not None:
                    completion_tokens = event.completion_tokens

            if finish_reason is None:
                yield _sse({
                    "error": {
                        "message": "Inference stream ended without a completion",
                        "type": "server_error",
                    }
                })
            else:
                yield _sse({
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                })
                if prompt_tokens is not None or completion_tokens is not None:
                    prompt = prompt_tokens or 0
                    completion = completion_tokens or 0
                    yield _sse({
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_name,
                        "choices": [],
                        "usage": {
                            "prompt_tokens": prompt,
                            "completion_tokens": completion,
                            "total_tokens": prompt + completion,
                        },
                    })
            yield _sse("[DONE]")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Streaming inference failed request_id=%s",
                getattr(http_request.state, "request_id", "unknown"),
            )
            yield _sse({
                "error": {
                    "message": "Inference stream failed",
                    "type": "server_error",
                }
            })
            yield _sse("[DONE]")
        finally:
            release_stream_slot()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        background=BackgroundTask(release_stream_slot),
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
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
    status_value = "queued"
    try:
        if idempotency_key is not None:
            task_id, _, status_value = queue.enqueue_idempotent(
                task_id,
                tenant_id,
                payload,
                idempotency_key,
                audit_actor=_api_task_actor(http_request),
            )
            work_branch_prefix = f"feature/aicode/{task_id[:12]}"
            accepted = True
        else:
            created = queue.enqueue(
                task_id, tenant_id, payload, audit_actor=_api_task_actor(http_request)
            )
            accepted = created
    except ValueError as exc:
        audit_api_task(http_request, tenant_id, task_id, "task.create", False)
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except Exception:
        logger.exception("Could not enqueue AICode task")
        audit_api_task(http_request, tenant_id, task_id, "task.create", False)
        raise HTTPException(status_code=503, detail="Task queue is unavailable") from None
    audit_api_task(
        http_request, tenant_id, task_id, "task.create", accepted, persist=False
    )
    if not accepted:
        raise HTTPException(status_code=409, detail="Task identifier conflict")
    return AICodeTaskResponse(
        task_id=task_id,
        status=status_value,
        tenant_id=tenant_id,
        work_branch_prefix=work_branch_prefix,
    )


@app.get("/v1/aicode/tasks")
def list_aicode_tasks(
    request: Request,
    status_filter: Literal[
        "queued", "running", "awaiting_approval", "succeeded", "failed", "cancelled"
    ] | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
    tenant_id: str = Depends(require_task_permission("task:read")),
):
    """List only the authenticated tenant's recent AICode tasks."""
    queue = getattr(app.state, "queue", None)
    if queue is None:
        audit_api_task(request, tenant_id, "*", "task.list", False)
        raise HTTPException(status_code=503, detail="Durable task queue is not configured")
    try:
        return queue.list(
            tenant_id,
            status_filter=status_filter,
            limit=limit,
            offset=offset,
            audit_actor=_api_task_actor(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:
        logger.exception("Could not list AICode tasks")
        audit_api_task(request, tenant_id, "*", "task.list", False)
        raise HTTPException(status_code=503, detail="Task queue is unavailable") from None


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
        result = queue.get(
            str(task_id), tenant_id, audit_actor=_api_task_actor(http_request)
        )
    except Exception:
        logger.exception("Could not read AICode task")
        audit_api_task(http_request, tenant_id, str(task_id), "task.read", False)
        raise HTTPException(status_code=503, detail="Task queue is unavailable") from None
    audit_api_task(
        http_request, tenant_id, str(task_id), "task.read", result is not None,
        persist=False,
    )
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
        raise HTTPException(status_code=503, detail="AICode task intake is disabled")
    queue = getattr(app.state, "queue", None)
    if queue is None:
        audit_api_task(http_request, tenant_id, str(task_id), "task.retry", False)
        raise HTTPException(status_code=503, detail="Durable task queue is not configured")
    try:
        retried = queue.retry(
            str(task_id), tenant_id, audit_actor=_api_task_actor(http_request)
        )
    except Exception:
        logger.exception("Could not retry AICode task")
        audit_api_task(http_request, tenant_id, str(task_id), "task.retry", False)
        raise HTTPException(status_code=503, detail="Task queue is unavailable") from None
    audit_api_task(
        http_request, tenant_id, str(task_id), "task.retry", retried, persist=False
    )
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
        cancelled = queue.cancel(
            str(task_id), tenant_id, audit_actor=_api_task_actor(http_request)
        )
    except Exception:
        logger.exception("Could not cancel AICode task")
        audit_api_task(http_request, tenant_id, str(task_id), "task.cancel", False)
        raise HTTPException(status_code=503, detail="Task queue is unavailable") from None
    audit_api_task(
        http_request, tenant_id, str(task_id), "task.cancel", cancelled, persist=False
    )
    if not cancelled:
        raise HTTPException(status_code=404, detail="Task not found or no longer cancellable")
    return {"task_id": str(task_id), "status": "cancelled"}
