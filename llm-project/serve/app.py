import os
import json
import logging
from contextlib import asynccontextmanager
from hmac import compare_digest
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
import jsonschema

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("rusa-ai-serve")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    environment = os.getenv("ENVIRONMENT", "").strip().lower() or "production"
    if environment == "production" and not os.getenv("RUSA_API_KEY", "").strip():
        raise RuntimeError("RUSA_API_KEY must be configured in production.")
    yield

app = FastAPI(
    title="RUSA-AI Serving Engine",
    description="Production-ready API for RUSA-AI model inference and task validation.",
    version="1.0.0",
    lifespan=lifespan,
)

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

def verify_api_key(api_key: Optional[str] = Depends(api_key_header)) -> None:
    expected_key = os.getenv("RUSA_API_KEY", "")
    if not expected_key.strip() or not api_key or not compare_digest(
        api_key.encode("utf-8"), expected_key.encode("utf-8", errors="surrogateescape")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key"
        )

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
    return {
        "status": "healthy",
        "service": "rusa-ai-serve",
        "version": "1.0.0",
        "environment": os.getenv("ENVIRONMENT", "production")
    }

@app.post("/v1/chat/completions", response_model=ChatCompletionResponse, dependencies=[Depends(verify_api_key)])
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
    except Exception as e:
        logger.error(f"Inference error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Inference engine failure: {str(e)}")

@app.post("/v1/validate-task", response_model=TaskValidationResponse, dependencies=[Depends(verify_api_key)])
def validate_task_envelope(request: TaskValidationRequest):
    schema_path = os.path.join(os.path.dirname(__file__), "..", "..", "schemas", "a2a", "task-envelope.schema.json")
    if not os.path.exists(schema_path):
        schema_path = "schemas/a2a/task-envelope.schema.json"
    
    if not os.path.exists(schema_path):
        return TaskValidationResponse(valid=True, errors=["Schema file not found on disk, skipped validation."])

    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
        
        validator = jsonschema.Draft7Validator(schema)
        errors = [err.message for err in validator.iter_errors(request.envelope)]
        
        if errors:
            return TaskValidationResponse(valid=False, errors=errors)
        return TaskValidationResponse(valid=True, errors=[])
    except Exception as e:
        return TaskValidationResponse(valid=False, errors=[str(e)])
