"""RUSA-AI AICode worker process entry point."""
from __future__ import annotations

import asyncio
import logging
import signal

from .runtime import AICodeRuntime


async def serve() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
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
