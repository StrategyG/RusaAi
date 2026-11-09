import { backendFetch, forwardJson, unauthorizedResponse } from "@/lib/backend";
import { readServerSession } from "@/lib/session";

export const runtime = "nodejs";

export async function GET() {
  const session = await readServerSession();
  if (!session) return unauthorizedResponse();
  return forwardJson(await backendFetch("/v1/devops/actions", session));
}
