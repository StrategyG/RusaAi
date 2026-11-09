from __future__ import annotations

from datetime import datetime, timezone

import pytest

from rusa_devops.a2a import agent_card, build_task_envelope
from rusa_devops.anomaly import BayesianAnomalyDetector
from rusa_devops.config import DevOpsConfig
from rusa_devops.contracts import TelemetrySample
from rusa_devops.decision import asymmetric_squared_loss, recommend_capacity, target_quantile
from rusa_devops.evaluation import classification_metrics, forecast_metrics
from rusa_devops.learning import DevOpsDatasetError, validate_record
from rusa_devops.telemetry import rolling_windows, validate_metric_identity


def test_config_defaults_to_disabled_and_controlled(monkeypatch):
    monkeypatch.delenv("RUSA_DEVOPS_ENABLED", raising=False)
    monkeypatch.delenv("RUSA_DEVOPS_AUTONOMY_MODE", raising=False)
    config = DevOpsConfig.from_env()
    assert config.enabled is False
    assert config.autonomy_mode == "controlled"


def test_config_rejects_unbounded_autonomy(monkeypatch):
    monkeypatch.setenv("RUSA_DEVOPS_AUTONOMY_MODE", "unrestricted")
    with pytest.raises(ValueError):
        DevOpsConfig.from_env()


def test_asymmetric_loss_penalizes_underprediction_more():
    under = asymmetric_squared_loss([100], [90], 100, 1)
    over = asymmetric_squared_loss([90], [100], 100, 1)
    assert under > over
    assert target_quantile(100, 1) == pytest.approx(100 / 101)


def test_capacity_recommendation_never_enables_execution():
    result = recommend_capacity([85, 92, 90], 80)
    assert result.action == "SCALE_OUT_RECOMMENDATION"
    assert result.approval_required is True
    assert result.execution_enabled is False


def test_anomaly_detector_flags_extreme_observation_without_underflow():
    detector = BayesianAnomalyDetector(prior=0.01, posterior_threshold=0.9)
    detector.fit([10 + (index % 3) * 0.1 for index in range(100)])
    results = detector.assess([10.1, 50.0, 1_000_000.0])
    assert results[0].is_anomaly is False
    assert results[1].is_anomaly is True
    assert results[2].posterior_probability > 0.99


def test_metrics_are_defined_for_imbalanced_classes():
    result = classification_metrics([False, False, True, True], [False, True, True, True])
    assert result["precision"] == pytest.approx(2 / 3)
    assert result["recall"] == 1.0
    forecast = forecast_metrics([10, 20], [9, 22])
    assert forecast["mae"] == pytest.approx(1.5)
    assert forecast["samples"] == 2


def test_telemetry_metric_validation_and_rolling_windows():
    validate_metric_identity("container_cpu_usage_seconds_total", "docker:container-a")
    with pytest.raises(ValueError):
        validate_metric_identity("cpu;drop_table", "docker-a")
    samples = [
        TelemetrySample("tenant-a", "cpu", "host-a", datetime(2025, 1, 1, tzinfo=timezone.utc), float(i))
        for i in range(10)
    ]
    windows = list(rolling_windows(samples, window_size=4, overlap=2))
    assert [len(window) for window in windows] == [4, 4, 4, 4]


def test_a2a_envelope_is_explicitly_controlled():
    envelope = build_task_envelope(
        task_id="5b3b2a8a-bc14-4e1d-9b0e-3f6e1a8f1b11",
        tenant_id="tenant-a", sender_agent="orchestrator",
        recipient_agent="devops-forecast", capability="devops.forecast",
        objective="Forecast CPU utilization", payload={"metric_name": "cpu"},
    )
    assert envelope["policy"]["human_approval_required"] is True
    assert envelope["policy"]["prohibited_actions"] == [
        "direct_infrastructure_write", "secret_read", "tenant_override"
    ]
    assert agent_card()["infrastructure_mutations"] is False


def test_devops_sft_contract_rejects_direct_mutation():
    record = {
        "id": "devops-1", "agent_role": "devops",
        "messages": [
            {"role": "system", "content": "Return a constrained DevOps action object."},
            {"role": "user", "content": "Scale the service."},
        ],
        "response": {
            "message": "I will prepare a capacity proposal.",
            "actions": [{"name": "apply_action", "arguments": {"target": 5}}],
            "done": True,
        },
    }
    with pytest.raises(DevOpsDatasetError):
        validate_record(record)
