import { useCallback, useEffect, useState } from "react";
import { AlertCircle, RefreshCw } from "lucide-react";
import { api } from "../api";

type Task = {
  task_id: string;
  tenant_id: string;
  state: string;
  objective: string;
  repository?: string;
  attempts: number;
  max_attempts: number;
  created_at: string;
  last_error?: string | null;
};

export function AdminTasksPage() {
  const [items, setItems] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await api<{ items: Task[] }>("/v1/admin/tasks?limit=100");
      setItems(result.items || []);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load tasks");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function action(task: Task, operation: "retry" | "cancel") {
    try {
      await api("/v1/admin/tasks/" + task.task_id + "/" + operation, { method: "POST" });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Task " + operation + " failed");
    }
  }

  return (
    <div className="page-stack">
      <div className="page-heading">
        <div><div className="eyebrow muted">AGENT OPERATIONS</div><h1>Task operations</h1><p>Review all tasks in this tenant and handle stuck or failed work.</p></div>
        <button className="secondary-button" onClick={() => void load()}><RefreshCw size={15} /> Refresh</button>
      </div>
      {error && <div className="error-banner"><AlertCircle size={17} /> {error}</div>}
      <div className="panel-card">
        {loading ? <div className="loading-line">Loading task queue…</div> : items.length ? (
          <div className="task-list">
            {items.map((task) => (
              <article className="task-row" key={task.task_id}>
                <div className="task-row-main">
                  <div className="task-row-title"><strong>{task.objective || "Agent task"}</strong><span className={"state-pill state-" + task.state}>{task.state.replaceAll("_", " ")}</span></div>
                  <div className="task-meta"><span>{task.repository || "—"}</span><span>Tenant: {task.tenant_id}</span><span>Attempt {task.attempts}/{task.max_attempts}</span><span>{new Date(task.created_at).toLocaleString()}</span></div>
                  {task.last_error && <div className="task-error">{task.last_error}</div>}
                  <small className="sub-id">{task.task_id}</small>
                </div>
                <div className="task-actions">
                  {task.state === "dead_letter" && <button className="text-action-button" onClick={() => void action(task, "retry")}>Retry</button>}
                  {["queued", "running"].includes(task.state) && <button className="text-danger-button" onClick={() => void action(task, "cancel")}>Cancel</button>}
                </div>
              </article>
            ))}
          </div>
        ) : <div className="empty-state"><strong>No tasks in the queue</strong><p>Agent work submitted by users will appear here.</p></div>}
      </div>
      <div className="notice-card"><AlertCircle size={18} /><div><strong>Operational safety</strong><p>Retries are limited to dead-letter tasks. Merge, deployment, release, and destructive operations remain subject to human approval in the agent policy.</p></div></div>
    </div>
  );
}
