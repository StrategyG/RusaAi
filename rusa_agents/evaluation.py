"""Deterministic evidence and acceptance-criteria evaluator."""
from dataclasses import dataclass

from .contracts import TaskSpec, WorkerResult


@dataclass(frozen=True)
class EvaluationReport:
    passed: bool
    score: float
    missing_criteria: tuple[str, ...]
    security_violations: tuple[str, ...]
    reasons: tuple[str, ...]


class ResultEvaluator:
    def evaluate(self, task: TaskSpec, result: WorkerResult) -> EvaluationReport:
        missing = tuple(
            criterion
            for criterion in task.acceptance_criteria
            if criterion not in result.completed_criteria
        )
        violations = tuple(result.security_violations)
        reasons: list[str] = []
        if not result.success:
            reasons.append("worker reported unsuccessful execution")
        if missing:
            reasons.append("one or more acceptance criteria are not evidenced")
        if violations:
            reasons.append("security violations are a hard failure")
        passed = result.success and not missing and not violations
        total = len(task.acceptance_criteria)
        completed = total - len(missing)
        score = (completed / total) * 100 if total else 0.0
        if not result.success or violations:
            score = 0.0
        return EvaluationReport(
            passed=passed,
            score=score,
            missing_criteria=missing,
            security_violations=violations,
            reasons=tuple(reasons),
        )
