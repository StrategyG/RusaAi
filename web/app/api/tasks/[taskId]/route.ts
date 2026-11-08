import { NextRequest, NextResponse } from "next/server";
import { backendFetch, forwardJson, sameOriginRequest, unauthorizedResponse } from "@/lib/backend";
import { readServerSession } from "@/lib/session";

export const runtime = "nodejs";

type RouteContext = { params: Promise<{ taskId: string }> };

export async function GET(_request: NextRequest, context: RouteContext) {
  const session = await readServerSession();
  if (!session) return unauthorizedResponse();
  const { taskId } = await context.params;
  const response = await backendFetch("/v1/aicode/tasks/" + encodeURIComponent(taskId), session);
  return forwardJson(response);
}

export async function DELETE(request: NextRequest, context: RouteContext) {
  if (!sameOriginRequest(request)) {
    return NextResponse.json({ error: "Cross-origin request rejected." }, { status: 403 });
  }
  const session = await readServerSession();
  if (!session) return unauthorizedResponse();
  const { taskId } = await context.params;
  const response = await backendFetch("/v1/aicode/tasks/" + encodeURIComponent(taskId), session, {
    method: "DELETE"
  });
  return forwardJson(response);
}