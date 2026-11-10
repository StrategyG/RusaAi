import json
from pathlib import Path

import jsonschema
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1] / "contracts" / "model_plane" / "v0.1.0"


def load_schema(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def validator(name: str) -> jsonschema.Draft202012Validator:
    schema = load_schema(name)
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def valid_request() -> dict:
    return {
        "contract_version": "0.1.0",
        "request_id": "req_01",
        "run_id": "run_01",
        "task_id": "task_01",
        "model_profile": "private-code-reasoning",
        "capability": "chat",
        "input": {
            "messages": [{"role": "user", "content": "Review this change."}],
            "temperature": 0,
            "max_output_tokens": 1024,
        },
        "constraints": {
            "data_classification": "confidential",
            "region": "approved-region",
            "max_latency_ms": 30000,
            "budget": {
                "max_input_tokens": 12000,
                "max_output_tokens": 1024,
                "currency": "USD",
                "max_cost_microunits": 500000,
            },
            "fallback": "deny",
        },
        "deadline": "2026-11-11T00:30:00Z",
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    }


def valid_response() -> dict:
    return {
        "contract_version": "0.1.0",
        "request_id": "req_01",
        "status": "completed",
        "model": {
            "profile": "private-code-reasoning",
            "model_id": "rusa-general-4b",
            "model_version": "sha256:abc123",
            "deployment_id": "deployment-01",
            "deployment_zone": "private",
            "region": "approved-region",
        },
        "output": {"content": "Review result.", "tool_call_proposals": []},
        "usage": {
            "input_tokens": 120,
            "output_tokens": 80,
            "total_tokens": 200,
            "cost_microunits": 1000,
            "currency": "USD",
        },
        "finish_reason": "stop",
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
    }


def test_model_plane_json_schemas_are_valid() -> None:
    for name in (
        "inference-request.schema.json",
        "inference-response.schema.json",
        "inference-error.schema.json",
    ):
        jsonschema.Draft202012Validator.check_schema(load_schema(name))


def test_openapi_document_references_existing_contract_schemas() -> None:
    document = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    assert document["openapi"] == "3.1.0"
    operation = document["paths"]["/model-plane/v1/inference"]["post"]
    request_ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert (ROOT / request_ref).is_file()
    for response in operation["responses"].values():
        content = response.get("content", {}).get("application/json", {})
        schema_ref = content.get("schema", {}).get("$ref")
        if schema_ref:
            assert (ROOT / schema_ref).is_file()


def test_valid_chat_request_matches_contract() -> None:
    validator("inference-request.schema.json").validate(valid_request())


def test_request_rejects_untrusted_tenant_override() -> None:
    payload = valid_request()
    payload["tenant_id"] = "attacker-selected-tenant"
    with pytest.raises(jsonschema.ValidationError):
        validator("inference-request.schema.json").validate(payload)


def test_request_rejects_unsupported_capability() -> None:
    payload = valid_request()
    payload["capability"] = "tool_execution"
    with pytest.raises(ValidationError):
        validator("inference-request.schema.json").validate(payload)


def test_valid_inference_response_matches_contract() -> None:
    validator("inference-response.schema.json").validate(valid_response())


def test_error_response_matches_contract() -> None:
    payload = {
        "contract_version": "0.1.0",
        "request_id": "req_01",
        "status": "failed",
        "error": {
            "code": "PROFILE_UNAVAILABLE",
            "message": "The approved model profile is unavailable.",
            "retryable": False,
            "details": {},
        },
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
    }
    validator("inference-error.schema.json").validate(payload)
