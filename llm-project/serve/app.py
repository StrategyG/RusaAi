from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol

import jsonschema
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from jsonschema import FormatChecker
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("rusa-ai-serve")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA_PATH = REPOSITORY_ROOT / "schemas" / "a2a" / "task-envelope.schema.json"
MAX_REQUEST_BYTES = int(os.getenv("RUSA_MAX_REQUEST_BYTES", "1048576"))
MAX_INPUT_TOKENS = int(os.getenv("RUSA_MAX_INPUT_TOKENS", "8192"))
if MAX_REQUEST_BYTES < 1 or MAX_INPUT_TOKENS < 1:
    raise RuntimeError("request byte and input token limits must be positive")
API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


class RequestBodyLimitMiddleware:
    """Buffer and cap all HTTP request bodies, including chunked requests."""

    def __init__(self, app, max_body_bytes: int):
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                response = JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Invalid Content-Length header"},
                    headers={
                        "X-Request-ID": request_id,
                        "X-Content-Type-Options": "nosniff",
                        "Cache-Control": "no-store",
                    },
                )
                await response(scope, receive, send)
                return
            if declared_size < 0 or declared_size > self.max_body_bytes:
                response = JSONResponse(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    content={"detail": "Request body exceeds the configured size limit"},
                    headers={
                        "X-Request-ID": request_id,
                        "X-Content-Type-Options": "nosniff",
                        "Cache-Control": "no-store",
                    },
                )
                await response(scope, receive, send)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > self.max_body_bytes:
                response = JSONResponse(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    content={"detail": "Request body exceeds the configured size limit"},
                    headers={
                        "X-Request-ID": request_id,
                        "X-Content-Type-Options": "nosniff",
                        "Cache-Control": "no-store",
                    },
                )
                await response(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        body_sent = False

        async def replay_receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)


class InferenceInputError(ValueError):
    """Raised when a prompt cannot fit the configured model context window."""


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
            if prompt_tokens > MAX_INPUT_TOKENS:
                raise InferenceInputError(
                    f"Prompt exceeds the configured {MAX_INPUT_TOKENS}-token input limit"
                )
            model_config = getattr(self.model, "config", None)
            context_limit = getattr(model_config, "max_position_embeddings", None)
            if (
                isinstance(context_limit, int)
                and context_limit > 0
                and prompt_tokens + max_new_tokens > context_limit
            ):
                raise InferenceInputError(
                    "Prompt plus requested completion exceeds the model context window"
                )

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


def authentication_is_configured() -> bool:
    return len(os.getenv("RUSA_API_KEY", "")) >= 32


def verify_api_key(api_key: str | None = Depends(api_key_header)) -> str:
    expected_key = os.getenv("RUSA_API_KEY", "")
    if len(expected_key) < 32:
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
    return api_key


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
app.add_middleware(
    RequestBodyLimitMiddleware,
    max_body_bytes=MAX_REQUEST_BYTES,
)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = uuid.uuid4().hex
    request.state.request_id = request_id

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            request_size = int(content_length)
        except ValueError:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"detail": "Invalid Content-Length header"},
                headers={
                    "X-Request-ID": request_id,
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-store",
                },
            )
        if request_size > MAX_REQUEST_BYTES:
            return JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={"detail": "Request body exceeds the configured size limit"},
                headers={
                    "X-Request-ID": request_id,
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-store",
                },
            )

    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


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
        headers={
            "X-Request-ID": request_id,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
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

    checks = {
        "model_loaded": model_loaded,
        "authentication_configured": authentication_is_configured(),
        "task_schema_loaded": schema_loaded,
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
    except InferenceInputError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
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
