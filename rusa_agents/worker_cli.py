"""Standalone concurrent worker process entry point."""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from .bootstrap import build_planner, build_tooling
from .storage import Database
from .worker import AgentRuntime

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())


async def main() -> None:
    database = Database()
    await database.initialize()
    tools, mcp_gateway, _rag_service = await build_tooling(database)
    concurrency = int(os.getenv("RUSA_AGENT_WORKER_CONCURRENCY", "4"))
    if not 1 <= concurrency <= 64:
        raise ValueError("RUSA_AGENT_WORKER_CONCURRENCY must be between 1 and 64")
    runtime = AgentRuntime(
        database,
        build_planner(),
        tools,
        max_concurrency=concurrency,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def poll_worker() -> None:
        while not stop.is_set():
            worked = await runtime.run_once()
            if not worked:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

    workers = [
        asyncio.create_task(poll_worker(), name=f"rusa-agent-poller-{index}")
        for index in range(concurrency)
    ]
    try:
        await stop.wait()
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await mcp_gateway.close()
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
