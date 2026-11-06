"""RUSA-AI AICode worker process entry point."""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from prometheus_client import start_http_server

from .runtime import AICodeRuntime


async def serve() -> None:
    # Preserve group access on setgid workspace roots for the rootless sandbox daemon.
    os.umask(0o027)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    metrics_port = int(os.getenv("RUSA_METRICS_PORT", "9100"))
    if metrics_port > 0:
        start_http_server(
            metrics_port,
            addr=os.getenv("RUSA_METRICS_BIND", "127.0.0.1"),  # nosec B104 - private container network; no host-published metrics port
        )
    runtime = AICodeRuntime()
    await asyncio.to_thread(runtime.initialize)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop_event.set)
    await runtime.run_forever(stop_event=stop_event)


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
