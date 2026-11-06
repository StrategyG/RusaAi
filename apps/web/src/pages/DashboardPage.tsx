import { useEffect, useState } from "react";
import { Activity, ArrowUpRight, Bot, CheckCircle2, CircleAlert, Cpu, ListTodo, RefreshCw, Users, type LucideIcon } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";

type Task = {
  task_id: string;
  state: string;
  objective: string;
  repository?: string;
  created_at: string;
  updated_at: string;
};
type Overview = {
  tenant_id: string;
  model: { name: string; loaded: boolean };
  agent: { enabled: boolean; queue_ready: boolean; stats: null | { counts: Record<string, number>; oldest_queued_age_seconds: number } };
  users?: { total: number; active: number; admins: number };
  recent_tasks?: Task[];
};

function MetricCard({ label, value, detail, icon: Icon, tone = "" }: { label: string; value: string | number; detail: string; icon: LucideIcon; tone?: string }) {
  return <div className="metric-card"><div className={"metric-icon " + tone}><Icon size={19} /></div><div className="metric-label">{label}</div><div className="metric-value">{value}</div><div className="metric-detail">{detail}</div></div>;
}

function StatePill({ state }: { state: string }) {
  return <span className={"state-pill state-" + state.toLowerCase()}>{state.replaceAll("_", " ")}</span>;
}

export function DashboardPage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let active = true;
    setLoading(true);
    api<Overview>(isAdmin ? "/v1/admin/overview" : "/v1/ui/overview")
      .then((result) => { if (active) { setData(result); setError(""); } })
      .catch((err) => { if (active) setError(err instanceof Error ? err.message : "Unable to load workspace"); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [isAdmin, reload]);

  const counts = data?.agent.stats?.counts || {};
  const totalTasks = Object.values(counts).reduce((sum, value) => sum + value, 0);
  const tasks = data?.recent_tasks || [];

  return (
    <div className="page-stack">
      <div className="page-heading">
        <div><div className="eyebrow muted">{isAdmin ? "ADMINISTRATION" : "YOUR WORKSPACE"}</div><h1>{isAdmin ? "Control center" : "Good to see you, " + (user?.display_name?.split(" ")[0] || "there")}</h1><p>{isAdmin ? "Monitor your tenant, users, and agent operations." : "Your AI workspace and task activity at a glance."}</p></div>
        <button className="secondary-button" onClick={() => setReload((v) => v + 1)}><RefreshCw size={15} /> Refresh</button>
      </div>
      {error && <div className="error-banner"><CircleAlert size={17} /> {error}</div>}
      {loading && <div className="loading-line">Loading live workspace data…</div>}
      <div className="metric-grid">
        <MetricCard label="Model status" value={data?.model.loaded ? "Online" : "Not ready"} detail={data?.model.name || "Model configuration"} icon={Cpu} tone={data?.model.loaded ? "green" : "amber"} />
        <MetricCard label="Agent runtime" value={data?.agent.enabled ? "Enabled" : "Disabled"} detail={data?.agent.queue_ready ? "Queue connected" : "Queue unavailable"} icon={Bot} tone={data?.agent.enabled && data?.agent.queue_ready ? "green" : "amber"} />
        <MetricCard label={isAdmin ? "Workspace users" : "My tasks"} value={isAdmin ? (data?.users?.total ?? "—") : totalTasks} detail={isAdmin ? (data?.users?.active ?? 0) + " active accounts" : (counts.running || 0) + " currently running"} icon={isAdmin ? Users : ListTodo} />
        <MetricCard label="Completed tasks" value={counts.succeeded ?? "—"} detail={(counts.failed || 0) + " failed · " + (counts.queued || 0) + " queued"} icon={CheckCircle2} tone="green" />
      </div>
      {!data?.model.loaded && <div className="notice-card warning"><CircleAlert size={18} /><div><strong>Inference model is not loaded</strong><p>The workspace is connected to the API, but chat requests will remain unavailable until a valid local model is configured and loaded.</p></div></div>}
      {!data?.agent.enabled && <div className="notice-card"><Activity size={18} /><div><strong>Agent task processing is disabled</strong><p>Enable the agent runtime and start a worker to accept and execute tasks. The dashboard does not display simulated activity.</p></div></div>}
      <div className="section-heading"><div><h2>{isAdmin ? "Operations snapshot" : "Recent activity"}</h2><p>{isAdmin ? "Current task states for this workspace tenant." : "The latest work submitted from your account."}</p></div><Link to={isAdmin ? "/admin/tasks" : "/tasks"} className="text-link">View all <ArrowUpRight size={15} /></Link></div>
      <div className="panel-card">
        {tasks.length ? <div className="table-wrap"><table><thead><tr><th>Task</th><th>Repository</th><th>Status</th><th>Created</th></tr></thead><tbody>{tasks.map((task) => <tr key={task.task_id}><td><strong className="task-title">{task.objective || task.task_id}</strong><small className="sub-id">{task.task_id.slice(0, 8)}</small></td><td>{task.repository || "—"}</td><td><StatePill state={task.state} /></td><td>{new Date(task.created_at).toLocaleString()}</td></tr>)}</tbody></table></div> : <div className="empty-state"><div className="empty-icon"><Bot size={24} /></div><strong>No task activity yet</strong><p>When tasks are submitted and processed, they will appear here.</p>{!isAdmin && <Link to="/tasks" className="primary-button">Create your first task</Link>}</div>}
      </div>
      {isAdmin && <div className="admin-shortcuts"><Link to="/admin/users"><Users size={18} /><span><strong>Manage users</strong><small>Create accounts and control access</small></span><ArrowUpRight size={16} /></Link><Link to="/admin/tasks"><ListTodo size={18} /><span><strong>Task operations</strong><small>Review, cancel, and retry dead-letter tasks</small></span><ArrowUpRight size={16} /></Link></div>}
    </div>
  );
}
