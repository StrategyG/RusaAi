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
  return NextResponse.json({
    subject: session.subject,
    tenant_id: session.tenantId,
    roles: session.roles
  });
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
  if (!apiKey || apiKey.length > 4096) {
    return NextResponse.json({ error: "Enter a valid tenant API key." }, { status: 400 });
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