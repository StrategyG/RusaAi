import os
import json
import logging
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Security, Depends, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
import jsonschema

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("rusa-ai-serve")

app = FastAPI(
    title="RUSA-AI Serving Engine",
    description="Production-ready API for RUSA-AI model inference and task validation.",
    version="1.0.0"
)

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


def verify_api_key(api_key: Optional[str] = Depends(api_key_header)):
    expected_key = os.getenv("RUSA_API_KEY")
    if expected_key:
        if not api_key or api_key != expected_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API Key"
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
    """Liveness endpoint: HTTP 200 means the API process is alive."""
    return {
        "status": "healthy",
        "service": "rusa-ai-serve",
        "version": "1.0.0",
        "environment": os.getenv("ENVIRONMENT", "production")
    }


@app.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse,
    dependencies=[Depends(verify_api_key)]
)
def chat_completions(request: ChatCompletionRequest):
    try:
        if not request.messages:
            raise HTTPException(status_code=400, detail="Messages list cannot be empty")

        last_msg = request.messages[-1].content
        mock_response = f"[RUSA-AI Model Output]: Processed prompt: '{last_msg}' successfully."

        import time
        return ChatCompletionResponse(
            id=f"rusa-chatcmpl-{int(time.time())}",
            created=int(time.time()),
            model=request.model,
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content=mock_response),
                    finish_reason="stop"
                )
            ]
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Inference error")
        raise HTTPException(
            status_code=500, detail="Inference engine failure"
        ) from exc


@app.post("/v1/validate-task", response_model=TaskValidationResponse)
def validate_task_envelope(request: TaskValidationRequest):
    schema_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "schemas",
        "a2a",
        "task-envelope.schema.json",
    )
    if not os.path.exists(schema_path):
        schema_path = "schemas/a2a/task-envelope.schema.json"

    if not os.path.exists(schema_path):
        return TaskValidationResponse(
            valid=False, errors=["Task envelope schema is unavailable."]
        )

    try:
        with open(schema_path, "r", encoding="utf-8") as schema_file:
            schema = json.load(schema_file)

        validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        )
        errors = [
            error.message
            for error in validator.iter_errors(request.envelope)
        ]
        return TaskValidationResponse(valid=not errors, errors=errors)
    except (OSError, json.JSONDecodeError, jsonschema.SchemaError) as exc:
        logger.exception("Task schema validation failed")
        raise HTTPException(
            status_code=500, detail="Task schema validation is unavailable."
        ) from exc
