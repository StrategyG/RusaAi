import { NextRequest, NextResponse } from "next/server";
import { backendBaseUrl, sameOriginRequest } from "@/lib/backend";
import {
  issueSessionToken,
  readServerSession,
  SESSION_COOKIE,
  sessionCookieOptions
} from "@/lib/session";

export const runtime = "nodejs";

export async function GET() {
  const session = await readServerSession();
  if (!session) return NextResponse.json({ error: "No active session." }, { status: 401 });
  try {
    const response = await fetch(backendBaseUrl() + "/v1/auth/session", {
      headers: { "X-API-Key": session.apiKey },
      cache: "no-store",
      signal: AbortSignal.timeout(5000)
    });
    if (response.status === 401 || response.status === 403) {
      return NextResponse.json({ error: "Session is no longer authorized. Sign in again." }, { status: 401 });
    }
    if (!response.ok) {
      return NextResponse.json({ error: "RUSA API identity could not be revalidated." }, { status: 503 });
    }
    const identity = await response.json();
    if (
      typeof identity.subject !== "string" ||
      typeof identity.tenant_id !== "string" ||
      !Array.isArray(identity.roles) ||
      !identity.roles.every((role: unknown) => typeof role === "string") ||
      identity.subject !== session.subject ||
      identity.tenant_id !== session.tenantId
    ) {
      return NextResponse.json({ error: "Session identity changed or is invalid. Sign in again." }, { status: 401 });
    }
    return NextResponse.json({
      subject: identity.subject,
      tenant_id: identity.tenant_id,
      roles: identity.roles
    });
  } catch {
    return NextResponse.json({ error: "RUSA API identity service is unavailable." }, { status: 503 });
  }
}

export async function POST(request: NextRequest) {
  if (!sameOriginRequest(request)) {
    return NextResponse.json({ error: "Cross-origin request rejected." }, { status: 403 });
  }
  let body: { apiKey?: unknown };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid request body." }, { status: 400 });
  }
  const apiKey = typeof body.apiKey === "string" ? body.apiKey.trim() : "";
  if (!apiKey || apiKey.length > 2048) {
    return NextResponse.json({ error: "Enter a valid tenant API key (maximum 2,048 characters)." }, { status: 400 });
  }

  try {
    const response = await fetch(backendBaseUrl() + "/v1/auth/session", {
      headers: { "X-API-Key": apiKey },
      cache: "no-store"
    });
    if (!response.ok) {
      return NextResponse.json(
        { error: response.status === 401 ? "API key is invalid." : "RUSA API authentication is unavailable." },
        { status: response.status === 401 ? 401 : 503 }
      );
    }
    const identity = await response.json();
    if (
      typeof identity.subject !== "string" ||
      typeof identity.tenant_id !== "string" ||
      !Array.isArray(identity.roles) ||
      !identity.roles.every((role: unknown) => typeof role === "string")
    ) {
      return NextResponse.json({ error: "RUSA API returned an invalid identity." }, { status: 502 });
    }

    const token = await issueSessionToken(apiKey, identity);
    if (token.length > 3800) {
      return NextResponse.json({ error: "The encrypted session would exceed the safe browser-cookie size." }, { status: 400 });
    }
    const result = NextResponse.json({
      subject: identity.subject,
      tenant_id: identity.tenant_id,
      roles: identity.roles
    });
    result.cookies.set(SESSION_COOKIE, token, sessionCookieOptions());
    return result;
  } catch {
    return NextResponse.json(
      { error: "Could not reach RUSA API or session encryption is not configured." },
      { status: 503 }
    );
  }
}

export async function DELETE(request: NextRequest) {
  if (!sameOriginRequest(request)) {
    return NextResponse.json({ error: "Cross-origin request rejected." }, { status: 403 });
  }
  const response = NextResponse.json({ ok: true });
  response.cookies.set(SESSION_COOKIE, "", { ...sessionCookieOptions(), maxAge: 0 });
  return response;
}