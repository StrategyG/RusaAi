from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol

import jsonschema
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from jsonschema import FormatChecker
from pydantic import BaseModel, ConfigDict, Field

from rusa_agents.queue import PostgresTaskQueue

logger = logging.getLogger("rusa-ai-serve")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA_PATH = REPOSITORY_ROOT / "schemas" / "a2a" / "task-envelope.schema.json"
MAX_REQUEST_BYTES = int(os.getenv("RUSA_MAX_REQUEST_BYTES", "1048576"))
if not 1024 <= MAX_REQUEST_BYTES <= 16 * 1024 * 1024:
    raise ValueError("RUSA_MAX_REQUEST_BYTES must be between 1024 and 16777216")
API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "developer", "user", "assistant"]
    content: str = Field(min_length=1, max_length=32768)


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(default="rusa-ai-v1", min_length=1, max_length=128)
    messages: list[ChatMessage] = Field(min_length=1, max_length=128)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=0.9, gt=0.0, le=1.0)
    max_tokens: int = Field(default=512, ge=1, le=4096)
    stream: bool = False


class ChatChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: Literal["stop", "length"]


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatChoice]
    usage: Usage


class TaskValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    envelope: dict[str, Any]


class TaskValidationResponse(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)


class AgentTaskSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    envelope: dict[str, Any]


@dataclass(frozen=True)
class GenerationResult:
    content: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: Literal["stop", "length"]


class InferenceBackend(Protocol):
    def generate(
        self,
        messages: list[ChatMessage],
        *,
        max_new_tokens: int,
        temperature: float,
        top_p: float,
    ) -> GenerationResult:
        """Generate one completion from the supplied chat messages."""


class TransformersBackend:
    """Local-only Hugging Face Transformers adapter.

    Model files must already exist on disk. Remote code and implicit downloads
    are deliberately disabled.
    """

    def __init__(self, model: Any, tokenizer: Any, torch: Any, device: Any):
        self.model = model
        self.tokenizer = tokenizer
        self.torch = torch
        self.device = device
        self._generation_lock = threading.Lock()

    @classmethod
    def from_pretrained(cls, model_path: str) -> "TransformersBackend":
        path = Path(model_path).expanduser().resolve()
        if not path.is_dir():
            raise RuntimeError("RUSA_MODEL_PATH must point to an existing model directory")

        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(
            str(path),
            local_files_only=True,
            trust_remote_code=False,
        )
        if not getattr(tokenizer, "chat_template", None):
            raise RuntimeError("The configured tokenizer does not contain a chat template")

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        dtype = torch.float16 if device.type == "cuda" else torch.float32
        model = AutoModelForCausalLM.from_pretrained(
            str(path),
            local_files_only=True,
            trust_remote_code=False,
            use_safetensors=True,
            torch_dtype=dtype,
        )
        model.to(device)
        model.eval()

        if tokenizer.pad_token_id is None:
            if tokenizer.eos_token_id is None:
                raise RuntimeError("Tokenizer must define either pad_token_id or eos_token_id")
            tokenizer.pad_token = tokenizer.eos_token

        return cls(model=model, tokenizer=tokenizer, torch=torch, device=device)

    def generate(
        self,
        messages: list[ChatMessage],
        *,
        max_new_tokens: int,
        temperature: float,
        top_p: float,
    ) -> GenerationResult:
        conversation = [
            {"role": message.role, "content": message.content}
            for message in messages
        ]

        with self._generation_lock:
            encoded = self.tokenizer.apply_chat_template(
                conversation,
                tokenize=True,
                add_generation_prompt=True,
                return_tensors="pt",
                return_dict=True,
            )
            encoded = {
                key: value.to(self.device)
                for key, value in encoded.items()
            }
            prompt_tokens = int(encoded["input_ids"].shape[-1])

            generation_args: dict[str, Any] = {
                "max_new_tokens": max_new_tokens,
                "pad_token_id": self.tokenizer.pad_token_id,
            }
            if temperature == 0:
                generation_args["do_sample"] = False
            else:
                generation_args.update(
                    {
                        "do_sample": True,
                        "temperature": temperature,
                        "top_p": top_p,
                    }
                )

            with self.torch.inference_mode():
                output = self.model.generate(**encoded, **generation_args)

            generated_tokens = output[0, prompt_tokens:]
            completion_tokens = int(generated_tokens.shape[-1])
            content = self.tokenizer.decode(
                generated_tokens,
                skip_special_tokens=True,
            ).strip()
            if not content:
                raise RuntimeError("The model returned an empty completion")

            eos_token_id = self.tokenizer.eos_token_id
            if isinstance(eos_token_id, list):
                reached_eos = (
                    completion_tokens > 0
                    and int(generated_tokens[-1]) in eos_token_id
                )
            else:
                reached_eos = (
                    eos_token_id is not None
                    and completion_tokens > 0
                    and int(generated_tokens[-1]) == eos_token_id
                )
            finish_reason: Literal["stop", "length"] = (
                "stop"
                if reached_eos or completion_tokens < max_new_tokens
                else "length"
            )

            return GenerationResult(
                content=content,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                finish_reason=finish_reason,
            )


