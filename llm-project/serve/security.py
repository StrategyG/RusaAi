"""Fail-closed API-key authentication and role-based authorization."""
from __future__ import annotations
import hashlib, hmac, json, os, re
from dataclasses import dataclass
from typing import Any
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

API_KEY_HEADER = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)
KNOWN_ROLES = {"platform_admin", "tenant_admin", "developer", "read_only"}
ROLE_PERMISSIONS = {
    "platform_admin": {"chat:invoke", "task:validate", "records:read", "records:write"},
    "tenant_admin": {"chat:invoke", "task:validate", "records:read", "records:write"},
    "developer": {"chat:invoke", "task:validate", "records:read", "records:write"},
    "read_only": {"task:validate", "records:read"},
}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    roles: tuple[str, ...]

    @property
    def permissions(self) -> frozenset[str]:
        return frozenset().union(*(ROLE_PERMISSIONS[role] for role in self.roles))

def load_api_key_registry() -> dict[str, dict[str, Any]]:
    raw = os.getenv("RUSA_API_KEYS_JSON", "")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("RUSA_API_KEYS_JSON is not valid JSON") from exc
    if not isinstance(value, dict) or not value:
        raise ValueError("RUSA_API_KEYS_JSON must be a non-empty object")
    registry: dict[str, dict[str, Any]] = {}
    for key_digest, claims in value.items():
        if not isinstance(key_digest, str) or not _HASH_RE.fullmatch(key_digest):
            raise ValueError("API key registry contains an invalid SHA-256 digest")
        if not isinstance(claims, dict):
            raise ValueError("API key claims must be objects")
        subject, tenant_id, roles = claims.get("subject"), claims.get("tenant_id"), claims.get("roles")
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 128:
            raise ValueError("API key subject is missing or invalid")
        if not isinstance(tenant_id, str) or not tenant_id.strip() or len(tenant_id) > 128:
            raise ValueError("API key tenant_id is missing or invalid")
        if (not isinstance(roles, list) or not roles
                or any(not isinstance(role, str) or role not in KNOWN_ROLES for role in roles)):
            raise ValueError("API key roles are missing or invalid")
        registry[key_digest] = {"subject": subject.strip(), "tenant_id": tenant_id.strip(),
                                "roles": tuple(sorted(set(roles)))}
    return registry

def authenticate(api_key: str | None = Depends(api_key_header)) -> Principal:
    try:
        registry = load_api_key_registry()
    except ValueError:
        raise HTTPException(status_code=503, detail="Authentication service is misconfigured") from None
    if not registry:
        raise HTTPException(status_code=503, detail="Authentication is not configured")
    if not api_key or len(api_key) < 32:
        raise HTTPException(status_code=401, detail="Invalid or missing credentials",
                            headers={"WWW-Authenticate": "ApiKey"})
    supplied_digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    for stored_digest, claims in registry.items():
        if hmac.compare_digest(supplied_digest, stored_digest):
            return Principal(**claims)
    raise HTTPException(status_code=401, detail="Invalid or missing credentials",
                        headers={"WWW-Authenticate": "ApiKey"})

def require_permission(permission: str):
    async def dependency(request: Request, principal: Principal = Depends(authenticate)) -> Principal:
        await request.app.state.rate_limiter.check(request, principal)
        if permission not in principal.permissions:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return principal
    return dependency
