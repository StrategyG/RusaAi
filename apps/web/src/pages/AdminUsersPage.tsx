import { useCallback, useEffect, useState, type FormEvent } from "react";
import { AlertCircle, Plus, RefreshCw, Shield, UserRound, UserX } from "lucide-react";
import { api, type User } from "../api";

type UserList = { items: User[]; counts: { total: number; active: number; admins: number } };

export function AdminUsersPage() {
  const [data, setData] = useState<UserList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [saving, setSaving] = useState(false);
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<"user" | "admin">("user");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await api<UserList>("/v1/admin/users?limit=100"));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load users");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function createUser(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError("");
    try {
      await api("/v1/admin/users", {
        method: "POST",
        body: JSON.stringify({ email, display_name: name, password, role }),
      });
      setEmail("");
      setName("");
      setPassword("");
      setRole("user");
      setShowForm(false);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "User could not be created");
    } finally {
      setSaving(false);
    }
  }

  async function updateUser(
    user: User,
    patch: { role?: "user" | "admin"; is_active?: boolean },
  ) {
    try {
      await api("/v1/admin/users/" + user.id, {
        method: "PATCH",
        body: JSON.stringify(patch),
      });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "User could not be updated");
    }
  }

  return (
    <div className="page-stack">
      <div className="page-heading">
        <div><div className="eyebrow muted">ACCESS CONTROL</div><h1>User management</h1><p>Create accounts and manage access within this workspace tenant.</p></div>
        <div className="heading-actions">
          <button className="secondary-button" onClick={() => void load()}><RefreshCw size={15} /> Refresh</button>
          <button className="primary-button" onClick={() => setShowForm(!showForm)}><Plus size={16} /> Add user</button>
        </div>
      </div>
      {error && <div className="error-banner"><AlertCircle size={17} /> {error}</div>}
      {showForm && (
        <section className="panel-card form-panel">
          <div className="section-heading"><div><h2>Create workspace account</h2><p>Share the temporary password securely with the new user.</p></div></div>
          <form className="form-grid user-create-form" onSubmit={(event) => void createUser(event)}>
            <label>Full name<input required maxLength={120} value={name} onChange={(e) => setName(e.target.value)} /></label>
            <label>Email<input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} /></label>
            <label>Temporary password<input type="password" minLength={12} required value={password} onChange={(e) => setPassword(e.target.value)} /></label>
            <label>Role<select value={role} onChange={(e) => setRole(e.target.value as "user" | "admin")}><option value="user">User</option><option value="admin">Administrator</option></select></label>
            <div className="form-actions">
              <button type="button" className="secondary-button" onClick={() => setShowForm(false)}>Cancel</button>
              <button className="primary-button" disabled={saving}>{saving ? "Creating…" : "Create account"}</button>
            </div>
          </form>
        </section>
      )}
      <div className="metric-grid compact-grid">
        <div className="metric-card"><div className="metric-icon"><UserRound size={18} /></div><div className="metric-label">Total users</div><div className="metric-value">{data?.counts.total ?? "—"}</div></div>
        <div className="metric-card"><div className="metric-icon green"><Shield size={18} /></div><div className="metric-label">Active accounts</div><div className="metric-value">{data?.counts.active ?? "—"}</div></div>
        <div className="metric-card"><div className="metric-icon purple"><Shield size={18} /></div><div className="metric-label">Administrators</div><div className="metric-value">{data?.counts.admins ?? "—"}</div></div>
      </div>
      <div className="panel-card">
        {loading ? <div className="loading-line">Loading users…</div> : data?.items.length ? (
          <div className="table-wrap">
            <table>
              <thead><tr><th>User</th><th>Role</th><th>Status</th><th>Last sign in</th><th>Actions</th></tr></thead>
              <tbody>
                {data.items.map((user) => (
                  <tr key={user.id}>
                    <td><div className="user-cell"><div className="avatar small-avatar">{user.display_name.slice(0, 1).toUpperCase()}</div><div><strong>{user.display_name}</strong><small>{user.email}</small></div></div></td>
                    <td><select className="inline-select" value={user.role} onChange={(e) => void updateUser(user, { role: e.target.value as "user" | "admin" })}><option value="user">User</option><option value="admin">Admin</option></select></td>
                    <td><span className={"state-pill " + (user.is_active ? "state-succeeded" : "state-failed")}>{user.is_active ? "Active" : "Disabled"}</span></td>
                    <td>{user.last_login_at ? new Date(user.last_login_at).toLocaleString() : "Never"}</td>
                    <td><button className={user.is_active ? "text-danger-button" : "text-action-button"} onClick={() => void updateUser(user, { is_active: !user.is_active })}>{user.is_active ? <><UserX size={14} /> Disable</> : "Enable"}</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className="empty-state"><strong>No users found</strong><p>Create the first account for this tenant.</p></div>}
      </div>
      <div className="notice-card"><Shield size={18} /><div><strong>Role-based access is enforced by the API</strong><p>Changing a role or disabling an account takes effect on the next authenticated request. The last active administrator cannot be demoted or disabled.</p></div></div>
    </div>
  );
}