@lru_cache(maxsize=4)
def _load_task_validator(schema_path: str) -> jsonschema.protocols.Validator:
    path = Path(schema_path)
    with path.open("r", encoding="utf-8") as schema_file:
        schema = json.load(schema_file)

    validator_class = jsonschema.validators.validator_for(schema)
    validator_class.check_schema(schema)
    return validator_class(schema, format_checker=FormatChecker())


def get_task_validator() -> jsonschema.protocols.Validator:
    schema_path = os.getenv("TASK_ENVELOPE_SCHEMA_PATH", str(DEFAULT_SCHEMA_PATH))
    try:
        return _load_task_validator(schema_path)
    except FileNotFoundError as exc:
        logger.error("A2A schema file is missing")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task validation is unavailable because its schema is not installed",
        ) from exc
    except (json.JSONDecodeError, jsonschema.SchemaError, OSError) as exc:
        logger.exception("A2A schema could not be loaded")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task validation is unavailable because its schema is invalid",
        ) from exc


def _load_api_key_tenants() -> dict[str, str] | None:
    raw = os.getenv("RUSA_API_KEY_TENANTS_JSON")
    if raw is None:
        return None
    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("RUSA_API_KEY_TENANTS_JSON must contain valid JSON") from exc
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("RUSA_API_KEY_TENANTS_JSON must be a non-empty object")
    validated: dict[str, str] = {}
    for key_hash, tenant_id in mapping.items():
        if not isinstance(key_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", key_hash):
            raise ValueError("tenant API-key map keys must be SHA-256 hex digests")
        if not isinstance(tenant_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", tenant_id):
            raise ValueError("tenant IDs must contain 1 to 128 safe characters")
        validated[key_hash.lower()] = tenant_id
    return validated


def authentication_is_configured() -> bool:
    try:
        mapping = _load_api_key_tenants()
    except ValueError:
        return False
    if mapping is not None:
        return bool(mapping)
    tenant_id = os.getenv("RUSA_TENANT_ID", "default")
    return len(os.getenv("RUSA_API_KEY", "")) >= 32 and bool(
        re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", tenant_id)
    )


def verify_api_key(api_key: str | None = Depends(api_key_header)) -> str:
    try:
        tenant_mapping = _load_api_key_tenants()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service tenant authentication is not configured correctly",
        ) from exc

    if tenant_mapping is not None:
        if not tenant_mapping:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service tenant authentication is not configured correctly",
            )
        if api_key:
            supplied_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
            for expected_hash, tenant_id in tenant_mapping.items():
                if hmac.compare_digest(supplied_hash, expected_hash):
                    return tenant_id
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    expected_key = os.getenv("RUSA_API_KEY", "")
    tenant_id = os.getenv("RUSA_TENANT_ID", "default")
    if len(expected_key) < 32 or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", tenant_id):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service authentication is not configured correctly",
        )
    if not api_key or not hmac.compare_digest(
        api_key.encode("utf-8"),
        expected_key.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return tenant_id


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.backend = None
    application.state.backend_error = "RUSA_MODEL_PATH is not configured"
    model_path = os.getenv("RUSA_MODEL_PATH")

    if model_path:
        try:
            application.state.backend = TransformersBackend.from_pretrained(model_path)
            application.state.backend_error = None
            logger.info("Local inference model loaded successfully")
        except Exception:
            application.state.backend_error = "Model initialization failed"
            logger.exception("Local inference model initialization failed")

    application.state.task_queue = None
    application.state.task_queue_error = None
    agent_enabled = os.getenv("RUSA_AGENT_ENABLED", "false").lower() == "true"
    if agent_enabled:
        database_url = os.getenv("DATABASE_URL", "")
        tenant_id = os.getenv("RUSA_TENANT_ID", "default")
        if not database_url:
            application.state.task_queue_error = "DATABASE_URL is not configured"
        elif not tenant_id or len(tenant_id) > 128:
            application.state.task_queue_error = "RUSA_TENANT_ID is invalid"
        else:
            try:
                application.state.task_queue = PostgresTaskQueue(database_url)
            except Exception as exc:
                application.state.task_queue_error = type(exc).__name__
                logger.exception("Agent queue initialization failed")

    yield


environment = os.getenv("ENVIRONMENT", "production").lower()
app = FastAPI(
    title="RUSA-AI Serving API",
    description="Authenticated local-model inference and A2A task validation.",
    version="1.1.0",
    docs_url="/docs" if environment != "production" else None,
    redoc_url=None,
    openapi_url="/openapi.json" if environment != "production" else None,
    lifespan=lifespan,
)


class _RequestTooLarge(Exception):
    pass


class RequestSizeLimitMiddleware:
    """Enforce body limits for both Content-Length and streamed/chunked requests."""

    def __init__(self, application, max_bytes: int) -> None:
        self.application = application
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.application(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        request_headers = dict(scope.get("headers", []))
        content_length = request_headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = -1
            if declared_size < 0:
                response = JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length header"},
                    headers={"X-Request-ID": request_id},
                )
                await response(scope, receive, send)
                return
            if declared_size > self.max_bytes:
                response = JSONResponse(
                    status_code=413,
                    content={"detail": "Request body exceeds the configured size limit"},
                    headers={"X-Request-ID": request_id},
                )
                await response(scope, receive, send)
                return

        received_bytes = 0

        async def limited_receive():
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_bytes:
                    raise _RequestTooLarge
            return message

        async def secured_send(message):
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in {
                        b"x-request-id",
                        b"x-content-type-options",
                        b"cache-control",
                    }
                ]
                headers.extend(
                    [
                        (b"x-request-id", request_id.encode("ascii")),
                        (b"x-content-type-options", b"nosniff"),
                        (b"cache-control", b"no-store"),
                    ]
                )
                message["headers"] = headers
            await send(message)

        try:
            await self.application(scope, limited_receive, secured_send)
        except _RequestTooLarge:
            response = JSONResponse(
                status_code=413,
                content={"detail": "Request body exceeds the configured size limit"},
                headers={
                    "X-Request-ID": request_id,
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-store",
                },
            )
            await response(scope, receive, send)


