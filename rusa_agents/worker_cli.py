"""Standalone worker process entry point."""
from __future__ import annotations
import asyncio, logging, os, signal
from .bootstrap import build_planner, build_tooling
from .storage import Database
from .worker import AgentRuntime

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

async def main() -> None:
    database = Database()
    await database.initialize()
    tools, mcp_gateway, _rag_service = await build_tooling(database)
    runtime = AgentRuntime(database, build_planner(), tools)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        while not stop.is_set():
            worked = await runtime.run_once()
            if not worked:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
    finally:
        await mcp_gateway.close()
        await database.close()

if __name__ == "__main__":
    asyncio.run(main())
