"""Process entry point for the dedicated Predictive DevOps worker."""
from __future__ import annotations

import logging
import os
import signal
import threading

from prometheus_client import start_http_server

from .config import DevOpsConfig
from .worker import DevOpsWorkerRuntime


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = DevOpsConfig.from_env()
    if not config.enabled:
        raise SystemExit("RUSA_DEVOPS_ENABLED must be true to start the DevOps worker")
    metrics_port = int(os.getenv("RUSA_DEVOPS_METRICS_PORT", "9101"))
    if metrics_port > 0:
        start_http_server(metrics_port, addr=os.getenv("RUSA_DEVOPS_METRICS_BIND", "127.0.0.1"))
    runtime = DevOpsWorkerRuntime(config)
    runtime.initialize()
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    try:
        while not stop.is_set():
            if not runtime.run_once():
                stop.wait(config.poll_seconds)
    finally:
        logging.getLogger("rusa-devops-worker").info("worker stopped")


if __name__ == "__main__":
    main()