app.add_middleware(RequestSizeLimitMiddleware, max_bytes=MAX_REQUEST_BYTES)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    logger.exception(
        "Unhandled request failure request_id=%s exception_type=%s",
        request_id,
        type(exc).__name__,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "type": "internal_error",
                "message": "Internal server error",
                "request_id": request_id,
            }
        },
        headers={"X-Request-ID": request_id},
    )


@app.get("/health/live", status_code=status.HTTP_200_OK)
@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    return {"status": "ok", "service": "rusa-ai-serve", "version": "1.1.0"}


@app.get("/health/ready")
def readiness_check(request: Request):
    model_loaded = getattr(request.app.state, "backend", None) is not None
    schema_loaded = True
    try:
        get_task_validator()
    except HTTPException:
        schema_loaded = False

    agent_enabled = os.getenv("RUSA_AGENT_ENABLED", "false").lower() == "true"
    agent_queue_loaded = (
        not agent_enabled
        or getattr(request.app.state, "task_queue", None) is not None
    )
    checks = {
        "model_loaded": model_loaded,
        "authentication_configured": authentication_is_configured(),
        "task_schema_loaded": schema_loaded,
        "agent_queue_loaded": agent_queue_loaded,
    }
    ready = all(checks.values())
    return JSONResponse(
        status_code=(
            status.HTTP_200_OK
            if ready
            else status.HTTP_503_SERVICE_UNAVAILABLE
        ),
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )


