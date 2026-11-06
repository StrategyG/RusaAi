from fastapi import FastAPI
from fastapi.testclient import TestClient

from rusa_observability.alert_sink import _sanitize_alert
from rusa_observability.metrics import (
    DB_SIZE_LIMIT,
    INFERENCE_REQUESTS,
    TOKEN_USAGE,
    RAG_EVAL_RECALL,
    RAG_EVAL_MRR,
    RAG_EVAL_NDCG,
    record_database_capacity_limit,
    record_inference,
    record_retrieval_evaluation,
)
from rusa_observability.middleware import install_metrics


def test_inference_metrics_record_latency_outcome_and_exact_tokens():
    model = "test-observability-model"
    before_requests = INFERENCE_REQUESTS.labels(model, "success")._value.get()
    before_prompt = TOKEN_USAGE.labels(model, "prompt")._value.get()
    before_completion = TOKEN_USAGE.labels(model, "completion")._value.get()

    record_inference(
        model,
        "success",
        0.25,
        prompt_tokens=17,
        completion_tokens=9,
    )

    assert INFERENCE_REQUESTS.labels(model, "success")._value.get() == before_requests + 1
    assert TOKEN_USAGE.labels(model, "prompt")._value.get() == before_prompt + 17
    assert TOKEN_USAGE.labels(model, "completion")._value.get() == before_completion + 9


def test_metrics_endpoint_requires_bearer_token_and_exposes_http_metrics(monkeypatch):
    token = "test-metrics-secret-with-at-least-32-characters"
    monkeypatch.setenv("RUSA_METRICS_BEARER_TOKEN", token)
    app = FastAPI()

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    install_metrics(app, "test-observability-api")
    client = TestClient(app)
    assert client.get("/health/live").status_code == 200
    assert client.get("/metrics").status_code == 401

    response = client.get(
        "/metrics",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert "rusa_http_requests_total" in response.text
    assert 'route="/health/live"' in response.text
    assert 'service="test-observability-api"' in response.text


def test_retrieval_evaluation_updates_quality_gauges():
    report = {
        "queries": 12,
        "k": 5,
        "recall_at_k": 0.91,
        "mrr_at_k": 0.82,
        "ndcg_at_k": 0.87,
    }
    record_retrieval_evaluation(report)

    assert RAG_EVAL_RECALL.labels("5")._value.get() == 0.91
    assert RAG_EVAL_MRR.labels("5")._value.get() == 0.82
    assert RAG_EVAL_NDCG.labels("5")._value.get() == 0.87


def test_database_capacity_threshold_is_exported():
    record_database_capacity_limit(10 * 1024**3)
    assert DB_SIZE_LIMIT.labels("rusa_agent")._value.get() == 10 * 1024**3


def test_alert_sink_drops_unapproved_fields():
    sanitized = _sanitize_alert(
        {
            "status": "firing",
            "fingerprint": "abc123",
            "labels": {
                "alertname": "InferenceP95LatencyHigh",
                "severity": "warning",
                "service": "rusa-inference",
                "tenant_id": "must-not-be-forwarded",
                "secret": "must-not-be-forwarded",
            },
            "annotations": {
                "summary": "Inference latency is high",
                "description": "p95 exceeded the threshold",
                "prompt": "must-not-be-forwarded",
            },
        }
    )

    assert sanitized["alertname"] == "InferenceP95LatencyHigh"
    assert sanitized["service"] == "rusa-inference"
    assert "tenant_id" not in sanitized
    assert "secret" not in sanitized
    assert "prompt" not in sanitized
