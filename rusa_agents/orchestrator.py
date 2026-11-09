"""Hierarchical DAG orchestrator with bounded parallelism and evidence-gated repair."""

from __future__ import annotations

import asyncio

from .audit import AuditLog
from .contracts import CodingTask, TaskRecord, TaskState, WorkerResult, WorkerRole
from .memory import SQLiteMemory
from .metrics import WORKER_STEPS
from .policy import AutonomyPolicy
from .workers import Worker


class AgentManager:
    """Dispatch specialist agents within a manager-owned capability boundary."""

    def __init__(
        self,
        name: str,
        roles: tuple[WorkerRole, ...],
        workers: dict[WorkerRole, Worker],
    ) -> None:
        self.name = name
        self.roles = frozenset(roles)
        self.workers = workers

    async def dispatch(
        self,
        role: WorkerRole,
        task: CodingTask,
        prior_results: list[WorkerResult],
        semaphore: asyncio.Semaphore,
    ) -> WorkerResult:
        if role not in self.roles:
            raise ValueError(f"{self.name} manager cannot dispatch {role.value}")
        worker = self.workers.get(role)
        if worker is None:
            raise RuntimeError(f"{self.name} manager has no worker for {role.value}")
        async with semaphore:
            return await worker.run(task, prior_results)


