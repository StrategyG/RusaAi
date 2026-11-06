"""Evidence verification. Default implementation rejects all unverified claims."""
from typing import Protocol
from .contracts import CodingTask, Evidence


class EvidenceVerifier(Protocol):
    async def verify(self, evidence: Evidence, task: CodingTask) -> bool: ...


class RejectUnverifiedEvidence:
    async def verify(self, evidence: Evidence, task: CodingTask) -> bool:
        return False
