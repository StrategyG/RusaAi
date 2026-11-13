"""Production worker runtime wiring PostgreSQL, model provider, Git worktrees, and sandbox."""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import socket
import time
from collections.abc import Coroutine, Mapping
from typing import Any

from .agent_tools import ROLE_PERMISSION_ALLOWLIST, ROLE_TOOL_ALLOWLIST
from .audit import PostgresAuditLog, PostgresTaskPolicyAudit, record_task_policy
from .autonomous import AutonomousCodeWorker
from .capability_registry import RegistryNotActivatedError
from .config import RuntimeConfig
from .contracts import CodingTask, RepositoryTarget, TaskState, WorkerRole
from .git_workspace import GitHubPublisher, GitWorkspaceManager
from .memory import PostgresMemory
from .metrics import AICODE_TASK_DURATION, AICODE_TASKS
from .orchestrator import AICodeOrchestrator
from .policy import AutonomyPolicy, RiskLevel
from .providers import OpenAICompatibleProvider
from .queue import Job, LeaseLostError, PostgresTaskQueue
from .runtime_v1 import AgentRuntimeV1
from .task_decomposer import TaskNode, TaskPlan
from .tool_gateway import DockerSandbox, WorkspaceToolGateway
from .worker_execution import (
    AICodeWorkerAdapter,
    LeaseBoundCheckpointStore,
    PolicyAuthorizer,
    ResultVerifier,
    TaskExecutionResult,
    TaskMemoryCheckpointStore,
    VerificationDecision,
    WorkerExecutionEngine,
    WorkerHandler,
    WorkerOutput,
)
from .worker_execution import PolicyDecision as AgentPolicyDecision

logger = logging.getLogger("rusa-ai.aicode.runtime")


class _ScopedAICodeWorkerHandler:
    """Apply a task-node tool allowlist on top of the worker's fixed role boundary."""

    def __init__(self, worker: Any, repository: RepositoryTarget, tenant_id: str, context: str):
        self.worker = worker
        self.repository = repository
        self.tenant_id = tenant_id
        self.context = context

    async def execute(self, node: TaskNode, dependency_results: Mapping[str, Any]) -> WorkerOutput:
        if node.selected_role_id != self.worker.role.value:
            raise PermissionError("Agent Runtime node role does not match its worker adapter")
        allowed_tools = frozenset(node.tool_allowlist) if node.tool_allowlist else ROLE_TOOL_ALLOWLIST[self.worker.role]
        if not allowed_tools.issubset(ROLE_TOOL_ALLOWLIST[self.worker.role]):
            raise PermissionError(
                f"Task tool allowlist exceeds the registered {self.worker.role.value} role permissions"
            )
        scoped_worker = AutonomousCodeWorker(
            self.worker.role,
            self.worker.provider,
            self.worker.gateway,
            max_steps=self.worker.max_steps,
            max_actions_per_step=self.worker.max_actions_per_step,
            max_output_chars=self.worker.max_output_chars,
            allowed_tools=allowed_tools,
        )
        adapter = AICodeWorkerAdapter(
            scoped_worker, self.repository, self.tenant_id, self.context
        )
        return await adapter.execute(node, dependency_results)


