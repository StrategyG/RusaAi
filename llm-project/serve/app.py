from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import jsonschema
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from jsonschema import FormatChecker
from pydantic import BaseModel, ConfigDict, Field

from model_runtime import (
    GenerationEvent,
    GenerationResult,
    InferenceBackend,
    ModelArtifactError,
    TransientInferenceError,
    load_inference_backend,
    verify_model_artifact,
)

logger = logging.getLogger("rusa-ai-serve")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA_PATH = REPOSITORY_ROOT / "schemas" / "a2a" / "task-envelope.schema.json"
MAX_REQUEST_BYTES = int(os.getenv("RUSA_MAX_REQUEST_BYTES", "1048576"))
BODY_READ_TIMEOUT_SECONDS = float(os.getenv("RUSA_BODY_READ_TIMEOUT_SECONDS", "15"))
if MAX_REQUEST_BYTES < 1 or BODY_READ_TIMEOUT_SECONDS <= 0:
    raise RuntimeError("Request body limits must be positive")

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


class RequestBodyTooLarge(Exception):
    pass


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


class InferenceAdmissionController:
    """Bound active inference and the number/time of requests waiting for a slot."""

    def __init__(self, max_active: int, max_waiting: int, queue_timeout: float):
        if max_active < 1 or max_waiting < 0 or queue_timeout <= 0:
            raise ValueError("Invalid inference admission limits")
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


@asynccontextmanager
async def inference_slot(controller: InferenceAdmissionController):
    await controller.acquire()
    try:
        yield
    finally:
        controller.release()


def _positive_int(name: str, default: int, minimum: int = 1) -> int:
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return value


def _positive_float(name: str, default: float, maximum: float | None = None) -> float:
    value = float(os.getenv(name, str(default)))
    if value <= 0 or (maximum is not None and value > maximum):
        raise RuntimeError(f"{name} is outside the allowed range")
    return value


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.backend = None
    application.state.artifact = None
    application.state.backend_error = "RUSA_MODEL_PATH is not configured"
    application.state.model_name = os.getenv("RUSA_MODEL_NAME", "rusa-ai-v1")
    application.state.admission = InferenceAdmissionController(
        max_active=_positive_int("RUSA_MAX_CONCURRENT_REQUESTS", 8),
        max_waiting=_positive_int("RUSA_MAX_QUEUED_REQUESTS", 32, minimum=0),
        queue_timeout=_positive_float("RUSA_QUEUE_TIMEOUT_SECONDS", 10.0),
    )

    model_path = os.getenv("RUSA_MODEL_PATH")
    if model_path:
        environment = os.getenv("ENVIRONMENT", "production").lower()
        expected_manifest_sha = os.getenv("RUSA_MODEL_MANIFEST_SHA256")
        try:
            artifact = await asyncio.to_thread(
                verify_model_artifact,
                model_path,
                expected_manifest_sha256=expected_manifest_sha,
                require_pinned_manifest=environment == "production",
            )
            application.state.artifact = artifact
            application.state.model_name = os.getenv("RUSA_MODEL_NAME", artifact.model_id)

            load_timeout = _positive_float("RUSA_MODEL_LOAD_TIMEOUT_SECONDS", 1800.0)
            attempts = _positive_int("RUSA_MODEL_LOAD_RETRIES", 2)
            backend_name = os.getenv("RUSA_INFERENCE_BACKEND", "transformers")
            last_error: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    backend = await asyncio.wait_for(
                        asyncio.to_thread(
                            load_inference_backend,
                            str(artifact.root),
                            backend_name=backend_name,
                            timeout_seconds=_positive_float(
                                "RUSA_GENERATION_TIMEOUT_SECONDS", 180.0
                            ),
                            max_model_len=_positive_int("RUSA_MAX_MODEL_LEN", 8192),
                            max_num_seqs=_positive_int("RUSA_MAX_NUM_SEQS", 16),
                            max_num_batched_tokens=_positive_int(
                                "RUSA_MAX_NUM_BATCHED_TOKENS", 8192
                            ),
                            tensor_parallel_size=_positive_int(
                                "RUSA_TENSOR_PARALLEL_SIZE", 1
                            ),
                            gpu_memory_utilization=_positive_float(
                                "RUSA_GPU_MEMORY_UTILIZATION", 0.9, maximum=1.0
                            ),
                        ),
                        timeout=load_timeout,
                    )
                    application.state.backend = backend
                    application.state.backend_error = None
                    logger.info(
                        "Inference backend loaded backend=%s device=%s model=%s manifest_sha256=%s",
                        backend.name,
                        backend.device,
                        artifact.model_id,
                        artifact.manifest_sha256,
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    logger.exception(
                        "Model backend load attempt %s/%s failed exception_type=%s",
                        attempt,
                        attempts,
                        type(exc).__name__,
                    )
                    if attempt < attempts:
                        await asyncio.sleep(min(2**attempt, 10))
            if application.state.backend is None:
                application.state.backend_error = (
                    f"Model backend initialization failed: {type(last_error).__name__}"
                    if last_error
                    else "Model backend initialization failed"
                )
        except (ModelArtifactError, OSError, ValueError) as exc:
            application.state.backend_error = f"Model artifact verification failed: {type(exc).__name__}"
            logger.error(
                "Model artifact verification failed exception_type=%s",
                type(exc).__name__,
            )
        except Exception as exc:
            application.state.backend_error = f"Model initialization failed: {type(exc).__name__}"
            logger.exception("Model initialization failed exception_type=%s", type(exc).__name__)
    yield
    backend = getattr(application.state, "backend", None)
    if backend is not None:
        try:
            await backend.close()
        except Exception:
            logger.exception("Inference backend shutdown failed")
    application.state.backend = None


environment = os.getenv("ENVIRONMENT", "production").lower()
app = FastAPI(
    title="RUSA-AI Serving API",
    description="Authenticated local-model inference and A2A task validation.",
    version="1.2.0",
    docs_url="/docs" if environment != "production" else None,
    redoc_url=None,
    openapi_url="/openapi.json" if environment != "production" else None,
    lifespan=lifespan,
)


class RequestContextAndBodyLimitMiddleware:
    """Count actual ASGI body bytes, including chunked requests, before parsing."""

    def __init__(self, application: Any):
        self.application = application

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.application(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-request-id", request_id.encode("ascii")),
                        (b"x-content-type-options", b"nosniff"),
                        (b"cache-control", b"no-store"),
                    ]
                )
                message["headers"] = headers
            await send(message)

        content_length = next(
            (value.decode("latin-1") for key, value in scope.get("headers", []) if key.lower() == b"content-length"),
            None,
        )
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                response = JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length header"},
                )
                await response(scope, receive, send_with_headers)
                return
            if declared_size < 0:
                response = JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length header"},
                )
                await response(scope, receive, send_with_headers)
                return
            if declared_size > MAX_REQUEST_BYTES:
                response = JSONResponse(
                    status_code=413,
                    content={"detail": "Request body exceeds the configured size limit"},
                )
                await response(scope, receive, send_with_headers)
                return

        body = bytearray()
        disconnected = False
        try:
            async with asyncio.timeout(BODY_READ_TIMEOUT_SECONDS):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        disconnected = True
                        break
                    if message["type"] != "http.request":
                        continue
                    body.extend(message.get("body", b""))
                    if len(body) > MAX_REQUEST_BYTES:
                        raise RequestBodyTooLarge
                    if not message.get("more_body", False):
                        break
        except RequestBodyTooLarge:
            response = JSONResponse(
                status_code=413,
                content={"detail": "Request body exceeds the configured size limit"},
            )
            await response(scope, receive, send_with_headers)
            return
        except TimeoutError:
            response = JSONResponse(
                status_code=408,
                content={"detail": "Timed out while receiving request body"},
            )
            await response(scope, receive, send_with_headers)
            return

        if disconnected:
            return

        replayed = False

        async def replay_receive() -> dict[str, Any]:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.application(scope, replay_receive, send_with_headers)


