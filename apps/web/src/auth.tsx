import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { authRequest, setAccessToken, type User } from "./api";

type SessionResponse = {
  access_token: string;
  expires_in: number;
  user: User;
};

type AuthContextValue = {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<User>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const acceptSession = useCallback((session: SessionResponse) => {
    setAccessToken(session.access_token);
    setUser(session.user);
  }, []);

  useEffect(() => {
    let active = true;
    fetch("/v1/auth/refresh", {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    })
      .then(async (response) => {
        if (!response.ok) return null;
        return (await response.json()) as SessionResponse;
      })
      .then((session) => {
        if (active && session) acceptSession(session);
      })
      .catch(() => {
        if (active) setAccessToken(null);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [acceptSession]);

  const login = useCallback(
    async (email: string, password: string) => {
      const session = await authRequest<SessionResponse>("/v1/auth/login", {
        email,
        password,
      });
      acceptSession(session);
      return session.user;
    },
    [acceptSession],
  );

  const logout = useCallback(async () => {
    try {
      await fetch("/v1/auth/logout", {
        method: "POST",
        credentials: "same-origin",
      });
    } finally {
      setAccessToken(null);
      setUser(null);
    }
  }, []);

  const value = useMemo(
    () => ({ user, loading, login, logout }),
    [user, loading, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
