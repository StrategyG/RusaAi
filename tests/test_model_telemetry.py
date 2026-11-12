import asyncio
import time
from pathlib import Path

import httpx
import pytest

from rusa_agents.model_telemetry import ModelTelemetryError, fetch_model_telemetry
from rusa_agents.model_telemetry_exporter import read_model_memory_snapshot


def _proc_entry(root: Path, pid: str, *, name: str, peak_kb: int, current_kb: int) -> None:
    process = root / pid
    process.mkdir(parents=True)
    (process / "comm").write_text(name + "\n", encoding="utf-8")
    (process / "status").write_text(
        f"Name:\t{name}\nVmHWM:\t{peak_kb} kB\nVmRSS:\t{current_kb} kB\n",
        encoding="utf-8",
    )


def test_model_telemetry_reads_peak_and_current_rss_from_procfs(tmp_path):
    _proc_entry(tmp_path, "101", name="llama-server", peak_kb=2_097_152, current_kb=1_048_576)

    snapshot = read_model_memory_snapshot(tmp_path)

    assert snapshot == {"peak_ram_mb": 2048.0, "current_ram_mb": 1024.0}


def test_model_telemetry_selects_largest_matching_model_process(tmp_path):
    _proc_entry(tmp_path, "101", name="llama-server", peak_kb=1_048_576, current_kb=512_000)
    _proc_entry(tmp_path, "102", name="python", peak_kb=9_000_000, current_kb=8_000_000)
    _proc_entry(tmp_path, "103", name="llama-server", peak_kb=2_097_152, current_kb=1_500_000)

    snapshot = read_model_memory_snapshot(tmp_path)

    assert snapshot["peak_ram_mb"] == 2048.0
    assert snapshot["current_ram_mb"] == pytest.approx(1_500_000 / 1024)


def test_model_telemetry_fails_closed_when_model_process_is_missing(tmp_path):
    with pytest.raises(RuntimeError, match="was not found"):
        read_model_memory_snapshot(tmp_path)


def test_model_telemetry_rejects_unsafe_process_name(tmp_path):
    with pytest.raises(ValueError, match="process name"):
        read_model_memory_snapshot(tmp_path, process_name="../llama-server")


def test_model_telemetry_client_authenticates_and_checks_model_identity():
    captured = {}

    def handler(request):
        captured["headers"] = request.headers
        return httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "model_id": "rusaai-edge-derived",
                "peak_ram_mb": 2048.0,
                "current_ram_mb": 1800.0,
                "sampled_at_unix": time.time(),
            },
            request=request,
        )

    result = asyncio.run(
        fetch_model_telemetry(
            "https://model.internal/v1/telemetry",
            "t" * 40,
            "rusaai-edge-derived",
            transport=httpx.MockTransport(handler),
        )
    )

    assert captured["headers"]["X-API-Key"] == "t" * 40
    assert result["peak_ram_mb"] == 2048.0
    assert result["current_ram_mb"] == 1800.0


def test_model_telemetry_client_rejects_wrong_model_identity():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "model_id": "different-model",
                "peak_ram_mb": 2048.0,
                "current_ram_mb": 1800.0,
                "sampled_at_unix": time.time(),
            },
            request=request,
        )
    )

    with pytest.raises(ModelTelemetryError, match="identity"):
        asyncio.run(
            fetch_model_telemetry(
                "https://model.internal/v1/telemetry",
                "t" * 40,
                "rusaai-edge-derived",
                transport=transport,
            )
        )


def test_model_telemetry_client_rejects_missing_or_non_finite_memory_values():
    for value in (None, True, -1, float("nan"), float("inf")):
        transport = httpx.MockTransport(
            lambda request, value=value: httpx.Response(
                200,
                json={
                    "schema_version": "1.0",
                    "model_id": "rusaai-edge-derived",
                    "peak_ram_mb": value,
                    "current_ram_mb": 1800.0,
                    "sampled_at_unix": time.time(),
                },
                request=request,
            )
        )
        with pytest.raises(ModelTelemetryError, match="peak_ram_mb"):
            asyncio.run(
                fetch_model_telemetry(
                    "https://model.internal/v1/telemetry",
                    "t" * 40,
                    "rusaai-edge-derived",
                    transport=transport,
                )
            )



def test_model_telemetry_client_rejects_stale_samples():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "model_id": "rusaai-edge-derived",
                "peak_ram_mb": 2048.0,
                "current_ram_mb": 1800.0,
                "sampled_at_unix": time.time() - 120,
            },
            request=request,
        )
    )

    with pytest.raises(ModelTelemetryError, match="stale"):
        asyncio.run(
            fetch_model_telemetry(
                "https://model.internal/v1/telemetry",
                "t" * 40,
                "rusaai-edge-derived",
                transport=transport,
            )
        )
