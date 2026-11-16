import { NextRequest, NextResponse } from "next/server";
import { backendFetch, forwardJson, sameOriginRequest, unauthorizedResponse } from "@/lib/backend";
import { readServerSession } from "@/lib/session";

export const runtime = "nodejs";

export async function POST(request: NextRequest) {
  if (!sameOriginRequest(request)) {
    return NextResponse.json({ error: "Cross-origin request rejected." }, { status: 403 });
  }
  const session = await readServerSession();
  if (!session) return unauthorizedResponse();
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid request body." }, { status: 400 });
  }
  const serializedBody = JSON.stringify(body);
  if (new TextEncoder().encode(serializedBody).byteLength > 900 * 1024) {
    return NextResponse.json({ error: "Chat request exceeds the 900 KiB web limit." }, { status: 413 });
  }
  const response = await backendFetch("/v1/chat/completions", session, {
    method: "POST",
    body: serializedBody,
    signal: request.signal
  });
  if (response.ok && (response.headers.get("content-type") || "").includes("text/event-stream")) {
    return new Response(response.body, {
      status: response.status,
      headers: {
        "Content-Type": response.headers.get("content-type") || "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no"
      }
    });
  }
  return forwardJson(response);
}