class AICodeRuntime:
    def __init__(self, config: RuntimeConfig | None = None, worker_id: str | None = None):
        self.config = config or RuntimeConfig.from_env()
        self.config.validate_worker()
        if not self.config.worker_database_url or not self.config.workspace_root or not self.config.git_mirror_root:
            raise RuntimeError("Database, workspace root, and Git mirror root are required")
        if self.config.environment == "production" and not os.getenv("DOCKER_HOST"):
            raise RuntimeError("Production worker requires DOCKER_HOST connected to a dedicated rootless sandbox daemon")
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.agent_runtime: AgentRuntimeV1 | None = None
        self.queue = PostgresTaskQueue(self.config.worker_database_url)
        self.memory = PostgresMemory(self.config.worker_database_url)
        if not self.config.audit_hmac_key:
            raise RuntimeError("RUSA_AUDIT_HMAC_KEY is required by the worker runtime")
        self.audit = PostgresAuditLog(self.config.worker_database_url, self.config.audit_hmac_key)
        self.task_policy_audit = PostgresTaskPolicyAudit(self.config.worker_database_url)
        self.workspace_manager = GitWorkspaceManager(
            self.config.git_mirror_root,
            self.config.workspace_root,
            github_token=self.config.github_token,
        )
        self.provider = OpenAICompatibleProvider()
        self.publisher = GitHubPublisher(self.config.github_token)
        self.sandbox = DockerSandbox(image=self.config.sandbox_image)
        rag_flag = os.getenv("RUSA_RAG_ENABLED", "false").strip().lower()
        if rag_flag not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            raise RuntimeError("RUSA_RAG_ENABLED must be a boolean value")
        self.knowledge_service = None
        if rag_flag in {"1", "true", "yes", "on"}:
            from rusa_knowledge.service import KnowledgeService

            knowledge_dsn = os.getenv("RUSA_KNOWLEDGE_DATABASE_URL") or self.config.worker_database_url
            self.knowledge_service = KnowledgeService.from_env(
                check_ready=True,
                database_url=knowledge_dsn,
            )


    def close(self) -> None:
        """Release optional retrieval resources during worker shutdown."""
        if self.knowledge_service is not None:
            self.knowledge_service.close()
            self.knowledge_service = None

    def _load_agent_runtime_registry(self) -> dict[str, Any]:
        """Load the v1 registry and verify the existing six-worker AICode binding."""
        runtime = AgentRuntimeV1()
        compatibility = runtime.validate_aicode_worker_bindings(
            role.value for role in AICodeOrchestrator.PIPELINE
        )
        self.agent_runtime = runtime
        logger.info(
            "Agent Runtime v1 registry loaded: status=%s roles=%s dispatchable=%s",
            runtime.status()["status"],
            runtime.status()["role_count"],
            compatibility["dispatchable"],
        )
        return compatibility

    def configure_agent_runtime_dispatch(
        self,
        handlers: Mapping[str, WorkerHandler],
        policy_authorizer: PolicyAuthorizer,
        result_verifier: ResultVerifier,
        *,
        workspace: str | None = None,
        coding_task: CodingTask | None = None,
    ) -> None:
        """Inject an explicit reviewed adapter override for specialized deployments."""
        if not isinstance(handlers, Mapping) or not handlers:
            raise ValueError("Agent Runtime dispatch requires a non-empty handler mapping")
        if not callable(policy_authorizer) or not callable(result_verifier):
            raise TypeError("Agent Runtime policy and verifier adapters must be callable")
        normalized = dict(handlers)
        if any(
            not isinstance(role_id, str)
            or not role_id
            or not callable(getattr(handler, "execute", None))
            for role_id, handler in normalized.items()
        ):
            raise TypeError("Every Agent Runtime handler must expose an async execute method")
        if (workspace is None) != (coding_task is None):
            raise ValueError("Agent Runtime publication requires both workspace and coding_task")
        self._agent_runtime_dispatch = {
            "handlers": normalized,
            "policy_authorizer": policy_authorizer,
            "result_verifier": result_verifier,
            "workspace": workspace,
            "coding_task": coding_task,
        }

    @staticmethod
    def _agent_runtime_plan_from_job(job: Job) -> TaskPlan:
        """Parse the versioned queue payload without trusting caller tenant/root IDs."""
        payload = job.payload
        if not isinstance(payload, dict):
            raise ValueError("Agent Runtime queue payload must be an object")
        raw_plan = payload.get("agent_runtime_plan")
        if not isinstance(raw_plan, dict):
            raise ValueError("Agent Runtime queue payload requires agent_runtime_plan")
        if set(raw_plan) - {"schema_version", "objective", "nodes"}:
            raise ValueError("Agent Runtime plan contains unsupported top-level fields")
        if raw_plan.get("schema_version", "1.0") != "1.0":
            raise ValueError("Unsupported Agent Runtime plan schema_version")
        objective = raw_plan.get("objective")
        raw_nodes = raw_plan.get("nodes")
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("Agent Runtime plan objective is required")
        if not isinstance(raw_nodes, list) or not 1 <= len(raw_nodes) <= 64:
            raise ValueError("Agent Runtime plan must contain between 1 and 64 nodes")

        tuple_fields = {
            "acceptance_criteria",
            "dependencies",
            "tool_allowlist",
            "requested_permissions",
            "context_refs",
        }
        required_fields = {
            "task_id",
            "selected_role_id",
            "scope",
            "objective",
            "delegated_by_role_id",
            "acceptance_criteria",
        }
        allowed_fields = set(TaskNode.__dataclass_fields__)
        nodes: list[TaskNode] = []
        for index, raw_node in enumerate(raw_nodes):
            if not isinstance(raw_node, dict):
                raise ValueError(f"Agent Runtime node {index} must be an object")
            unknown = set(raw_node) - allowed_fields
            if unknown:
                raise ValueError(
                    f"Agent Runtime node {index} contains unsupported fields: "
                    + ", ".join(sorted(unknown))
                )
            missing = required_fields - set(raw_node)
            if missing:
                raise ValueError(
                    f"Agent Runtime node {index} is missing required fields: "
                    + ", ".join(sorted(missing))
                )
            normalized = dict(raw_node)
            for field_name in tuple_fields:
                if field_name in normalized:
                    value = normalized[field_name]
                    if not isinstance(value, list) or any(
                        not isinstance(item, str) for item in value
                    ):
                        raise ValueError(
                            f"Agent Runtime node {index} field {field_name} must be an array of strings"
                        )
                    normalized[field_name] = tuple(value)
            for field_name in required_fields - {"acceptance_criteria"}:
                if not isinstance(normalized[field_name], str):
                    raise ValueError(
                        f"Agent Runtime node {index} field {field_name} must be a string"
                    )
            for field_name in allowed_fields - required_fields - tuple_fields:
                if field_name in normalized and normalized[field_name] is not None:
                    if field_name == "retry_limit":
                        if type(normalized[field_name]) is not int:
                            raise ValueError("Agent Runtime retry_limit must be an integer")
                    elif not isinstance(normalized[field_name], str):
                        raise ValueError(
                            f"Agent Runtime node {index} field {field_name} must be a string or null"
                        )
            nodes.append(TaskNode(**normalized))

        return TaskPlan(
            root_task_id=job.task_id,
            tenant_id=job.tenant_id,
            objective=objective,
            nodes=tuple(nodes),
        )

    def _agent_runtime_coding_task(self, job: Job, plan: TaskPlan) -> CodingTask:
        repository = job.payload.get("repository")
        if not isinstance(repository, dict) or set(repository) - {"full_name", "base_branch"}:
            raise ValueError("Agent Runtime queue payload requires a valid repository target")
        full_name = repository.get("full_name")
        base_branch = repository.get("base_branch", "main")
        if not isinstance(full_name, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name
        ) or any(part in {".", ".."} for part in full_name.split("/", 1)):
            raise ValueError("Agent Runtime repository must use a valid owner/name identifier")
        if not isinstance(base_branch, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/-]{0,180}", base_branch
        ) or ".." in base_branch or "//" in base_branch or base_branch.endswith("/"):
            raise ValueError("Agent Runtime base branch is invalid")

        allowed_repositories = getattr(self.config, "tenant_repositories", {}).get(job.tenant_id)
        if getattr(self.config, "environment", "development") == "production" and not allowed_repositories:
            raise PermissionError("Tenant has no configured repository permissions")
        if allowed_repositories is not None and full_name not in allowed_repositories:
            raise PermissionError("Tenant is not authorized for this repository")

        context = job.payload.get("context", "")
        if not isinstance(context, str) or len(context) > 20000:
            raise ValueError("Agent Runtime context must be text of at most 20000 characters")
        acceptance_criteria = list(dict.fromkeys(
            criterion for node in plan.nodes for criterion in node.acceptance_criteria
        ))
        lease_suffix = job.lease_token.replace("-", "")[:8]
        return CodingTask(
            objective=plan.objective,
            repository=RepositoryTarget(
                full_name=full_name,
                base_branch=base_branch,
                work_branch=f"feature/aicode/{job.task_id[:12]}-{lease_suffix}",
            ),
            acceptance_criteria=acceptance_criteria,
            context=context,
            task_id=job.task_id,
            metadata={"tenant_id": job.tenant_id, "lease_token": job.lease_token},
        )

    def _validate_agent_runtime_quality_gates(self, plan: TaskPlan) -> None:
        developer_ids = {
            node.task_id for node in plan.nodes if node.selected_role_id == "developer"
        }
        if not developer_ids:
            return
        roles = {node.selected_role_id for node in plan.nodes}
        missing = {"tester", "security"} - roles
        if missing:
            raise ValueError(
                "Repository implementation plans require tester and security nodes; missing: "
                + ", ".join(sorted(missing))
            )
        if self.agent_runtime is None:
            self._load_agent_runtime_registry()
        if self.agent_runtime is None:
            raise RuntimeError("Agent Runtime registry is unavailable")
        validation = self.agent_runtime.validate_plan(plan)
        dependencies = validation.effective_dependencies

        def ancestors(task_id: str) -> set[str]:
            found: set[str] = set()
            pending = list(dependencies[task_id])
            while pending:
                parent_id = pending.pop()
                if parent_id in found:
                    continue
                found.add(parent_id)
                pending.extend(dependencies[parent_id])
            return found

        for node in plan.nodes:
            if node.selected_role_id in {"tester", "security"}:
                missing_implementations = developer_ids - ancestors(node.task_id)
                if missing_implementations:
                    raise ValueError(
                        f"{node.selected_role_id} node {node.task_id!r} must depend on every "
                        "developer node before quality verification"
                    )

    @staticmethod
    def _agent_runtime_policy_authorizer(node: TaskNode, tenant_id: str) -> AgentPolicyDecision:
        try:
            role = WorkerRole(node.selected_role_id)
        except ValueError:
            return AgentPolicyDecision(
                allowed=False, reason="No reviewed policy mapping exists for this worker role"
            )
        allowed = ROLE_PERMISSION_ALLOWLIST.get(role)
        if allowed is None:
            return AgentPolicyDecision(
                allowed=False, reason="No reviewed policy mapping exists for this worker role"
            )
        requested = frozenset(node.requested_permissions) or allowed
        if not requested.issubset(allowed):
            return AgentPolicyDecision(
                allowed=False,
                reason="Requested permissions exceed the selected worker role policy",
            )
        policy = AutonomyPolicy()
        risk = RiskLevel(node.risk_class)
        decisions = [policy.evaluate(action, risk) for action in sorted(requested)]
        approval = next((decision for decision in decisions if decision.requires_approval), None)
        if approval is not None:
            return AgentPolicyDecision(
                allowed=False, requires_approval=True, reason=approval.reason
            )
        denied = next((decision for decision in decisions if not decision.allowed), None)
        if denied is not None:
            return AgentPolicyDecision(allowed=False, reason=denied.reason)
        return AgentPolicyDecision(allowed=True, reason="Allowed by the reviewed AICode worker policy")

    @staticmethod
    def _agent_runtime_result_verifier(
        node: TaskNode, output: WorkerOutput
    ) -> VerificationDecision:
        if not isinstance(output.summary, str) or not output.summary.strip():
            return VerificationDecision(False, "Worker output must include a non-empty summary")
        checks = output.tests_or_checks
        if not isinstance(checks, Mapping):
            return VerificationDecision(False, "Worker validation evidence must be an object")
        for key, evidence in checks.items():
            if str(key).startswith("check:") and (
                not isinstance(evidence, Mapping)
                or type(evidence.get("exit_code")) is not int
                or evidence.get("exit_code") != 0
            ):
                return VerificationDecision(False, f"Validation evidence failed or is malformed: {key}")
        required_checks = {
            "tester": ("check:pytest",),
            "security": ("check:bandit", "check:ruff", "check:compile"),
        }.get(node.selected_role_id, ())
        for key in required_checks:
            evidence = checks.get(key)
            if (
                not isinstance(evidence, Mapping)
                or type(evidence.get("exit_code")) is not int
                or evidence.get("exit_code") != 0
            ):
                return VerificationDecision(False, f"Required verification evidence is missing or failed: {key}")
        if node.selected_role_id == "developer" and not output.artifacts:
            return VerificationDecision(False, "Developer worker must return at least one changed artifact")
        return VerificationDecision(True, "Worker output and required evidence passed verification")

    def _agent_runtime_approval_authorizer(
        self, job: Job, plan: TaskPlan, dispatch: Mapping[str, Any]
    ) -> tuple[PolicyAuthorizer, dict[str, Any] | None]:
        """Bind an API approval to this exact plan fingerprint and its gated nodes."""
        result = job.result if isinstance(job.result, dict) else None
        approval = result.get("approval") if isinstance(result, dict) else None
        base_authorizer = dispatch["policy_authorizer"]
        if approval is None:
            return base_authorizer, None
        if not isinstance(approval, dict):
            raise ValueError("Agent Runtime approval record must be an object")
        fingerprint = WorkerExecutionEngine._fingerprint(plan)
        approved_ids = approval.get("approved_node_ids")
        plan_node_ids = {node.task_id for node in plan.nodes}
        if (
            approval.get("plan_fingerprint") != fingerprint
            or not isinstance(approved_ids, list)
            or not approved_ids
            or any(not isinstance(item, str) for item in approved_ids)
            or len(set(approved_ids)) != len(approved_ids)
            or not set(approved_ids).issubset(plan_node_ids)
            or not isinstance(approval.get("approved_by"), str)
            or not approval.get("approved_by", "").startswith("api:")
            or not isinstance(approval.get("rationale"), str)
            or not approval.get("rationale", "").strip()
            or not isinstance(approval.get("approved_at"), str)
            or not approval.get("approved_at")
        ):
            raise ValueError("Agent Runtime approval does not match the current plan or node set")
        approved_node_ids = frozenset(approved_ids)
        approved_by = approval["approved_by"]

        async def authorize(node: TaskNode, tenant_id: str) -> AgentPolicyDecision:
            decision = base_authorizer(node, tenant_id)
            if inspect.isawaitable(decision):
                decision = await decision
            if node.task_id in approved_node_ids and (
                decision.requires_approval or (node.approval_required and decision.allowed)
            ):
                return AgentPolicyDecision(
                    allowed=True,
                    requires_approval=True,
                    approval_satisfied=True,
                    reason=f"Human approval recorded by {approved_by}",
                )
            return decision

        return authorize, approval

    async def _build_agent_runtime_dispatch(
        self, job: Job, plan: TaskPlan, cancellation_event: asyncio.Event
    ) -> dict[str, Any]:
        if self.agent_runtime is None:
            self._load_agent_runtime_registry()
        if self.agent_runtime is None or not self.agent_runtime.status()["execution_enabled"]:
            raise RegistryNotActivatedError(
                "Agent Runtime execution is disabled until the registry is approved and activated"
            )
        self._validate_agent_runtime_quality_gates(plan)
        supported_roles = {role.value for role in AICodeOrchestrator.PIPELINE}
        unsupported = sorted({node.selected_role_id for node in plan.nodes} - supported_roles)
        if unsupported:
            raise RegistryNotActivatedError(
                "No reviewed execution adapter is available for roles: " + ", ".join(unsupported)
            )
        await self._assert_lease(job, cancellation_event)
        task = self._agent_runtime_coding_task(job, plan)
        orchestrator, workspace = await self._build_orchestrator(task, job.tenant_id)
        if cancellation_event.is_set():
            try:
                await asyncio.to_thread(self.workspace_manager.cleanup, task, job.tenant_id)
            except Exception:
                logger.exception("Could not clean workspace after Agent Runtime lease loss %s", task.task_id)
            raise LeaseLostError("Agent Runtime queue lease was lost while creating its workspace")
        handlers: dict[str, WorkerHandler] = {}
        for role_id in sorted({node.selected_role_id for node in plan.nodes}):
            role = WorkerRole(role_id)
            worker = orchestrator.workers.get(role)
            if worker is None:
                try:
                    await asyncio.to_thread(self.workspace_manager.cleanup, task, job.tenant_id)
                except Exception:
                    logger.exception("Could not clean unsupported Agent Runtime workspace %s", task.task_id)
                raise RegistryNotActivatedError(
                    f"No initialized AICode worker adapter exists for role {role_id!r}"
                )
            handlers[role_id] = _ScopedAICodeWorkerHandler(
                worker, task.repository, job.tenant_id, task.context
            )
        return {
            "handlers": handlers,
            "policy_authorizer": self._agent_runtime_policy_authorizer,
            "result_verifier": self._agent_runtime_result_verifier,
            "workspace": workspace,
            "coding_task": task,
            "publish_changes": any(node.selected_role_id == "developer" for node in plan.nodes),
        }

    async def _await_agent_runtime_approval(
        self,
        job: Job,
        reason: str,
        result: dict[str, Any] | None = None,
    ) -> str:
        updated = await asyncio.to_thread(
            self.queue.await_approval,
            job.id,
            job.tenant_id,
            self.worker_id,
            job.lease_token,
            reason,
            result,
        )
        if not updated:
            raise LeaseLostError(
                "Queue rejected Agent Runtime approval transition because the lease was lost"
            )
        await asyncio.to_thread(
            self.audit.record,
            "task.awaiting_approval",
            job.task_id,
            self.worker_id,
            {"reason": reason[:2000], "execution_mode": "agent_runtime_v1"},
            tenant_id=job.tenant_id,
        )
        return "awaiting_approval"

    async def _dispatch_agent_runtime_job(
        self,
        job: Job,
        cancellation_event: asyncio.Event,
    ) -> str:
        """Execute one explicitly typed DAG job and publish only verified output."""
        plan = self._agent_runtime_plan_from_job(job)
        self._validate_agent_runtime_quality_gates(plan)
        dispatch = getattr(self, "_agent_runtime_dispatch", None)
        if dispatch is None:
            try:
                dispatch = await self._build_agent_runtime_dispatch(job, plan, cancellation_event)
            except RegistryNotActivatedError as exc:
                return await self._await_agent_runtime_approval(
                    job,
                    str(exc),
                    {
                        "execution_mode": "agent_runtime_v1",
                        "plan_schema_version": "1.0",
                        "activation_blocked": True,
                    },
                )

        policy_authorizer, approval_record = self._agent_runtime_approval_authorizer(
            job, plan, dispatch
        )
        if approval_record is not None:
            await asyncio.to_thread(
                self.audit.record,
                "agent_runtime.approval_applied",
                job.task_id,
                self.worker_id,
                {
                    "plan_fingerprint": approval_record["plan_fingerprint"],
                    "approved_node_ids": approval_record["approved_node_ids"],
                    "approved_by": approval_record["approved_by"],
                    "rationale": approval_record.get("rationale", ""),
                },
                tenant_id=job.tenant_id,
            )
        try:
            result = await self.execute_agent_plan(
                job,
                plan,
                dispatch["handlers"],
                policy_authorizer,
                dispatch["result_verifier"],
                cancellation_event,
            )
        except RegistryNotActivatedError as exc:
            return await self._await_agent_runtime_approval(
                job,
                str(exc),
                {
                    "execution_mode": "agent_runtime_v1",
                    "plan_schema_version": "1.0",
                    "activation_blocked": True,
                },
            )

        coding_task = dispatch.get("coding_task")
        workspace = dispatch.get("workspace")
        publish_changes = dispatch.get("publish_changes", workspace is not None)
        result_payload = {
            "execution_mode": "agent_runtime_v1",
            "plan_status": result.status.value,
            "plan_fingerprint": result.plan_fingerprint,
            "topological_order": list(result.topological_order),
            "node_results": [item.to_dict() for item in result.node_results],
        }
        if coding_task is not None:
            result_payload["branch"] = coding_task.repository.work_branch

        if result.status.value in {"needs_approval", "blocked"}:
            relevant = [
                item.error or item.summary
                for item in result.node_results
                if item.status.value in {"needs_approval", "blocked"}
            ]
            reason = "; ".join(relevant) or (
                "Agent Runtime plan was blocked by policy or unmet dependencies"
            )
            return await self._await_agent_runtime_approval(job, reason, result_payload)
        if result.status.value == "cancelled":
            return "lease_lost"
        if result.status.value == "completed":
            if (workspace is None) != (coding_task is None):
                raise RuntimeError("Agent Runtime publication context is incomplete")
            if workspace is None and self.config.environment == "production":
                return await self._await_agent_runtime_approval(
                    job,
                    "Production Agent Runtime requires a repository workspace and publication adapter",
                    result_payload,
                )
            if workspace is not None and coding_task is not None and publish_changes:
                await self._assert_lease(job, cancellation_event)
                commit_sha = await asyncio.to_thread(
                    self.queue.publish_if_lease_owned,
                    job.id,
                    job.tenant_id,
                    self.worker_id,
                    job.lease_token,
                    lambda: self.publisher.commit_and_push(
                        workspace,
                        coding_task.repository.full_name,
                        coding_task.repository.work_branch,
                        f"feat(agent-runtime): {coding_task.objective[:120]}",
                    ),
                    lease_seconds=900,
                )
                await self._assert_lease(job, cancellation_event)
                pr_url = await self._await_with_lease(
                    self.publisher.create_or_get_draft_pr(
                        coding_task.repository.full_name,
                        coding_task.repository.work_branch,
                        coding_task.repository.base_branch,
                        f"Agent Runtime: {coding_task.objective[:180]}",
                        (
                            "## Agent Runtime implementation\n\n"
                            f"Task ID: {job.task_id}\n\n"
                            f"Plan fingerprint: {result.plan_fingerprint}\n\n"
                            f"Workspace commit: {commit_sha}\n\n"
                            "Acceptance criteria:\n"
                            + "\n".join(f"- {item}" for item in coding_task.acceptance_criteria)
                            + "\n\nAutomated checks were independently verified. This pull request is draft "
                            "and requires human review before merge."
                        ),
                    ),
                    cancellation_event,
                )
                await asyncio.to_thread(
                    self.audit.record,
                    "pull_request.created",
                    job.task_id,
                    self.worker_id,
                    {"pull_request_url": pr_url, "commit_sha": commit_sha, "execution_mode": "agent_runtime_v1"},
                    tenant_id=job.tenant_id,
                )
                try:
                    ci_timeout = int(os.getenv("RUSA_CI_TIMEOUT_SECONDS", "900"))
                    ci_poll = int(os.getenv("RUSA_CI_POLL_SECONDS", "15"))
                except ValueError as exc:
                    raise ValueError("CI timeout and poll interval must be integers") from exc
                if not 60 <= ci_timeout <= 3600 or not 5 <= ci_poll <= 60:
                    raise ValueError("CI timeout must be 60-3600s and poll interval 5-60s")
                ci_ok, ci_reason = await self._await_with_lease(
                    self.publisher.wait_for_ci(
                        coding_task.repository.full_name,
                        commit_sha,
                        timeout_seconds=ci_timeout,
                        poll_seconds=ci_poll,
                    ),
                    cancellation_event,
                )
                result_payload.update({
                    "commit_sha": commit_sha,
                    "pull_request_url": pr_url,
                })
                if not ci_ok:
                    return await self._await_agent_runtime_approval(
                        job, ci_reason, result_payload
                    )
                await asyncio.to_thread(
                    self.audit.record,
                    "ci.verified",
                    job.task_id,
                    self.worker_id,
                    {
                        "commit_sha": commit_sha,
                        "pull_request_url": pr_url,
                        "result": ci_reason,
                        "execution_mode": "agent_runtime_v1",
                    },
                    tenant_id=job.tenant_id,
                )

            completed = await asyncio.to_thread(
                self.queue.complete,
                job.id,
                job.tenant_id,
                self.worker_id,
                job.lease_token,
                result_payload,
            )
            if not completed:
                raise LeaseLostError(
                    "Queue rejected Agent Runtime completion because the lease was lost"
                )
            await asyncio.to_thread(
                self.audit.record,
                "agent_runtime.completed",
                job.task_id,
                self.worker_id,
                {
                    "plan_fingerprint": result.plan_fingerprint,
                    "node_count": len(result.node_results),
                    "pull_request_url": result_payload.get("pull_request_url"),
                },
                tenant_id=job.tenant_id,
            )
            if workspace is not None and coding_task is not None:
                try:
                    await asyncio.to_thread(
                        self.workspace_manager.cleanup, coding_task, job.tenant_id
                    )
                except Exception:
                    logger.exception("Could not clean successful Agent Runtime workspace %s", job.task_id)
            return "success"

        errors = [item.error for item in result.node_results if item.error]
        reason = "; ".join(errors) or f"Agent Runtime plan ended in {result.status.value}"
        failed = await asyncio.to_thread(
            self.queue.fail,
            job.id,
            job.tenant_id,
            self.worker_id,
            job.lease_token,
            reason,
        )
        if not failed:
            raise LeaseLostError(
                "Queue rejected Agent Runtime failure transition because the lease was lost"
            )
        await asyncio.to_thread(
            self.audit.record,
            "agent_runtime.failed",
            job.task_id,
            self.worker_id,
            {"plan_status": result.status.value, "reason": reason[:2000]},
            tenant_id=job.tenant_id,
        )
        return "failed"

    def build_agent_execution_engine(
        self,
        job: Job,
        policy_authorizer: PolicyAuthorizer,
        result_verifier: ResultVerifier,
    ) -> WorkerExecutionEngine:
        """Compose the staged DAG engine with tenant memory and the live queue lease."""
        if job.lease_owner != self.worker_id:
            raise PermissionError("Agent Runtime job lease belongs to a different worker")
        if not job.tenant_id or not job.lease_token:
            raise ValueError("Agent Runtime job requires a tenant and lease token")
        if self.agent_runtime is None:
            self._load_agent_runtime_registry()
        if self.agent_runtime is None:
            raise RuntimeError("Agent Runtime registry failed to initialize")

        checkpoint_store = LeaseBoundCheckpointStore(
            TaskMemoryCheckpointStore(self.memory),
            self.queue,
            job.id,
            job.task_id,
            job.tenant_id,
            self.worker_id,
            job.lease_token,
        )
        return self.agent_runtime.build_execution_engine(
            checkpoint_store,
            policy_authorizer,
            result_verifier,
            worker_parallelism=1,
        )

    async def execute_agent_plan(
        self,
        job: Job,
        plan: TaskPlan,
        handlers: Mapping[str, WorkerHandler],
        policy_authorizer: PolicyAuthorizer,
        result_verifier: ResultVerifier,
        cancellation_event: asyncio.Event,
    ) -> TaskExecutionResult:
        """Run one validated DAG under the caller's live queue lease signal.

        The caller remains responsible for queue completion/failure. This
        method only executes the plan and never bypasses the registry,
        policy, verifier, tenant, or lease gates.
        """
        if plan.root_task_id != job.task_id:
            raise PermissionError("Agent Runtime plan root does not match the claimed queue task")
        if plan.tenant_id != job.tenant_id:
            raise PermissionError("Agent Runtime plan tenant does not match the claimed queue tenant")
        await self._assert_lease(job, cancellation_event)
        engine = self.build_agent_execution_engine(job, policy_authorizer, result_verifier)
        result = await engine.execute(
            plan,
            handlers,
            trusted_tenant_id=job.tenant_id,
            cancellation_event=cancellation_event,
        )
        if cancellation_event.is_set():
            if result.status.value != "cancelled":
                raise LeaseLostError(
                    "Queue lease was revoked before DAG completion could be accepted"
                )
        else:
            await self._assert_lease(job, cancellation_event)
        return result

    def initialize(self) -> None:
        self._load_agent_runtime_registry()
        self.queue.initialize()
        self.memory.initialize()
        self.audit.initialize()
        if self.config.environment == "production":
            self.task_policy_audit.verify_ready("rusa_worker_runtime")
        self.sandbox.verify_ready(
            require_rootless=self.config.environment == "production",
            require_image=self.config.environment == "production",
        )
        if not self.audit.verify():
            raise RuntimeError("Central audit ledger integrity verification failed")

    @staticmethod
    def _task(job: Job) -> CodingTask:
        data: dict[str, Any] = job.payload
        repository = data["repository"]
        lease_suffix = job.lease_token.replace("-", "")[:8]
        return CodingTask(
            objective=data["objective"],
            repository=RepositoryTarget(
                full_name=repository["full_name"],
                base_branch=repository.get("base_branch", "main"),
                work_branch=f"feature/aicode/{job.task_id[:12]}-{lease_suffix}",
            ),
            acceptance_criteria=data["acceptance_criteria"],
            context=data.get("context", ""),
            task_id=job.task_id,
            metadata={"tenant_id": job.tenant_id, "lease_token": job.lease_token},
        )

    @staticmethod
    def _knowledge_project_scope(repository_full_name: str) -> str:
        """Derive a stable project scope from the already-authorized repository target."""
        if not isinstance(repository_full_name, str) or repository_full_name.count("/") != 1:
            raise ValueError("repository target cannot be mapped to a knowledge project scope")
        project_id = repository_full_name.lower().replace("/", "--")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", project_id):
            raise ValueError("repository target contains unsupported project-scope characters")
        return project_id

    async def _build_orchestrator(
        self, task: CodingTask, tenant_id: str
    ) -> tuple[AICodeOrchestrator, str]:
        tenant_provider_factory = getattr(self.provider, "for_tenant", None)
        tenant_provider = (
            tenant_provider_factory(tenant_id)
            if callable(tenant_provider_factory)
            else self.provider
        )
        workspace = await asyncio.to_thread(
            self.workspace_manager.create, task, tenant_id
        )
        gateway = WorkspaceToolGateway(
            workspace,
            sandbox=self.sandbox,
            knowledge_service=self.knowledge_service,
            knowledge_tenant_id=tenant_id if self.knowledge_service is not None else None,
            knowledge_collection=os.getenv("RUSA_KNOWLEDGE_COLLECTION", "agent"),
            knowledge_project_id=(
                self._knowledge_project_scope(task.repository.full_name)
                if self.knowledge_service is not None else None
            ),
        )
        workers = [
            AutonomousCodeWorker(role, tenant_provider, gateway)
            for role in AICodeOrchestrator.PIPELINE
        ]
        return (
            AICodeOrchestrator(
                workers=workers,
                memory=self.memory,
                audit=self.audit,
                max_parallel_workers=int(os.getenv("RUSA_MAX_PARALLEL_WORKERS", "4")),
                max_repair_cycles=int(os.getenv("RUSA_MAX_REPAIR_CYCLES", "2")),
            ),
            str(workspace),
        )

    async def _await_with_lease(
        self, operation: Coroutine, lost_lease: asyncio.Event
    ) -> Any:
        operation_task = asyncio.create_task(operation)
        lease_watch = asyncio.create_task(lost_lease.wait())
        try:
            done, _ = await asyncio.wait(
                {operation_task, lease_watch},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lease_watch in done and lost_lease.is_set():
                operation_task.cancel()
                try:
                    await operation_task
                except asyncio.CancelledError:
                    pass
                raise RuntimeError("Worker lost its task lease; operation cancelled")
            return await operation_task
        finally:
            lease_watch.cancel()
            try:
                await lease_watch
            except asyncio.CancelledError:
                pass

    async def _heartbeat(self, job: Job, lost_lease: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                renewed = await asyncio.to_thread(
                    self.queue.heartbeat, job.id, job.tenant_id, self.worker_id, job.lease_token, 180
                )
            except Exception:
                logger.exception("Lease heartbeat failed for job %s", job.id)
                lost_lease.set()
                return
            if not renewed:
                lost_lease.set()
                logger.error("Lease lost for job %s", job.id)
                return

    async def _assert_lease(self, job: Job, lost_lease: asyncio.Event) -> None:
        if lost_lease.is_set():
            raise RuntimeError("Worker has already lost its task lease")
        owned = await asyncio.to_thread(
            self.queue.owns_lease,
            job.id,
            job.tenant_id,
            self.worker_id,
            job.lease_token,
        )
        if not owned:
            lost_lease.set()
            raise RuntimeError("Task lease is no longer owned by this worker")

    async def _await_human_review(
        self,
        job: Job,
        task: CodingTask,
        reason: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        updated = await asyncio.to_thread(
            self.queue.await_approval,
            job.id,
            job.tenant_id,
            self.worker_id,
            job.lease_token,
            reason,
            result,
        )
        if not updated:
            raise RuntimeError("Queue rejected awaiting-approval transition because the lease was lost")
        await asyncio.to_thread(
            self.audit.record,
            "task.awaiting_approval",
            task.task_id,
            self.worker_id,
            {"reason": reason[:2000]},
            tenant_id=job.tenant_id,
        )

    async def run_once(self) -> bool:
        if not self.config.aicode_enabled:
            return False
        job = await asyncio.to_thread(self.queue.claim, self.worker_id, 180)
        if job is None:
            return False
        started = time.perf_counter()
        outcome = "failed"
        lost_lease = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(job, lost_lease))
        authorized = False
        policy_audited = False
        try:
            execution_mode = job.payload.get("execution_mode")
            if execution_mode == "agent_runtime_v1":
                await self._assert_lease(job, lost_lease)
                authorized = await asyncio.to_thread(
                    self.queue.authorize_execution,
                    job.id,
                    job.task_id,
                    job.tenant_id,
                    self.worker_id,
                    job.lease_token,
                )
                policy_audited = True
                record_task_policy(
                    job.tenant_id,
                    job.task_id,
                    f"worker:{self.worker_id}",
                    "task.execute",
                    authorized,
                )
                if not authorized:
                    lost_lease.set()
                    raise RuntimeError("Agent Runtime worker is not authorized by the current task lease")
                outcome = await self._dispatch_agent_runtime_job(job, lost_lease)
                return True
            if execution_mode not in {None, "aicode"}:
                raise ValueError(f"Unsupported queue execution_mode: {execution_mode!r}")

            task = self._task(job)
            allowed_repositories = self.config.tenant_repositories.get(job.tenant_id)
            if allowed_repositories is not None and task.repository.full_name not in allowed_repositories:
                raise PermissionError("Tenant is not authorized for this repository")
            if self.config.environment == "production" and not allowed_repositories:
                raise PermissionError("Tenant has no configured repository permissions")

            await self._assert_lease(job, lost_lease)
            authorized = await asyncio.to_thread(
                self.queue.authorize_execution,
                job.id,
                task.task_id,
                job.tenant_id,
                self.worker_id,
                job.lease_token,
            )
            policy_audited = True
            record_task_policy(
                job.tenant_id,
                job.task_id,
                f"worker:{self.worker_id}",
                "task.execute",
                authorized,
            )
            if not authorized:
                lost_lease.set()
                raise RuntimeError("Worker is not authorized by the current task lease")
            orchestrator, workspace = await self._build_orchestrator(task, job.tenant_id)
            if lost_lease.is_set():
                raise RuntimeError("Worker lost its task lease before execution")
            task_timeout = int(os.getenv("RUSA_TASK_TIMEOUT_SECONDS", "3600"))
            if not 60 <= task_timeout <= 86400:
                raise ValueError("RUSA_TASK_TIMEOUT_SECONDS must be between 60 and 86400")
            record = await self._await_with_lease(
                asyncio.wait_for(orchestrator.run(task), timeout=task_timeout),
                lost_lease,
            )
            if record.state == TaskState.AWAITING_APPROVAL:
                await self._await_human_review(
                    job,
                    task,
                    "; ".join(record.errors) or "Human intervention required",
                    {"branch": task.repository.work_branch},
                )
                outcome = "awaiting_approval"
                return True
            if record.state != TaskState.SUCCEEDED:
                await asyncio.to_thread(
                    self.queue.fail, job.id, job.tenant_id, self.worker_id, job.lease_token,
                    "; ".join(record.errors) or f"Task ended in {record.state.value}",
                )
                outcome = "failed"
                return True

            await self._assert_lease(job, lost_lease)
            commit_sha = await asyncio.to_thread(
                self.queue.publish_if_lease_owned,
                job.id,
                job.tenant_id,
                self.worker_id,
                job.lease_token,
                lambda: self.publisher.commit_and_push(
                    workspace,
                    task.repository.full_name,
                    task.repository.work_branch,
                    f"feat(aicode): {task.objective[:120]}",
                ),
                lease_seconds=900,
            )
            await self._assert_lease(job, lost_lease)
            pr_url = await self._await_with_lease(
                self.publisher.create_or_get_draft_pr(
                    task.repository.full_name,
                    task.repository.work_branch,
                    task.repository.base_branch,
                    f"AICode: {task.objective[:180]}",
                    (
                        "## Autonomous implementation\n\n"
                        f"Task ID: {task.task_id}\n\n"
                        f"Workspace commit: {commit_sha}\n\n"
                        "Acceptance criteria:\n" +
                        "\n".join(f"- {item}" for item in task.acceptance_criteria) +
                        "\n\nAutomated test and security evidence is recorded in the task audit trail. "
                        "This pull request is draft and requires human review before merge."
                    ),
                ),
                lost_lease,
            )
            await asyncio.to_thread(
                self.audit.record,
                "pull_request.created",
                task.task_id,
                self.worker_id,
                {"pull_request_url": pr_url, "commit_sha": commit_sha},
                tenant_id=job.tenant_id,
            )
            ci_timeout = int(os.getenv("RUSA_CI_TIMEOUT_SECONDS", "900"))
            ci_poll = int(os.getenv("RUSA_CI_POLL_SECONDS", "15"))
            if not 60 <= ci_timeout <= 3600 or not 5 <= ci_poll <= 60:
                raise ValueError("CI timeout must be 60-3600s and poll interval 5-60s")
            ci_ok, ci_reason = await self._await_with_lease(
                self.publisher.wait_for_ci(
                    task.repository.full_name, commit_sha,
                    timeout_seconds=ci_timeout, poll_seconds=ci_poll,
                ),
                lost_lease,
            )
            if not ci_ok:
                await self._await_human_review(
                    job,
                    task,
                    ci_reason,
                    {
                        "pull_request_url": pr_url,
                        "commit_sha": commit_sha,
                        "branch": task.repository.work_branch,
                    },
                )
                outcome = "awaiting_approval"
                return True

            await asyncio.to_thread(
                self.audit.record,
                "ci.verified",
                task.task_id,
                self.worker_id,
                {"commit_sha": commit_sha, "pull_request_url": pr_url, "result": ci_reason},
                tenant_id=job.tenant_id,
            )
            completed = await asyncio.to_thread(
                self.queue.complete,
                job.id,
                job.tenant_id,
                self.worker_id,
                job.lease_token,
                {
                    "pull_request_url": pr_url,
                    "commit_sha": commit_sha,
                    "branch": task.repository.work_branch,
                },
            )
            if not completed:
                raise RuntimeError("Queue rejected completion because the lease is no longer owned")
            try:
                await asyncio.to_thread(self.workspace_manager.cleanup, task, job.tenant_id)
            except Exception:
                logger.exception("Could not clean successful task workspace %s", task.task_id)
            logger.info("AICode task %s completed: %s", task.task_id, pr_url)
            outcome = "success"
        except Exception as exc:
            if isinstance(exc, LeaseLostError):
                lost_lease.set()
            if not policy_audited:
                try:
                    record_task_policy(
                        job.tenant_id,
                        job.task_id,
                        f"worker:{self.worker_id}",
                        "task.execute",
                        False,
                        sink=self.task_policy_audit,
                        required=self.config.environment == "production",
                    )
                except Exception:
                    logger.error("Could not persist denied task execution audit event")
            elif not authorized:
                record_task_policy(
                    job.tenant_id, job.task_id, f"worker:{self.worker_id}",
                    "task.execute", False,
                )
            logger.exception("AICode task %s failed", job.task_id)
            outcome = "lease_lost" if lost_lease.is_set() else "failed"
            if not lost_lease.is_set():
                await asyncio.to_thread(
                    self.queue.fail, job.id, job.tenant_id, self.worker_id, job.lease_token,
                    f"{type(exc).__name__}: {exc}",
                )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            AICODE_TASKS.labels(outcome).inc()
            AICODE_TASK_DURATION.labels(outcome).observe(time.perf_counter() - started)
        return True

    async def run_forever(
        self, poll_seconds: float = 2.0, stop_event: asyncio.Event | None = None
    ) -> None:
        stop_event = stop_event or asyncio.Event()
        while not stop_event.is_set():
            processed = await self.run_once()
            if not processed:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
                except asyncio.TimeoutError:
                    pass
