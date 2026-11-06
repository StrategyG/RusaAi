import { useCallback, useEffect, useState, type FormEvent } from "react";
import { AlertCircle, ArrowRight, Plus, RefreshCw, X } from "lucide-react";
import { api } from "../api";

type Task = { task_id: string; state: string; objective: string; repository?: string; attempts: number; max_attempts: number; created_at: string; last_error?: string | null };

function StatePill({ state }: { state: string }) {
  return <span className={"state-pill state-" + state.toLowerCase()}>{state.replaceAll("_", " ")}</span>;
}

export function TasksPage() {
  const [items, setItems] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [saving, setSaving] = useState(false);
  const [objective, setObjective] = useState("");
  const [context, setContext] = useState("");
  const [repository, setRepository] = useState("");
  const [baseBranch, setBaseBranch] = useState("main");
  const [criteria, setCriteria] = useState("The requested work is implemented and tests pass.");
  const [agentReady, setAgentReady] = useState<boolean | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await api<{ items: Task[] }>("/v1/agent/tasks?limit=50");
      setItems(result.items || []);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load tasks");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    let active = true;
    api<{ agent: { enabled: boolean; queue_ready: boolean } }>("/v1/ui/overview")
      .then((overview) => {
        if (active) setAgentReady(Boolean(overview.agent?.enabled && overview.agent?.queue_ready));
      })
      .catch(() => {
        if (active) setAgentReady(false);
      });
    return () => { active = false; };
  }, []);

  async function createTask(event: FormEvent) {
    event.preventDefault();
    if (agentReady !== true) {
      setError("Agent processing is not ready. Ask an administrator to enable the agent runtime and worker.");
      return;
    }
    setSaving(true);
    setError("");
    const taskId = crypto.randomUUID();
    const envelope = {
      protocol_version: "1.0",
      task_id: taskId,
      message_id: crypto.randomUUID(),
      type: "task.create",
      from: { agent_id: "workspace-user", role: "human" },
      to: { agent_id: "rusa-main", role: "rusa_orchestrator" },
      created_at: new Date().toISOString(),
      task: {
        objective: objective.trim(),
        context: context.trim(),
        acceptance_criteria: criteria.split("\n").map((line) => line.trim()).filter(Boolean),
        repository: repository.trim(),
        base_branch: baseBranch.trim(),
        work_branch: "feature/rusa-" + taskId.slice(0, 8),
      },
      policy: {
        allowed_actions: ["read_repository", "write_branch", "run_tests", "create_pull_request"],
        prohibited_actions: ["access_secrets", "merge", "deploy"],
        human_approval_required: ["merge", "deploy", "release", "secret_change", "permission_change", "destructive_action"],
      },
    };
    try {
      await api("/v1/agent/tasks", {
        method: "POST",
        headers: { "Idempotency-Key": taskId },
        body: JSON.stringify({ envelope }),
      });
      setShowForm(false);
      setObjective("");
      setContext("");
      setRepository("");
      setCriteria("The requested work is implemented and tests pass.");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Task could not be submitted");
    } finally {
      setSaving(false);
    }
  }

  async function cancelTask(taskId: string) {
    if (!window.confirm("Cancel this task? The agent will stop processing it when cancellation is applied.")) return;
    try {
      await api("/v1/agent/tasks/" + taskId + "/cancel", { method: "POST" });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Task could not be cancelled");
    }
  }

  return (
    <div className="page-stack">
      <div className="page-heading"><div><div className="eyebrow muted">WORK MANAGEMENT</div><h1>My tasks</h1><p>Submit work to the RUSA-AI agent runtime and track its execution.</p></div><div className="heading-actions"><button className="secondary-button" onClick={() => void load()}><RefreshCw size={15} /> Refresh</button><button className="primary-button" onClick={() => setShowForm(true)} disabled={agentReady !== true} title={agentReady === false ? "Agent runtime or worker is unavailable" : "Create a new agent task"}><Plus size={16} /> New task</button></div></div>
      {error && <div className="error-banner"><AlertCircle size={17} /> {error}</div>}
      {agentReady === false && <div className="notice-card warning"><AlertCircle size={18} /><div><strong>Agent processing is unavailable</strong><p>Task submission is paused because the agent runtime or queue is not ready. Ask an administrator to enable the worker, then refresh this page.</p></div></div>}
      {agentReady === null && <div className="loading-line">Checking agent runtime…</div>}
      {showForm && <div className="modal-backdrop" role="presentation" onMouseDown={(e) => { if (e.target === e.currentTarget) setShowForm(false); }}><section className="modal-card"><div className="modal-heading"><div><div className="eyebrow muted">NEW AGENT TASK</div><h2>Describe the work</h2></div><button className="icon-button" onClick={() => setShowForm(false)} aria-label="Close"><X size={19} /></button></div><form onSubmit={(event) => void createTask(event)} className="form-stack"><label>Objective<textarea required minLength={1} maxLength={8000} rows={3} value={objective} onChange={(e) => setObjective(e.target.value)} placeholder="What should the agent accomplish?" /></label><div className="form-grid"><label>GitHub repository<input required pattern="[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+" value={repository} onChange={(e) => setRepository(e.target.value)} placeholder="owner/repository" /></label><label>Base branch<input required value={baseBranch} onChange={(e) => setBaseBranch(e.target.value)} /></label></div><label>Additional context<textarea rows={3} maxLength={20000} value={context} onChange={(e) => setContext(e.target.value)} placeholder="Relevant files, constraints, or background…" /></label><label>Acceptance criteria<textarea required rows={3} value={criteria} onChange={(e) => setCriteria(e.target.value)} placeholder="One criterion per line" /></label><div className="modal-note">Merge, deployment, secret changes, and destructive actions remain subject to human approval.</div><div className="modal-actions"><button type="button" className="secondary-button" onClick={() => setShowForm(false)}>Cancel</button><button className="primary-button" disabled={saving || agentReady !== true || !objective.trim() || !repository.trim() || !criteria.trim()}>{saving ? "Submitting…" : <>Submit task <ArrowRight size={16} /></>}</button></div></form></section></div>}
      <div className="panel-card">
        {loading ? <div className="loading-line">Loading tasks…</div> : items.length ? <div className="task-list">{items.map((task) => <article className="task-row" key={task.task_id}><div className="task-row-main"><div className="task-row-title"><strong>{task.objective || "Agent task"}</strong><StatePill state={task.state} /></div><div className="task-meta"><span>{task.repository || "Repository not specified"}</span><span>Attempt {task.attempts}/{task.max_attempts}</span><span>{new Date(task.created_at).toLocaleString()}</span></div>{task.last_error && <div className="task-error">{task.last_error}</div>}<small className="sub-id">{task.task_id}</small></div>{["queued", "running"].includes(task.state) && <button className="text-danger-button" onClick={() => void cancelTask(task.task_id)}>Cancel</button>}</article>)}</div> : <div className="empty-state"><div className="empty-icon"><Plus size={23} /></div><strong>No tasks yet</strong><p>Create a task and RUSA-AI will add it to the agent queue.</p><button className="primary-button" onClick={() => setShowForm(true)} disabled={agentReady !== true}>Create a task</button></div>}
      </div>
    </div>
  );
}
