import { NextRequest, NextResponse } from "next/server";
import { backendFetch, forwardJson, sameOriginRequest, unauthorizedResponse } from "@/lib/backend";
import { readServerSession } from "@/lib/session";

export const runtime = "nodejs";

export async function POST(request: NextRequest, context: { params: Promise<{ proposalId: string }> }) {
  if (!sameOriginRequest(request)) return NextResponse.json({ error: "Cross-origin request rejected." }, { status: 403 });
  const session = await readServerSession();
  if (!session) return unauthorizedResponse();
  const { proposalId } = await context.params;
  return forwardJson(await backendFetch("/v1/devops/actions/" + encodeURIComponent(proposalId) + "/approve", session, {
    method: "POST",
    body: JSON.stringify({ reason: "Approved in Control Center; infrastructure execution remains disabled." })
  }));
}
