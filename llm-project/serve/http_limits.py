"""Bound request bodies before JSON parsing, including chunked requests."""

import asyncio
from collections.abc import Callable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class BodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        max_bytes: Callable[[], int],
        timeout: float = 15.0,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.timeout = timeout

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.max_bytes()
        if limit <= 0:
            raise RuntimeError("Request body limit must be positive")
        body = bytearray()

        async def collect() -> str:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return "disconnected"
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > limit:
                    return "oversized"
                body.extend(chunk)
                if not message.get("more_body", False):
                    return "complete"

        try:
            outcome = await asyncio.wait_for(collect(), timeout=self.timeout)
        except asyncio.TimeoutError:
            response = JSONResponse(
                {"detail": "Request body timed out"},
                status_code=408,
            )
            await response(scope, receive, send)
            return

        if outcome == "disconnected":
            return
        if outcome == "oversized":
            response = JSONResponse(
                {"detail": "Request body exceeds the configured size limit"},
                status_code=413,
            )
            await response(scope, receive, send)
            return

        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {
                    "type": "http.request",
                    "body": bytes(body),
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, replay, send)
