import { createHash } from "node:crypto";
import { cookies } from "next/headers";
import { EncryptJWT, jwtDecrypt } from "jose";

export const SESSION_COOKIE = "rusa_session";
const SESSION_TTL_SECONDS = 60 * 60 * 8;

export type SessionIdentity = {
  subject: string;
  tenant_id: string;
  roles: string[];
};

export type ServerSession = {
  apiKey: string;
  subject: string;
  tenantId: string;
  roles: string[];
};

function encryptionKey(): Uint8Array {
  const secret = process.env.RUSA_WEB_SESSION_SECRET;
  if (!secret || secret.length < 32) {
    throw new Error("RUSA_WEB_SESSION_SECRET must contain at least 32 characters.");
  }
  return new Uint8Array(createHash("sha256").update(secret).digest());
}

export async function issueSessionToken(apiKey: string, identity: SessionIdentity): Promise<string> {
  const token = await new EncryptJWT({
    apiKey,
    subject: identity.subject,
    tenantId: identity.tenant_id,
    roles: identity.roles
  })
    .setProtectedHeader({ alg: "dir", enc: "A256GCM" })
    .setIssuedAt()
    .setExpirationTime("8h")
    .encrypt(encryptionKey());
  if (token.length > 3800) {
    throw new Error("Encrypted session token exceeds the safe browser-cookie size.");
  }
  return token;
}

export async function readServerSession(): Promise<ServerSession | null> {
  try {
    const cookieStore = await cookies();
    const token = cookieStore.get(SESSION_COOKIE)?.value;
    if (!token) return null;
    const { payload } = await jwtDecrypt(token, encryptionKey());
    if (
      typeof payload.apiKey !== "string" ||
      payload.apiKey.length === 0 || payload.apiKey.length > 2048 ||
      typeof payload.subject !== "string" || payload.subject.length === 0 || payload.subject.length > 128 ||
      typeof payload.tenantId !== "string" || payload.tenantId.length === 0 || payload.tenantId.length > 128 ||
      !Array.isArray(payload.roles) || payload.roles.length === 0 ||
      !payload.roles.every((role) => ["platform_admin", "tenant_admin", "developer", "read_only"].includes(String(role)))
    ) return null;
    return {
      apiKey: payload.apiKey,
      subject: payload.subject,
      tenantId: payload.tenantId,
      roles: payload.roles as string[]
    };
  } catch {
    return null;
  }
}

export function sessionCookieOptions() {
  return {
    httpOnly: true,
    secure:
      process.env.RUSA_WEB_COOKIE_SECURE === "true" ||
      (process.env.RUSA_WEB_COOKIE_SECURE !== "false" && process.env.NODE_ENV === "production"),
    sameSite: "lax" as const,
    path: "/",
    maxAge: SESSION_TTL_SECONDS
  };
}