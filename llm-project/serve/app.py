import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import jsonschema
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("rusa-ai-serve")

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "a2a"
    / "task-envelope.schema.json"
)


def load_task_validator(
    schema_path: Path = SCHEMA_PATH,
) -> jsonschema.Draft202012Validator:
    """Load and validate the protocol schema once; fail startup on bad config."""
    try:
        with schema_path.open("r", encoding="utf-8") as schema_file:
            schema = json.load(schema_file)
        jsonschema.Draft202012Validator.check_schema(schema)
        return jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        )
    except (OSError, json.JSONDecodeError, jsonschema.SchemaError) as exc:
        logger.exception("Unable to load the required A2A task schema")
        raise RuntimeError("Required A2A task schema is missing or invalid") from exc


TASK_VALIDATOR = load_task_validator()

app = FastAPI(
    title="RUSA-AI Serving Engine",
    description="FastAPI API for RUSA-AI inference and task validation.",
    version="1.0.0",
)

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


def verify_api_key(api_key: Optional[str] = Depends(api_key_header)):
    expected_key = os.getenv("RUSA_API_KEY")
    if expected_key and (not api_key or api_key != expected_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key",
        )
    return api_key


class ChatMessage(BaseModel):
    role: str = Field(..., description="Message role: system, user, or assistant")
    content: str = Field(..., description="Content of the message")


class ChatCompletionRequest(BaseModel):
    model: str = Field(default="rusa-ai-v1", description="Model identifier")
    messages: List[ChatMessage] = Field(..., description="List of chat messages")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=0.9, ge=0.0, le=1.0)
    max_tokens: int = Field(default=1024, ge=1, le=4096)
    stream: bool = Field(default=False)


class ChatChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatChoice]


class TaskValidationRequest(BaseModel):
    envelope: Dict[str, Any]


class TaskValidationResponse(BaseModel):
    valid: bool
    errors: Optional[List[str]] = None


@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    """Liveness only: HTTP 200 means the API process is alive."""
    return {
        "status": "healthy",
        "service": "rusa-ai-serve",
        "version": "1.0.0",
        "environment": os.getenv("ENVIRONMENT", "production"),
    }


@app.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse,
    dependencies=[Depends(verify_api_key)],
)
def chat_completions(request: ChatCompletionRequest):
    if not request.messages:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Messages list cannot be empty",
        )

    # Placeholder response until a real inference backend is configured.
    last_msg = request.messages[-1].content
    mock_response = (
        f"[RUSA-AI Model Output]: Processed prompt: '{last_msg}' successfully."
    )
    now = int(time.time())
    return ChatCompletionResponse(
        id=f"rusa-chatcmpl-{now}",
        created=now,
        model=request.model,
        choices=[
            ChatChoice(
                index=0,
                message=ChatMessage(role="assistant", content=mock_response),
                finish_reason="stop",
            )
        ],
    )


@app.post("/v1/validate-task", response_model=TaskValidationResponse)
def validate_task_envelope(request: TaskValidationRequest):
    errors = [
        error.message
        for error in TASK_VALIDATOR.iter_errors(request.envelope)
    ]
    return TaskValidationResponse(valid=not errors, errors=errors)
