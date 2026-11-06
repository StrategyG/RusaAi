from __future__ import annotations

import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from itertools import islice
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import APIKeyHeader
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, Field

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("rusa-ai-serve")

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)
TASK_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "a2a"
    / "task-envelope.schema.json"
)
MAX_VALIDATION_ERRORS = 10


def load_task_validator(
    schema_path: Path | None = None,
) -> Draft202012Validator:
    """Load and validate the repository's Draft 2020-12 task schema."""
    schema_path = schema_path or TASK_SCHEMA_PATH
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Unable to load required task schema: {schema_path}"
        ) from exc

    if not isinstance(schema, dict):
        raise RuntimeError("Task schema root must be a JSON object.")
    if schema.get("$schema") != Draft202012Validator.META_SCHEMA["$id"]:
        raise RuntimeError(
            "Task schema must declare JSON Schema Draft 2020-12."
        )

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise RuntimeError(
            f"Invalid task schema at {schema_path}: {exc.message}"
        ) from exc

    return Draft202012Validator(schema, format_checker=FormatChecker())


@asynccontextmanager
async def lifespan(application: FastAPI):
    environment = os.getenv("ENVIRONMENT", "development").strip().lower()
    api_key = os.getenv("RUSA_API_KEY", "")
    mock_enabled = os.getenv("RUSA_ENABLE_MOCK_INFERENCE", "").lower() in {
        "1",
        "true",
        "yes",
    }

    if environment == "production":
        if len(api_key) < 32:
            raise RuntimeError(
                "RUSA_API_KEY must contain at least 32 characters in production."
            )
        if mock_enabled:
            raise RuntimeError(
                "RUSA_ENABLE_MOCK_INFERENCE cannot be enabled in production."
            )

    application.state.task_validator = load_task_validator()
    logger.info("Application startup complete; task schema loaded.")
    yield


app = FastAPI(
    title="RUSA-AI API",
    description=(
        "API scaffold for chat completions and A2A task-envelope validation. "
        "A real inference backend must be configured before serving model traffic."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def verify_api_key(api_key: str | None = Depends(api_key_header)) -> None:
    expected_key = os.getenv("RUSA_API_KEY", "")
    if not expected_key:
        if os.getenv("ENVIRONMENT", "development").strip().lower() == "production":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="API authentication is not configured.",
            )
        return

    if not api_key or not hmac.compare_digest(api_key, expected_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )


class ChatMessage(BaseModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str = Field(min_length=1, max_length=128_000)


class ChatCompletionRequest(BaseModel):
    model: str = Field(default="rusa-ai-v1", min_length=1, max_length=128)
    messages: list[ChatMessage] = Field(min_length=1, max_length=100)
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


@app.get("/health", status_code=status.HTTP_200_OK)
def health_check() -> dict[str, str]:
    """Liveness endpoint; it does not claim that model inference is ready."""
    return {
        "status": "healthy",
        "service": "rusa-ai-api",
        "version": "1.0.0",
        "environment": os.getenv("ENVIRONMENT", "development"),
        "inference": "not_configured",
    }


@app.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse,
    dependencies=[Depends(verify_api_key)],
)
def chat_completions(request: ChatCompletionRequest) -> ChatCompletionResponse:
    if request.stream:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Streaming is not implemented by the configured runtime.",
        )

    environment = os.getenv("ENVIRONMENT", "development").strip().lower()
    mock_enabled = os.getenv("RUSA_ENABLE_MOCK_INFERENCE", "").lower() in {
        "1",
        "true",
        "yes",
    }

    if mock_enabled and environment != "production":
        last_message = request.messages[-1].content
        now = int(time.time())
        return ChatCompletionResponse(
            id=f"rusa-chatcmpl-{now}",
            created=now,
            model=request.model,
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatMessage(
                        role="assistant",
                        content=(
                            "[DEVELOPMENT MOCK ONLY] Received prompt: "
                            f"{last_message}"
                        ),
                    ),
                    finish_reason="stop",
                )
            ],
        )

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Inference backend is not configured.",
    )


def _json_path(error: Any) -> str:
    path = "$"
    for part in error.absolute_path:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{str(part)[:80]}"
    return path[:256]


def _format_validation_error(error: Any) -> str:
    """Return field-oriented feedback without echoing submitted field values."""
    path = _json_path(error)
    rule = error.validator

    if rule == "required" and isinstance(error.instance, dict):
        for field in error.validator_value:
            if (
                field not in error.instance
                and error.message == f"{field!r} is a required property"
            ):
                return f"{path}.{field}: is required"
        return f"{path}: is missing a required property"
    if rule == "type":
        return f"{path}: must have type {error.validator_value}"
    if rule == "format":
        return f"{path}: must use the {error.validator_value} format"
    if rule == "enum":
        allowed = ", ".join(str(value) for value in error.validator_value)
        return f"{path}: must be one of [{allowed}]"
    if rule == "const":
        return f"{path}: must equal the required value"
    if rule == "minLength":
        return f"{path}: must contain at least {error.validator_value} characters"
    if rule == "maxLength":
        return f"{path}: must contain at most {error.validator_value} characters"
    if rule == "minItems":
        return f"{path}: must contain at least {error.validator_value} items"
    if rule == "maxItems":
        return f"{path}: must contain at most {error.validator_value} items"
    if rule == "pattern":
        return f"{path}: does not match the required pattern"
    if rule == "additionalProperties":
        return f"{path}: contains unsupported properties"
    if rule in {"minimum", "exclusiveMinimum"}:
        return f"{path}: is below the permitted minimum"
    if rule in {"maximum", "exclusiveMaximum"}:
        return f"{path}: exceeds the permitted maximum"
    return f"{path}: does not satisfy the schema constraint ({rule})"


@app.post(
    "/v1/validate-task",
    response_model=TaskValidationResponse,
    dependencies=[Depends(verify_api_key)],
)
def validate_task_envelope(
    request: TaskValidationRequest,
) -> TaskValidationResponse:
    validator: Draft202012Validator = app.state.task_validator
    errors = list(
        islice(
            validator.iter_errors(request.envelope),
            MAX_VALIDATION_ERRORS + 1,
        )
    )
    has_more_errors = len(errors) > MAX_VALIDATION_ERRORS
    errors = sorted(
        errors[:MAX_VALIDATION_ERRORS],
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            error.validator or "",
            error.message,
        ),
    )

    details = [_format_validation_error(error) for error in errors]
    if has_more_errors:
        details.append(
            f"More than {MAX_VALIDATION_ERRORS} validation errors; "
            "correct the listed issues and resubmit."
        )

    return TaskValidationResponse(valid=not details, errors=details)