class AICodeOrchestrator:
    """Root supervisor that schedules repository, engineering, and quality managers."""

    MANAGER_ROLES = {
        "repository": (WorkerRole.ANALYST, WorkerRole.ARCHITECT),
        "engineering": (WorkerRole.DEVELOPER, WorkerRole.DEBUGGER),
        "quality": (WorkerRole.TESTER, WorkerRole.SECURITY),
    }
    EXECUTION_DAG = {
        WorkerRole.ANALYST: (),
        WorkerRole.ARCHITECT: (WorkerRole.ANALYST,),
        WorkerRole.DEVELOPER: (WorkerRole.ARCHITECT,),
        WorkerRole.DEBUGGER: (WorkerRole.DEVELOPER,),
        WorkerRole.TESTER: (WorkerRole.DEVELOPER,),
        WorkerRole.SECURITY: (WorkerRole.DEVELOPER,),
    }
    PRIMARY_ROLES = (
        WorkerRole.ANALYST,
        WorkerRole.ARCHITECT,
        WorkerRole.DEVELOPER,
        WorkerRole.DEBUGGER,
    )
    QUALITY_ROLES = (WorkerRole.TESTER, WorkerRole.SECURITY)
    PIPELINE = PRIMARY_ROLES + QUALITY_ROLES
    REQUIRED_TEST_EVIDENCE = ("check:pytest",)
    REQUIRED_SECURITY_EVIDENCE = ("check:bandit", "check:ruff", "check:compile", "check:refinery")

    def __init__(
        self,
        workers: list[Worker],
        policy: AutonomyPolicy | None = None,
        memory: SQLiteMemory | None = None,
        audit: AuditLog | None = None,
        max_parallel_workers: int = 4,
        max_repair_cycles: int = 2,
    ):
        self.workers = {worker.role: worker for worker in workers}
        self.policy = policy or AutonomyPolicy()
        self.memory = memory or SQLiteMemory()
        self.audit = audit or AuditLog()
        if not 1 <= max_parallel_workers <= 32:
            raise ValueError("max_parallel_workers must be between 1 and 32")
        if not 0 <= max_repair_cycles <= 5:
            raise ValueError("max_repair_cycles must be between 0 and 5")
        self.max_parallel_workers = max_parallel_workers
        self.max_repair_cycles = max_repair_cycles

        missing = set(self.PIPELINE) - set(self.workers)
        if missing:
            raise ValueError(
                "missing required workers: "
                + ", ".join(role.value for role in sorted(missing, key=lambda item: item.value))
            )

        self.role_manager = {
            role: manager_name
            for manager_name, roles in self.MANAGER_ROLES.items()
            for role in roles
        }
        self.managers = {
            name: AgentManager(name, roles, self.workers)
            for name, roles in self.MANAGER_ROLES.items()
        }
        for role, dependencies in self.EXECUTION_DAG.items():
            if role not in self.PIPELINE or not set(dependencies).issubset(self.PIPELINE):
                raise ValueError("execution DAG contains an unknown role dependency")

    @staticmethod
    def _tenant_id(task: CodingTask) -> str:
        tenant_id = str(task.metadata.get("tenant_id", "default"))
        if not tenant_id:
            raise ValueError("task tenant_id cannot be empty")
        return tenant_id

    async def _persist(self, record: TaskRecord) -> None:
        payload = {
            "objective": record.task.objective,
            "acceptance_criteria": record.task.acceptance_criteria,
            "results": [
                {
                    "role": result.role.value,
                    "summary": result.summary,
                    "success": result.success,
                    "artifacts": result.artifacts,
                    "evidence": result.evidence,
                    "blockers": result.blockers,
                }
                for result in record.results
            ],
            "errors": record.errors,
        }
        await asyncio.to_thread(
            self.memory.save,
            record.task.task_id,
            record.task.repository.full_name,
            record.state.value,
            payload,
            tenant_id=self._tenant_id(record.task),
        )

    async def _audit(
        self, event: str, task: CodingTask, actor: str, details: dict
    ) -> None:
        await asyncio.to_thread(
            self.audit.record,
            event,
            task.task_id,
            actor,
            details,
            tenant_id=self._tenant_id(task),
        )

    async def _dispatch(
        self,
        role: WorkerRole,
        task: CodingTask,
        prior_results: list[WorkerResult],
        semaphore: asyncio.Semaphore,
    ) -> WorkerResult:
        manager_name = self.role_manager[role]
        return await self.managers[manager_name].dispatch(
            role, task, prior_results, semaphore
        )

    async def _execute(
        self,
        role: WorkerRole,
        task: CodingTask,
        prior_results: list[WorkerResult],
        semaphore: asyncio.Semaphore,
    ) -> WorkerResult:
        try:
            result = await self._dispatch(role, task, prior_results, semaphore)
        except Exception as exc:
            message = f"{type(exc).__name__}: {str(exc)[:1000]}"
            return WorkerResult(
                role=role,
                summary=message,
                success=False,
                blockers=[f"{role.value} worker execution failed: {message}"],
            )
        if result.role != role:
            return WorkerResult(
                role=role,
                summary="Worker returned a result for the wrong specialist role.",
                success=False,
                blockers=[f"{role.value} worker role contract mismatch"],
            )
        return result

    async def _record_result(
        self,
        record: TaskRecord,
        result: WorkerResult,
    ) -> None:
        manager_name = self.role_manager[result.role]
        WORKER_STEPS.labels(
            result.role.value, "success" if result.success else "failure"
        ).inc()
        record.results.append(result)
        await self._audit(
            "worker.completed",
            record.task,
            result.role.value,
            {
                "manager": manager_name,
                "success": result.success,
                "summary": result.summary[:500],
                "evidence_summary": {
                    key: {
                        field: value[field]
                        for field in (
                            "exit_code", "quality_score", "report_sha256",
                            "files_analyzed", "backend", "run_id",
                            "snapshot_sha256", "runner_image_digest",
                        )
                        if field in value
                    }
                    for key, value in list(result.evidence.items())[:20]
                    if isinstance(value, dict)
                },
            },
        )
        await self._persist(record)

    @staticmethod
    def _check_passed(result: WorkerResult, key: str) -> bool:
        evidence = result.evidence.get(key)
        if not isinstance(evidence, dict) or evidence.get("exit_code") != 0:
            return False
        if key != "check:refinery":
            return True
        score = evidence.get("quality_score")
        digest = evidence.get("report_sha256")
        return (
            isinstance(score, (int, float))
            and not isinstance(score, bool)
            and score >= 85.0
            and isinstance(digest, str)
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
            and evidence.get("syntax_valid") is True
            and evidence.get("blocking_findings") == 0
        )

    def _verify_evidence(self, record: TaskRecord) -> bool | None:
        tester = next(
            (item for item in reversed(record.results) if item.role == WorkerRole.TESTER),
            None,
        )
        security = next(
            (item for item in reversed(record.results) if item.role == WorkerRole.SECURITY),
            None,
        )
        if tester is None or security is None:
            record.errors.append("Tester or security worker did not produce a result.")
            return None
        required = [
            (tester, key) for key in self.REQUIRED_TEST_EVIDENCE
        ] + [
            (security, key) for key in self.REQUIRED_SECURITY_EVIDENCE
        ]
        missing = [
            key for worker_result, key in required
            if key not in worker_result.evidence
        ]
        if missing:
            record.errors.append(
                "Missing mandatory verification evidence: " + ", ".join(missing)
            )
            return None
        failed = [
            key for worker_result, key in required
            if not self._check_passed(worker_result, key)
        ]
        if failed:
            record.errors.append("Validation checks failed: " + ", ".join(failed))
            return False
        return True

    async def _run_primary_roles(
        self,
        record: TaskRecord,
        prior_results: list[WorkerResult],
        semaphore: asyncio.Semaphore,
        roles: tuple[WorkerRole, ...],
    ) -> None:
        completed_roles = {
            result.role for result in prior_results + list(record.results)
        }
        for role in roles:
            missing_dependencies = set(self.EXECUTION_DAG[role]) - completed_roles
            if missing_dependencies:
                record.errors.append(
                    f"{role.value} is missing DAG dependencies: "
                    + ", ".join(sorted(item.value for item in missing_dependencies))
                )
                record.transition(TaskState.BLOCKED)
                return
            action = "workspace_write" if role == WorkerRole.DEVELOPER else "repo_read"
            decision = self.policy.evaluate(action)
            if not decision.allowed:
                record.errors.append(decision.reason)
                record.transition(TaskState.BLOCKED)
                return
            result = await self._execute(
                role,
                record.task,
                prior_results + list(record.results),
                semaphore,
            )
            await self._record_result(record, result)
            completed_roles.add(role)
            if not result.success or result.blockers:
                record.errors.extend(
                    result.blockers or [role.value + " worker failed"]
                )
                record.transition(TaskState.BLOCKED)
                return
            completed_roles.add(role)

    async def run(self, task: CodingTask) -> TaskRecord:
        record = TaskRecord(task=task)
        previous_attempt_results: list[WorkerResult] = []
        semaphore = asyncio.Semaphore(self.max_parallel_workers)
        try:
            record.transition(TaskState.OBSERVING)
            await self._audit(
                "task.received",
                task,
                "aicode-root",
                {"repository": task.repository.full_name},
            )
            previous = await asyncio.to_thread(
                self.memory.get,
                task.task_id,
                tenant_id=self._tenant_id(task),
            )
            if previous:
                previous_payload = previous.get("payload", {})
                for item in previous_payload.get("results", []):
                    try:
                        previous_attempt_results.append(
                            WorkerResult(
                                role=WorkerRole(item["role"]),
                                summary=str(item.get("summary", ""))[:4000],
                                success=bool(item.get("success", False)),
                                artifacts=list(item.get("artifacts", []))[:100],
                                evidence=dict(item.get("evidence", {})),
                                blockers=list(item.get("blockers", []))[:30],
                            )
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                prior_errors = previous_payload.get("errors", [])
                if prior_errors:
                    previous_attempt_results.append(
                        WorkerResult(
                            role=WorkerRole.ANALYST,
                            summary=(
                                "Previous attempt ended in state "
                                + str(previous.get("state", "unknown"))
                                + "; errors: "
                                + "; ".join(str(error) for error in prior_errors)[:3000]
                            ),
                            success=False,
                            blockers=[str(error) for error in prior_errors[:10]],
                        )
                    )

            await self._persist(record)
            record.transition(TaskState.PLANNING)
            await self._persist(record)
            if not self.policy.evaluate("repo_read").allowed:
                record.errors.append("Repository read denied by policy")
                record.transition(TaskState.BLOCKED)
                return record

            record.transition(TaskState.EXECUTING)
            await self._persist(record)
            await self._run_primary_roles(
                record,
                previous_attempt_results,
                semaphore,
                self.PRIMARY_ROLES,
            )

            if record.state == TaskState.EXECUTING:
                completed_roles = {
                    result.role for result in previous_attempt_results + record.results
                }
                missing_quality_dependencies = {
                    role: set(self.EXECUTION_DAG[role]) - completed_roles
                    for role in self.QUALITY_ROLES
                }
                if any(missing_quality_dependencies.values()):
                    missing = sorted({
                        dependency.value
                        for dependencies in missing_quality_dependencies.values()
                        for dependency in dependencies
                    })
                    record.errors.append(
                        "Quality DAG dependencies were not satisfied: " + ", ".join(missing)
                    )
                    record.transition(TaskState.BLOCKED)
                    await self._persist(record)

            if record.state == TaskState.EXECUTING:
                for repair_cycle in range(self.max_repair_cycles + 1):
                    record.transition(TaskState.VERIFYING)
                    await self._persist(record)
                    quality_results = await asyncio.gather(
                        *(
                            self._execute(
                                role,
                                task,
                                previous_attempt_results + list(record.results),
                                semaphore,
                            )
                            for role in self.QUALITY_ROLES
                        )
                    )
                    for result in quality_results:
                        await self._record_result(record, result)

                    failed_quality_workers = [
                        result.role.value
                        for result in quality_results
                        if not result.success or result.blockers
                    ]
                    if failed_quality_workers:
                        record.errors.append(
                            "Quality worker execution failed: "
                            + ", ".join(failed_quality_workers)
                        )
                        evidence_result = False
                    else:
                        evidence_result = self._verify_evidence(record)

                    if evidence_result is True:
                        record.transition(TaskState.SUCCEEDED)
                        await self._persist(record)
                        break
                    if evidence_result is None:
                        record.transition(TaskState.AWAITING_APPROVAL)
                        await self._persist(record)
                        break
                    if repair_cycle >= self.max_repair_cycles:
                        record.transition(TaskState.BLOCKED)
                        await self._persist(record)
                        break

                    record.errors.append(
                        f"Quality gates failed; scheduling repair cycle "
                        f"{repair_cycle + 1}/{self.max_repair_cycles}."
                    )
                    record.transition(TaskState.EXECUTING)
                    await self._persist(record)
                    await self._run_primary_roles(
                        record,
                        previous_attempt_results,
                        semaphore,
                        (WorkerRole.DEVELOPER, WorkerRole.DEBUGGER),
                    )
                    if record.state != TaskState.EXECUTING:
                        break

        except Exception as exc:
            record.errors.append(type(exc).__name__ + ": " + str(exc))
            if record.state not in {
                TaskState.FAILED,
                TaskState.BLOCKED,
                TaskState.CANCELLED,
                TaskState.AWAITING_APPROVAL,
                TaskState.SUCCEEDED,
            }:
                record.transition(TaskState.FAILED)
            await self._audit(
                "task.failed",
                task,
                "aicode-root",
                {"error": str(exc)},
            )
        finally:
            await self._persist(record)
            await self._audit(
                "task.finished",
                task,
                "aicode-root",
                {"state": record.state.value, "errors": record.errors},
            )
        return record
