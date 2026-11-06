import {
  ChevronDown,
  CircleHelp,
  ClipboardList,
  LayoutDashboard,
  LogOut,
  MessageSquareText,
  ShieldCheck,
  Users,
} from "lucide-react";
import {
  Navigate,
  NavLink,
  Outlet,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { useAuth } from "./auth";
import { LoginPage } from "./pages/LoginPage";
import { DashboardPage } from "./pages/DashboardPage";
import { ChatPage } from "./pages/ChatPage";
import { TasksPage } from "./pages/TasksPage";
import { AdminUsersPage } from "./pages/AdminUsersPage";
import { AdminTasksPage } from "./pages/AdminTasksPage";

function RequireAuth() {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return <div className="boot-screen"><div className="brand-mark">R</div><span>Connecting to RUSA-AI…</span></div>;
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <Outlet />;
}

function RequireAdmin() {
  const { user } = useAuth();
  return user?.role === "admin" ? <Outlet /> : <Navigate to="/" replace />;
}

function AppShell() {
  const { user, logout } = useAuth();
  const isAdmin = user?.role === "admin";
  const navItems = isAdmin
    ? [
        { to: "/admin", label: "Overview", icon: LayoutDashboard, end: true },
        { to: "/admin/users", label: "User management", icon: Users },
        { to: "/admin/tasks", label: "Task operations", icon: ClipboardList },
      ]
    : [
        { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
        { to: "/chat", label: "AI workspace", icon: MessageSquareText },
        { to: "/tasks", label: "My tasks", icon: ClipboardList },
      ];

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-lockup">
          <div className="brand-mark">R</div>
          <div><strong>RUSA<span>-AI</span></strong><small>INTELLIGENCE WORKSPACE</small></div>
        </div>
        <div className="workspace-label">WORKSPACE</div>
        <nav className="side-nav">
          {navItems.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end} className={({ isActive }) => "nav-link" + (isActive ? " active" : "")}>
              <Icon size={18} strokeWidth={1.8} /><span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="sidebar-help"><CircleHelp size={17} /><span>Help & support</span></div>
          <div className="profile-card">
            <div className="avatar">{user?.display_name?.slice(0, 1).toUpperCase() || "R"}</div>
            <div className="profile-meta"><strong>{user?.display_name}</strong><small>{isAdmin ? "Administrator" : "Workspace user"}</small></div>
            <button className="icon-button" aria-label="Sign out" title="Sign out" onClick={() => void logout()}><LogOut size={17} /></button>
          </div>
        </div>
      </aside>
      <main className="main-panel">
        <header className="topbar">
          <div className="breadcrumb"><span>RUSA-AI</span><span className="crumb-separator">/</span><strong>{isAdmin ? "Admin control center" : "User workspace"}</strong></div>
          <div className="topbar-right">
            <span className="secure-indicator"><ShieldCheck size={15} /> Secure session</span>
            <div className="role-chip">{isAdmin ? "ADMIN" : "USER"} <ChevronDown size={13} /></div>
          </div>
        </header>
        <div className="page-content"><Outlet /></div>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireAuth />}>
        <Route element={<AppShell />}>
          <Route index element={<DashboardPage />} />
          <Route path="chat" element={<ChatPage />} />
          <Route path="tasks" element={<TasksPage />} />
          <Route path="admin" element={<RequireAdmin />}>
            <Route index element={<DashboardPage />} />
            <Route path="users" element={<AdminUsersPage />} />
            <Route path="tasks" element={<AdminTasksPage />} />
          </Route>
        </Route>
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