app.add_middleware(RequestContextAndBodyLimitMiddleware)


def _load_task_validator() -> jsonschema.protocols.Validator:
    schema_path = Path(
        os.getenv("TASK_ENVELOPE_SCHEMA_PATH", str(DEFAULT_SCHEMA_PATH))
    )
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator_class = jsonschema.validators.validator_for(schema)
        validator_class.check_schema(schema)
        return validator_class(schema, format_checker=FormatChecker())
    except (FileNotFoundError, json.JSONDecodeError, jsonschema.SchemaError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task validation is unavailable because its schema is missing or invalid",
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


def _require_backend(request: Request) -> InferenceBackend:
    backend: InferenceBackend | None = getattr(request.app.state, "backend", None)
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Inference model is not loaded",
        )
    return backend


def _check_model_name(request: ChatCompletionRequest, http_request: Request) -> str:
    model_name = http_request.app.state.model_name
    if request.model != model_name:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model '{request.model}' is not available",
        )
    return model_name


def _message_payload(messages: list[ChatMessage]) -> list[dict[str, str]]:
    return [{"role": message.role, "content": message.content} for message in messages]


async def _generate_with_retry(
    backend: InferenceBackend,
    messages: list[dict[str, str]],
    *,
    max_new_tokens: int,
    temperature: float,
    top_p: float,
    timeout_seconds: float,
) -> GenerationResult:
    max_retries = _positive_int("RUSA_INFERENCE_RETRIES", 1, minimum=0)
    for attempt in range(max_retries + 1):
        try:
            return await asyncio.wait_for(
                backend.generate(
                    messages,
                    max_new_tokens=max_new_tokens,
                    temperature=temperature,
                    top_p=top_p,
                ),
                timeout=timeout_seconds,
            )
        except TransientInferenceError:
            if attempt >= max_retries:
                raise
            await asyncio.sleep(min(0.25 * (2**attempt), 2.0))
    raise RuntimeError("Inference retry loop exited unexpectedly")


