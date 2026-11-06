"""RUSA-AI API. Inference fails closed until a real backend is injected."""
from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Literal, Protocol
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, field_validator

from rusa_agents.config import RuntimeConfig
from rusa_agents.providers import OpenAIChatInferenceProvider
from rusa_agents.queue import PostgresTaskQueue

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
    if config.database_url:
        queue = PostgresTaskQueue(config.database_url)
        queue.initialize()
        application.state.queue = queue
    yield
    set_inference_provider(None)


app = FastAPI(
    title="RUSA-AI Serving and Agent API",
    description="Authenticated inference, task intake, and A2A validation API.",
    version="1.1.0",
    lifespan=lifespan,
)


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=100_000)


class ChatCompletionRequest(BaseModel):
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
    envelope: dict[str, Any]


class TaskValidationResponse(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)


class RepositoryInput(BaseModel):
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
    work_branch: str


def verify_api_key(api_key: str | None = Depends(api_key_header)) -> str:
    config = RuntimeConfig.from_env()
    if not config.api_keys:
        if config.environment == "production":
            raise HTTPException(status_code=503, detail="API authentication is not configured")
        return os.getenv("RUSA_DEFAULT_TENANT", "development")
    if not api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing API key")
    tenant_id = None
    for candidate, mapped_tenant in config.api_keys.items():
        if hmac.compare_digest(api_key, candidate):
            tenant_id = mapped_tenant
    if tenant_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return tenant_id


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
    return {"status": "ready", "inference": "reachable"}


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
    tenant_id: str = Depends(verify_api_key),
):
    queue = getattr(app.state, "queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="Durable task queue is not configured")
    config = RuntimeConfig.from_env()
    allowed_repositories = config.tenant_repositories.get(tenant_id)
    if config.environment == "production" and not allowed_repositories:
        raise HTTPException(status_code=403, detail="Tenant has no repository permissions")
    if allowed_repositories is not None and request.repository.full_name not in allowed_repositories:
        raise HTTPException(status_code=403, detail="Repository is not authorized for this tenant")
    task_id = str(uuid4())
    work_branch = f"feature/aicode/{task_id[:12]}"
    payload = {
        "objective": request.objective,
        "context": request.context,
        "repository": {
            **request.repository.model_dump(),
            "work_branch": work_branch,
        },
        "acceptance_criteria": request.acceptance_criteria,
    }
    try:
        created = queue.enqueue(task_id, tenant_id, payload)
    except Exception:
        logger.exception("Could not enqueue AICode task")
        raise HTTPException(status_code=503, detail="Task queue is unavailable") from None
    if not created:
        raise HTTPException(status_code=409, detail="Task identifier conflict")
    return AICodeTaskResponse(task_id=task_id, status="queued", tenant_id=tenant_id, work_branch=work_branch)


@app.get("/v1/aicode/tasks/{task_id}")
def get_aicode_task(task_id: str, tenant_id: str = Depends(verify_api_key)):
    queue = getattr(app.state, "queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="Durable task queue is not configured")
    try:
        result = queue.get(task_id, tenant_id)
    except Exception:
        logger.exception("Could not read AICode task")
        raise HTTPException(status_code=503, detail="Task queue is unavailable") from None
    if result is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return result


@app.delete("/v1/aicode/tasks/{task_id}", status_code=202)
def cancel_aicode_task(task_id: str, tenant_id: str = Depends(verify_api_key)):
    queue = getattr(app.state, "queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="Durable task queue is not configured")
    try:
        cancelled = queue.cancel(task_id, tenant_id)
    except Exception:
        logger.exception("Could not cancel AICode task")
        raise HTTPException(status_code=503, detail="Task queue is unavailable") from None
    if not cancelled:
        raise HTTPException(status_code=404, detail="Task not found or no longer cancellable")
    return {"task_id": task_id, "status": "cancelled"}
