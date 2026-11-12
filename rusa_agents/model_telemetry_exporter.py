"""Small, read-only telemetry endpoint for a colocated llama.cpp process."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MODEL_ARTIFACT_SHA256: str | None = None
MODEL_MANIFEST_VERIFIED = False


def compute_artifact_sha256(artifact_path: str | Path | None) -> str | None:
    """Hash the mounted model artifact once when the exporter starts."""
    if not artifact_path:
        return None
    path = Path(artifact_path)
    if not path.is_file():
        raise RuntimeError("Configured model artifact path is not a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_model_memory_snapshot(
    proc_root: Path = Path("/proc"),
    process_name: str = "llama-server",
) -> dict[str, float]:
    """Read VmHWM and VmRSS for the named model process from procfs."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", process_name):
        raise ValueError("Model process name is invalid")
    if not proc_root.is_dir():
        raise RuntimeError("Process telemetry requires a readable procfs mount")

    candidates: list[dict[str, float]] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
            if comm != process_name:
                continue
            status = (entry / "status").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        values: dict[str, int] = {}
        for line in status.splitlines():
            key, separator, raw = line.partition(":")
            if separator and key in {"VmHWM", "VmRSS"}:
                match = re.fullmatch(r"\s*(\d+)\s+kB\s*", raw)
                if match:
                    values[key] = int(match.group(1))
        if "VmHWM" in values and "VmRSS" in values:
            candidates.append(
                {
                    "peak_ram_mb": values["VmHWM"] / 1024.0,
                    "current_ram_mb": values["VmRSS"] / 1024.0,
                }
            )

    if not candidates:
        raise RuntimeError(f"Model process {process_name!r} was not found or has no RSS metrics")
    return max(candidates, key=lambda item: item["current_ram_mb"])


class TelemetryHandler(BaseHTTPRequestHandler):
    server_version = "RusaModelTelemetry/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/health/live":
            self._send_json(200, {"status": "alive"})
            return
        if self.path != "/v1/telemetry":
            self._send_json(404, {"error": "not_found"})
            return

        expected_key = os.getenv("RUSA_MODEL_TELEMETRY_API_KEY", "")
        supplied_key = self.headers.get("X-API-Key", "")
        if not expected_key or not hmac.compare_digest(supplied_key, expected_key):
            self._send_json(401, {"error": "unauthorized"})
            return

        try:
            snapshot = read_model_memory_snapshot(
                process_name=os.getenv("RUSA_MODEL_TELEMETRY_PROCESS_NAME", "llama-server")
            )
        except (OSError, RuntimeError, ValueError):
            self._send_json(503, {"error": "model_telemetry_unavailable"})
            return

        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "model_id": os.getenv("RUSA_MODEL_NAME", "unknown"),
            "peak_ram_mb": snapshot["peak_ram_mb"],
            "current_ram_mb": snapshot["current_ram_mb"],
            "artifact_sha256": MODEL_ARTIFACT_SHA256,
            "manifest_verified": MODEL_MANIFEST_VERIFIED,
            "sampled_at_unix": time.time(),
        }
        self._send_json(200, payload)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # Never log request headers, credentials, prompts, or model output.
        return


def main() -> None:
    api_key = os.getenv("RUSA_MODEL_TELEMETRY_API_KEY", "")
    if len(api_key.encode("utf-8")) < 32:
        raise RuntimeError("RUSA_MODEL_TELEMETRY_API_KEY must contain at least 32 bytes")
    host = os.getenv("RUSA_MODEL_TELEMETRY_BIND", "127.0.0.1")
    try:
        port = int(os.getenv("RUSA_MODEL_TELEMETRY_PORT", "9101"))
    except ValueError as exc:
        raise ValueError("RUSA_MODEL_TELEMETRY_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("RUSA_MODEL_TELEMETRY_PORT must be between 1 and 65535")
    global MODEL_ARTIFACT_SHA256, MODEL_MANIFEST_VERIFIED
    MODEL_ARTIFACT_SHA256 = compute_artifact_sha256(
        os.getenv("RUSA_MODEL_ARTIFACT_PATH")
    )
    expected_digest = os.getenv("RUSA_MODEL_ARTIFACT_SHA256", "")
    MODEL_MANIFEST_VERIFIED = bool(
        MODEL_ARTIFACT_SHA256
        and re.fullmatch(r"[0-9a-f]{64}", expected_digest)
        and hmac.compare_digest(MODEL_ARTIFACT_SHA256, expected_digest)
    )
    if os.getenv("ENVIRONMENT", "development").lower() == "production":
        if not MODEL_MANIFEST_VERIFIED:
            raise RuntimeError(
                "Production model telemetry cannot start without a matching pinned model artifact digest"
            )
    server = ThreadingHTTPServer((host, port), TelemetryHandler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