def _sse(data: dict[str, Any] | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
    return f"data: {payload}\n\n"


@app.get("/health/live", status_code=status.HTTP_200_OK)
@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    return {"status": "ok", "service": "rusa-ai-serve", "version": "1.2.0"}


@app.get("/health/ready")
def readiness_check(request: Request):
    backend = getattr(request.app.state, "backend", None)
    artifact = getattr(request.app.state, "artifact", None)
    try:
        _load_task_validator()
        schema_loaded = True
    except HTTPException:
        schema_loaded = False

    checks = {
        "model_loaded": backend is not None,
        "model_manifest_verified": artifact is not None,
        "authentication_configured": authentication_is_configured(),
        "task_schema_loaded": schema_loaded,
    }
    ready = all(checks.values())
    return JSONResponse(
        status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "ready" if ready else "not_ready",
            "checks": checks,
            "backend": getattr(backend, "name", None),
            "device": getattr(backend, "device", None),
            "continuous_batching": bool(getattr(backend, "supports_batching", False)),
            "model": request.app.state.model_name,
        },
    )


@app.get(
    "/v1/models",
    dependencies=[Depends(verify_api_key)],
)
def list_models(request: Request):
    _require_backend(request)
    return {
        "object": "list",
        "data": [
            {
                "id": request.app.state.model_name,
                "object": "model",
                "owned_by": "rusa-ai",
            }
        ],
    }


@app.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse,
    dependencies=[Depends(verify_api_key)],
)
async def chat_completions(request: ChatCompletionRequest, http_request: Request):
    backend = _require_backend(http_request)
    model_name = _check_model_name(request, http_request)
    messages = _message_payload(request.messages)
    controller: InferenceAdmissionController = http_request.app.state.admission
    timeout_seconds = _positive_float("RUSA_GENERATION_TIMEOUT_SECONDS", 180.0)

    if not request.stream:
        async with inference_slot(controller):
            try:
                result = await _generate_with_retry(
                    backend,
                    messages,
                    max_new_tokens=request.max_tokens,
                    temperature=request.temperature,
                    top_p=request.top_p,
                    timeout_seconds=timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise HTTPException(
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                    detail="Inference generation timed out",
                ) from exc
            except TransientInferenceError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Inference backend is temporarily unavailable",
                    headers={"Retry-After": "2"},
                ) from exc
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=str(exc)[:300],
                ) from exc
            except Exception as exc:
                request_id = getattr(http_request.state, "request_id", "unknown")
                logger.exception(
                    "Inference failed request_id=%s exception_type=%s",
                    request_id,
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Inference backend failed to generate a response",
                ) from exc

        return ChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex}",
            created=int(time.time()),
            model=model_name,
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

    await controller.acquire()
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    async def event_stream():
        final_event: GenerationEvent | None = None
        try:
            yield _sse(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                }
            )
            async with asyncio.timeout(timeout_seconds):
                async for event in backend.stream(
                    messages,
                    max_new_tokens=request.max_tokens,
                    temperature=request.temperature,
                    top_p=request.top_p,
                ):
                    if event.delta:
                        yield _sse(
                            {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model_name,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"content": event.delta},
                                        "finish_reason": None,
                                    }
                                ],
                            }
                        )
                    if event.finish_reason:
                        final_event = event

            if final_event is None:
                yield _sse(
                    {
                        "error": {
                            "message": "Inference stream ended without a completion",
                            "type": "server_error",
                        }
                    }
                )
            else:
                yield _sse(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_name,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": final_event.finish_reason,
                            }
                        ],
                    }
                )
                yield _sse(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_name,
                        "choices": [],
                        "usage": {
                            "prompt_tokens": final_event.prompt_tokens,
                            "completion_tokens": final_event.completion_tokens,
                            "total_tokens": final_event.prompt_tokens + final_event.completion_tokens,
                        },
                    }
                )
        except asyncio.TimeoutError:
            yield _sse(
                {
                    "error": {
                        "message": "Inference generation timed out",
                        "type": "timeout_error",
                    }
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            request_id = getattr(http_request.state, "request_id", "unknown")
            logger.exception(
                "Streaming inference failed request_id=%s exception_type=%s",
                request_id,
                type(exc).__name__,
            )
            yield _sse(
                {
                    "error": {
                        "message": "Inference stream failed",
                        "type": "server_error",
                    }
                }
            )
        yield _sse("[DONE]")
        finally:
            controller.release()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
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
    validator = _load_task_validator()
    validation_errors = sorted(
        validator.iter_errors(request.envelope),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    errors = []
    for error in validation_errors:
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        errors.append(f"{path}: {error.message}")
    return TaskValidationResponse(valid=not errors, errors=errors)
