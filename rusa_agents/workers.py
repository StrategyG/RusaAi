"""Provider-agnostic worker contracts and capability registry."""
from typing import Protocol

from .contracts import TaskSpec, WorkerResult


class AgentWorker(Protocol):
    worker_id: str
    capability: str

    async def run(self, task: TaskSpec, context: list[WorkerResult]) -> WorkerResult:
        """Execute one bounded task and return verifiable evidence."""


class WorkerRegistry:
    def __init__(self) -> None:
        self._workers: dict[str, AgentWorker] = {}

    def register(self, worker: AgentWorker) -> None:
        if not worker.worker_id.strip() or not worker.capability.strip():
            raise ValueError("worker_id and capability are required")
        if worker.capability in self._workers:
            raise ValueError(f"worker capability already registered: {worker.capability}")
        self._workers[worker.capability] = worker

    def get(self, capability: str) -> AgentWorker:
        try:
            return self._workers[capability]
        except KeyError as exc:
            raise LookupError(f"no worker registered for capability: {capability}") from exc

    def capabilities(self) -> tuple[str, ...]:
        return tuple(sorted(self._workers))