@app.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse,
    dependencies=[Depends(verify_api_key)],
)
def chat_completions(request: ChatCompletionRequest, http_request: Request):
    if request.stream:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Streaming responses are not implemented yet",
        )

    configured_model = os.getenv("RUSA_MODEL_NAME", "rusa-ai-v1")
    if request.model != configured_model:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model '{request.model}' is not available",
        )

    backend: InferenceBackend | None = getattr(
        http_request.app.state,
        "backend",
        None,
    )
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Inference model is not loaded",
        )

    try:
        result = backend.generate(
            request.messages,
            max_new_tokens=request.max_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
        )
    except Exception as exc:
        request_id = getattr(http_request.state, "request_id", "unknown")
        logger.exception("Inference failed request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Inference backend failed to generate a response",
        ) from exc

    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex}",
        created=int(time.time()),
        model=configured_model,
        choices=[
            ChatChoice(
                index=0,
                message=ChatMessage(role="assistant", content=result.content),
                finish_reason=result.finish_reason,
            )
        ],
        usage=Usage(
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.prompt_tokens + result.completion_tokens,
        ),
    )


@app.post("/v1/agent/tasks", status_code=status.HTTP_202_ACCEPTED)
def submit_agent_task(
    request: AgentTaskSubmission,
    http_request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    tenant_id: str = Depends(verify_api_key),
):
    """Validate and enqueue one idempotent coding-agent task."""
    if os.getenv("RUSA_AGENT_ENABLED", "false").lower() != "true":
        raise HTTPException(status_code=503, detail="AICode task processing is disabled")
    queue = getattr(http_request.app.state, "task_queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="Agent task queue is not configured")
    envelope = request.envelope
    if envelope.get("type") != "task.create":
        raise HTTPException(status_code=422, detail="Only task.create envelopes may be submitted")
    errors = list(get_task_validator().iter_errors(envelope))
    if errors:
        details = [str(error.message) for error in errors]
        raise HTTPException(status_code=422, detail=details)
    task = envelope["task"]
    payload = {
        "task_id": envelope["task_id"],
        "objective": task["objective"],
        "context": task.get("context", ""),
        "acceptance_criteria": task["acceptance_criteria"],
        "repository": {
            "full_name": task["repository"],
            "base_branch": task["base_branch"],
            "work_branch": task["work_branch"],
        },
        "tenant_id": tenant_id,
    }
    try:
        result = queue.enqueue(tenant_id, envelope["task_id"], payload, idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Agent task enqueue failed request_id=%s", http_request.state.request_id)
        raise HTTPException(status_code=503, detail="Agent task queue is temporarily unavailable") from exc
    return {"task_id": result["task_id"], "state": result["state"]}


@app.get("/v1/agent/tasks/{task_id}")
def get_agent_task(
    task_id: uuid.UUID,
    http_request: Request,
    tenant_id: str = Depends(verify_api_key),
):
    if os.getenv("RUSA_AGENT_ENABLED", "false").lower() != "true":
        raise HTTPException(status_code=503, detail="AICode task processing is disabled")
    queue = getattr(http_request.app.state, "task_queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="Agent task queue is not configured")
    record = queue.get(tenant_id, str(task_id))
    if record is None:
        raise HTTPException(status_code=404, detail="Agent task was not found")
    return {
        "task_id": record["task_id"],
        "state": record["state"],
        "attempts": record["attempts"],
        "max_attempts": record["max_attempts"],
        "result": record["result"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "last_error": record["last_error"],
    }


@app.get("/v1/agent/queue/stats")
def get_agent_queue_stats(
    http_request: Request,
    tenant_id: str = Depends(verify_api_key),
):
    if os.getenv("RUSA_AGENT_ENABLED", "false").lower() != "true":
        raise HTTPException(status_code=503, detail="AICode task processing is disabled")
    queue = getattr(http_request.app.state, "task_queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="Agent task queue is not configured")
    return queue.stats(tenant_id)


@app.get("/v1/agent/dead-letters")
def list_agent_dead_letters(
    http_request: Request,
    tenant_id: str = Depends(verify_api_key),
):
    if os.getenv("RUSA_AGENT_ENABLED", "false").lower() != "true":
        raise HTTPException(status_code=503, detail="AICode task processing is disabled")
    queue = getattr(http_request.app.state, "task_queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="Agent task queue is not configured")
    return {"items": queue.list_dead_letters(tenant_id, limit=50)}


@app.post("/v1/agent/tasks/{task_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_agent_task(
    task_id: uuid.UUID,
    http_request: Request,
    tenant_id: str = Depends(verify_api_key),
):
    if os.getenv("RUSA_AGENT_ENABLED", "false").lower() != "true":
        raise HTTPException(status_code=503, detail="AICode task processing is disabled")
    queue = getattr(http_request.app.state, "task_queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="Agent task queue is not configured")
    if not queue.retry_dead_letter(tenant_id, str(task_id)):
        record = queue.get(tenant_id, str(task_id))
        if record is None:
            raise HTTPException(status_code=404, detail="Agent task was not found")
        raise HTTPException(
            status_code=409,
            detail=f"Only dead-letter tasks can be replayed; current state is {record['state']}",
        )
    return {"task_id": str(task_id), "state": "queued", "attempts": 0}


@app.post("/v1/agent/tasks/{task_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_agent_task(
    task_id: uuid.UUID,
    http_request: Request,
    tenant_id: str = Depends(verify_api_key),
):
    if os.getenv("RUSA_AGENT_ENABLED", "false").lower() != "true":
        raise HTTPException(status_code=503, detail="AICode task processing is disabled")
    queue = getattr(http_request.app.state, "task_queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="Agent task queue is not configured")
    cancelled = queue.cancel(tenant_id, str(task_id))
    if not cancelled:
        record = queue.get(tenant_id, str(task_id))
        if record is None:
            raise HTTPException(status_code=404, detail="Agent task was not found")
        raise HTTPException(
            status_code=409,
            detail=f"Task cannot be cancelled from state {record['state']}",
        )
    return {"task_id": str(task_id), "state": "cancelled"}


@app.post(
    "/v1/validate-task",
    response_model=TaskValidationResponse,
    dependencies=[Depends(verify_api_key)],
)
def validate_task_envelope(request: TaskValidationRequest):
    validator = get_task_validator()
    validation_errors = sorted(
        validator.iter_errors(request.envelope),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    errors = []
    for error in validation_errors:
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        errors.append(f"{path}: {error.message}")

    return TaskValidationResponse(valid=not errors, errors=errors)
