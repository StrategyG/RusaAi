import { NextResponse } from "next/server";
import type { ServerSession } from "./session";

export function backendBaseUrl(): string {
  const value = process.env.RUSA_API_BASE_URL || "http://127.0.0.1:8000";
  return value.replace(/\/+$/, "");
}

export function backendFetch(path: string, session: ServerSession, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  headers.set("X-API-Key", session.apiKey);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return fetch(backendBaseUrl() + path, {
    ...init,
    headers,
    cache: "no-store"
  });
}

export async function forwardJson(response: Response): Promise<NextResponse> {
  const raw = await response.text();
  let body: unknown;
  try {
    body = raw ? JSON.parse(raw) : {};
  } catch {
    body = { detail: raw || "The RUSA API returned an unreadable response." };
  }
  return NextResponse.json(body, { status: response.status });
}

export function unauthorizedResponse() {
  return NextResponse.json({ error: "Authentication required." }, { status: 401 });
}

export function sameOriginRequest(request: Request): boolean {
  const origin = request.headers.get("origin");
  if (!origin) return false;
  try {
    const originUrl = new URL(origin);
    const requestUrl = new URL(request.url);
    const requestHost = request.headers.get("host") || requestUrl.host;
    const forwardedProto = request.headers.get("x-forwarded-proto")?.split(",")[0].trim();
    const requestProtocol = forwardedProto ? forwardedProto + ":" : requestUrl.protocol;
    return originUrl.host.toLowerCase() === requestHost.toLowerCase()
      && originUrl.protocol === requestProtocol;
  } catch {
    return false;
  }
}