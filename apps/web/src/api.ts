export type User = {
  id: string;
  tenant_id: string;
  email: string;
  display_name: string;
  role: "user" | "admin";
  is_active: boolean;
  created_at?: string | null;
  updated_at?: string | null;
  last_login_at?: string | null;
};

export type AuthSession = {
  access_token: string;
  expires_in: number;
  user: User;
};

let accessToken: string | null = null;
let authRevision = 0;
let refreshPromise: Promise<AuthSession | null> | null = null;
let refreshPromiseRevision: number | null = null;

export function setAccessToken(token: string | null) {
  accessToken = token;
  authRevision += 1;
}

/**
 * Share one rotating-refresh request across app bootstrap and API retries.
 * This prevents React StrictMode or concurrent 401 responses from reusing
 * the same one-time refresh token at the same time.
 */
export async function refreshSession(): Promise<AuthSession | null> {
  if (!refreshPromise) {
    const revisionAtStart = authRevision;
    refreshPromiseRevision = revisionAtStart;
    refreshPromise = fetch("/v1/auth/refresh", {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    })
      .then(async (response) => {
        if (!response.ok) return null;
        const session = (await response.json()) as AuthSession;
        if (authRevision !== revisionAtStart) return null;
        accessToken = session.access_token;
        authRevision += 1;
        return session;
      })
      .catch(() => null)
      .finally(() => {
        refreshPromise = null;
        refreshPromiseRevision = null;
      });
  }
  const promiseRevision = refreshPromiseRevision ?? authRevision;
  const session = await refreshPromise;
  if (!session && authRevision === promiseRevision) accessToken = null;
  return session;
}

async function refreshAccessToken(): Promise<boolean> {
  const session = await refreshSession();
  // A concurrent login may supersede a refresh that was already in flight.
  // In that case, keep the newly issued access token and retry the request.
  if (session || accessToken) return true;
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event("rusa:session-expired"));
  }
  return false;
}

export async function api<T>(
  path: string,
  init: RequestInit = {},
  retry = true,
): Promise<T> {
  const makeRequest = () => {
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    if (init.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    if (accessToken) headers.set("Authorization", "Bearer " + accessToken);
    return fetch(path, { ...init, headers, credentials: "same-origin" });
  };

  let response = await makeRequest();
  if (response.status === 401 && retry && !path.startsWith("/v1/auth/")) {
    if (await refreshAccessToken()) response = await makeRequest();
  }

  const raw = await response.text();
  let data: unknown = null;
  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch {
      data = { detail: raw };
    }
  }
  if (!response.ok) {
    const detail =
      typeof data === "object" && data !== null && "detail" in data
        ? String((data as { detail: unknown }).detail)
        : "Request failed (" + response.status + ")";
    throw new Error(detail);
  }
  return data as T;
}

export async function authRequest<T>(
  path: string,
  body?: Record<string, unknown>,
): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = (await response.json().catch(() => ({}))) as {
    detail?: string;
  };
  if (!response.ok) throw new Error(data.detail || "Authentication failed");
  return data as T;
}
