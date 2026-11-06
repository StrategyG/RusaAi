"""Standalone worker process entry point."""
from __future__ import annotations
import asyncio, logging, os, signal
from .mcp_gateway import MCPGateway
from .providers import OpenAICompatibleProvider
from .storage import Database
from .tools import GitHubTools, ToolRegistry
from .worker import AgentRuntime

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

async def main() -> None:
    database = Database()
    await database.initialize()
    provider = OpenAICompatibleProvider()
    tools = ToolRegistry()
    GitHubTools().register(tools)
    mcp_gateway = MCPGateway.from_env()
    await mcp_gateway.connect_and_register(tools)
    runtime = AgentRuntime(database, provider, tools)
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
