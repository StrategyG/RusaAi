import { useState, type FormEvent } from "react";
import { ArrowRight, Eye, EyeOff, LockKeyhole, Sparkles } from "lucide-react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth";

export function LoginPage() {
  const { user, loading, login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  if (!loading && user) return <Navigate to={user.role === "admin" ? "/admin" : "/"} replace />;

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      const signedInUser = await login(email, password);
      const next = (location.state as { from?: string } | null)?.from;
      navigate(next || (signedInUser.role === "admin" ? "/admin" : "/"), { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to sign in");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-layout">
      <section className="login-visual">
        <div className="login-brand"><div className="brand-mark">R</div><strong>RUSA<span>-AI</span></strong></div>
        <div className="login-hero-copy">
          <div className="eyebrow"><Sparkles size={14} /> ENTERPRISE AI PLATFORM</div>
          <h1>Think bigger.<br /><span>Build with intelligence.</span></h1>
          <p>One workspace to work with AI, coordinate agents, and move complex work forward.</p>
        </div>
        <div className="login-visual-footer"><span>PRIVATE WORKSPACE</span><span>SECURE BY DESIGN</span><span>BUILT FOR TEAMS</span></div>
      </section>
      <section className="login-panel">
        <div className="login-card">
          <div className="login-icon"><LockKeyhole size={22} /></div>
          <div className="eyebrow muted">WELCOME BACK</div>
          <h2>Sign in to RUSA-AI</h2>
          <p className="muted-copy">Use the account provided by your workspace administrator.</p>
          <form onSubmit={handleSubmit} className="form-stack">
            <label>Email address<input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@company.com" required /></label>
            <label>Password<div className="password-field"><input type={showPassword ? "text" : "password"} autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Enter your password" required /><button type="button" className="password-toggle" aria-label={showPassword ? "Hide password" : "Show password"} onClick={() => setShowPassword(!showPassword)}>{showPassword ? <EyeOff size={17} /> : <Eye size={17} />}</button></div></label>
            {error && <div className="error-banner" role="alert">{error}</div>}
            <button className="primary-button full-button" disabled={submitting || loading}>{submitting ? "Signing in…" : <>Continue <ArrowRight size={17} /></>}</button>
          </form>
          <div className="login-footnote">Access is managed by your RUSA-AI administrator.</div>
        </div>
      </section>
    </div>
  );
}
