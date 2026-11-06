from __future__ import annotations

from time import perf_counter
from typing import Any

from prometheus_client import make_asgi_app

from .metrics import record_http_request


class PrometheusASGIMiddleware:
    """Expose /metrics and record bounded-cardinality HTTP request metrics."""

    def __init__(self, app: Any, service_name: str) -> None:
        self.app = app
        self.service_name = service_name
        self.metrics_app = make_asgi_app()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == "/metrics" or path.startswith("/metrics/"):
            await self.metrics_app(scope, receive, send)
            return

        started = perf_counter()
        status_code = 500

        async def send_with_status(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        except BaseException:
            self._record(scope, status_code, perf_counter() - started)
            raise
        else:
            self._record(scope, status_code, perf_counter() - started)

    def _record(self, scope: dict[str, Any], status_code: int, duration: float) -> None:
        route = scope.get("route")
        route_name = getattr(route, "path", None) or "unmatched"
        record_http_request(
            self.service_name,
            str(scope.get("method", "GET")).upper(),
            route_name,
            status_code,
            duration,
        )


def install_metrics(app: Any, service_name: str) -> None:
    app.add_middleware(PrometheusASGIMiddleware, service_name=service_name)
