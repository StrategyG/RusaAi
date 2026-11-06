"""Hierarchical orchestrator with evidence-based completion."""
from dataclasses import asdict
from .audit import AuditLog
from .contracts import CodingTask,TaskRecord,TaskState,WorkerRole
from .workers import Worker
from .memory import SQLiteMemory
from .policy import AutonomyPolicy

class AICodeOrchestrator:
    PIPELINE=(WorkerRole.ANALYST,WorkerRole.ARCHITECT,WorkerRole.DEVELOPER,WorkerRole.DEBUGGER,WorkerRole.TESTER,WorkerRole.SECURITY)
    def __init__(self,workers,policy=None,memory=None,audit=None):
        self.workers={w.role:w for w in workers}; self.policy=policy or AutonomyPolicy()
        self.memory=memory or SQLiteMemory(); self.audit=audit or AuditLog()
        missing=set(self.PIPELINE)-set(self.workers)
        if missing: raise ValueError("missing required workers: "+", ".join(x.value for x in missing))
    def _persist(self,record):
        payload={"objective":record.task.objective,"acceptance_criteria":record.task.acceptance_criteria,
                 "results":[{"role":x.role.value,"summary":x.summary,"success":x.success,"artifacts":x.artifacts,"evidence":x.evidence,"blockers":x.blockers} for x in record.results],
                 "errors":record.errors}
        self.memory.save(record.task.task_id,record.task.repository.full_name,record.state.value,payload)
    async def run(self,task: CodingTask)->TaskRecord:
        record=TaskRecord(task=task)
        self.audit.record("task.received",task.task_id,"aicode-master",{"repository":task.repository.full_name})
        try:
            record.transition(TaskState.OBSERVING); self._persist(record)
            record.transition(TaskState.PLANNING); self._persist(record)
            if not self.policy.evaluate("repo_read").allowed:
                record.errors.append("Repository read denied by policy"); record.transition(TaskState.BLOCKED); return record
            record.transition(TaskState.EXECUTING); self._persist(record)
            for role in self.PIPELINE:
                action="workspace_write" if role==WorkerRole.DEVELOPER else "repo_read"
                decision=self.policy.evaluate(action)
                if not decision.allowed:
                    record.errors.append(decision.reason); record.transition(TaskState.BLOCKED); break
                result=await self.workers[role].run(task,list(record.results)); record.results.append(result)
                self.audit.record("worker.completed",task.task_id,role.value,{"success":result.success,"summary":result.summary[:500]})
                self._persist(record)
                if not result.success or result.blockers:
                    record.errors.extend(result.blockers or [role.value+" worker failed"])
                    record.transition(TaskState.BLOCKED); break
            if record.state==TaskState.EXECUTING:
                record.transition(TaskState.VERIFYING); self._persist(record)
                test=next((x for x in record.results if x.role==WorkerRole.TESTER),None)
                security=next((x for x in record.results if x.role==WorkerRole.SECURITY),None)
                if not test or not test.evidence or not security or not security.evidence:
                    record.errors.append("Missing test or security evidence; completion cannot be certified.")
                    record.transition(TaskState.AWAITING_APPROVAL)
                else: record.transition(TaskState.SUCCEEDED)
        except Exception as exc:
            record.errors.append(type(exc).__name__+": "+str(exc))
            if record.state not in {TaskState.FAILED,TaskState.BLOCKED,TaskState.CANCELLED}: record.transition(TaskState.FAILED)
            self.audit.record("task.failed",task.task_id,"aicode-master",{"error":str(exc)})
        finally:
            self._persist(record)
            self.audit.record("task.finished",task.task_id,"aicode-master",{"state":record.state.value,"errors":record.errors})
        return record
