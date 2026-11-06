# ruff: noqa: I001
"""Database-backed user identity, Argon2 passwords, JWT access tokens and rotating refresh sessions."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from psycopg.rows import dict_row


_EMAIL_RE = re.compile(r"^[^@\s]{1,128}@[^@\s]{1,128}\.[^@\s]{2,63}$")
_TENANT_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_ROLES = {"user", "admin"}


class IdentityError(ValueError):
    """Raised when an identity operation violates an application invariant."""


def validate_email(email: str) -> str:
    normalized = email.strip().lower()
    if len(normalized) > 254 or not _EMAIL_RE.fullmatch(normalized):
        raise IdentityError("A valid email address is required")
    return normalized


def validate_password(password: str) -> None:
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise IdentityError("Password must contain between 12 and 256 characters")


def validate_tenant_id(tenant_id: str) -> str:
    if not isinstance(tenant_id, str) or not _TENANT_RE.fullmatch(tenant_id):
        raise IdentityError("Tenant identifier is invalid")
    return tenant_id


class PostgresIdentityStore:
    """PostgreSQL-backed identities and rotating opaque refresh-token sessions."""

    ISSUER = "rusa-ai"
    AUDIENCE = "rusa-web"

    def __init__(
        self,
        dsn: str,
        signing_secret: str,
        access_token_seconds: int = 900,
        refresh_token_seconds: int = 2_592_000,
    ) -> None:
        if not dsn:
            raise ValueError("DATABASE_URL is required for identity storage")
        if not isinstance(signing_secret, str) or len(signing_secret.encode("utf-8")) < 32:
            raise ValueError("RUSA_AUTH_SECRET must contain at least 32 UTF-8 bytes")
        if not 60 <= access_token_seconds <= 3600:
            raise ValueError("Access-token lifetime must be between 60 and 3600 seconds")
        if not 3600 <= refresh_token_seconds <= 7_776_000:
            raise ValueError("Refresh-token lifetime must be between 3600 and 7776000 seconds")
        self.dsn = dsn
        self.signing_secret = signing_secret
        self.access_token_seconds = access_token_seconds
        self.refresh_token_seconds = refresh_token_seconds
        self.password_hasher = PasswordHasher()
        # Equalize the expensive password-verification path for unknown emails.
        self._dummy_password_hash = self.password_hasher.hash(secrets.token_urlsafe(32))

    def _connect(self):
        import psycopg

        return psycopg.connect(self.dsn, connect_timeout=5, row_factory=dict_row)

    def ensure_schema(self) -> None:
        """Create additive identity tables and indexes; safe to run repeatedly."""
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS rusa_users (
                    id UUID PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    email TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'admin')),
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_login_at TIMESTAMPTZ
                )
                """
            )
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_rusa_users_tenant_email
                ON rusa_users (tenant_id, lower(email))
                """
            )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rusa_users_tenant_created
                ON rusa_users (tenant_id, created_at DESC)
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS rusa_sessions (
                    id UUID PRIMARY KEY,
                    user_id UUID NOT NULL REFERENCES rusa_users(id) ON DELETE CASCADE,
                    refresh_token_hash TEXT NOT NULL UNIQUE,
                    expires_at TIMESTAMPTZ NOT NULL,
                    revoked_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_used_at TIMESTAMPTZ
                )
                """
            )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rusa_sessions_user
                ON rusa_sessions (user_id, expires_at DESC)
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS rusa_auth_login_attempts (
                    bucket_key TEXT PRIMARY KEY,
                    window_started_at TIMESTAMPTZ NOT NULL,
                    attempts INTEGER NOT NULL CHECK (attempts > 0)
                )
                """
            )

    @staticmethod
    def _safe_user(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = {
            "id": str(row["id"]),
            "tenant_id": row["tenant_id"],
            "email": row["email"],
            "display_name": row["display_name"],
            "role": row["role"],
            "is_active": bool(row["is_active"]),
        }
        for field in ("created_at", "updated_at", "last_login_at"):
            value = row.get(field)
            result[field] = value.isoformat() if value else None
        return result

    def create_user(
        self,
        tenant_id: str,
        email: str,
        display_name: str,
        password: str,
        role: str = "user",
    ) -> dict[str, Any]:
        tenant_id = validate_tenant_id(tenant_id)
        email = validate_email(email)
        validate_password(password)
        if role not in _ROLES:
            raise IdentityError("Role must be user or admin")
        display_name = display_name.strip()
        if not 1 <= len(display_name) <= 120:
            raise IdentityError("Display name must contain between 1 and 120 characters")
        user_id = str(uuid.uuid4())
        password_hash = self.password_hasher.hash(password)
        with self._connect() as db:
            row = db.execute(
                """
                INSERT INTO rusa_users
                    (id, tenant_id, email, display_name, password_hash, role)
                VALUES (%s::uuid, %s, %s, %s, %s, %s)
                RETURNING id, tenant_id, email, display_name, role, is_active,
                          created_at, updated_at, last_login_at
                """,
                (user_id, tenant_id, email, display_name, password_hash, role),
            ).fetchone()
        return self._safe_user(row)  # type: ignore[return-value]

    def bootstrap_admin(self, tenant_id: str, email: str, password: str) -> dict[str, Any]:
        """Create the first admin without changing an existing account's privileges."""
        tenant_id = validate_tenant_id(tenant_id)
        email = validate_email(email)
        with self._connect() as db:
            existing = db.execute(
                """
                SELECT id, tenant_id, email, display_name, role, is_active,
                       created_at, updated_at, last_login_at
                FROM rusa_users
                WHERE tenant_id=%s AND lower(email)=lower(%s)
                """,
                (tenant_id, email),
            ).fetchone()
        if existing:
            if existing["role"] != "admin" or not existing["is_active"]:
                raise IdentityError(
                    "Bootstrap account already exists but is not an active administrator"
                )
            return self._safe_user(existing)  # type: ignore[return-value]
        return self.create_user(
            tenant_id=tenant_id,
            email=email,
            display_name="RUSA Administrator",
            password=password,
            role="admin",
        )

    def authenticate(
        self,
        tenant_id: str,
        email: str,
        password: str,
    ) -> dict[str, Any] | None:
        tenant_id = validate_tenant_id(tenant_id)
        email = validate_email(email)
        with self._connect() as db:
            row = db.execute(
                """
                SELECT id, tenant_id, email, display_name, password_hash, role,
                       is_active, created_at, updated_at, last_login_at
                FROM rusa_users
                WHERE tenant_id=%s AND lower(email)=lower(%s)
                LIMIT 1
                """,
                (tenant_id, email),
            ).fetchone()
            if row is None:
                try:
                    self.password_hasher.verify(self._dummy_password_hash, password)
                except (VerificationError, InvalidHashError, TypeError):
                    pass
                return None
            try:
                verified = self.password_hasher.verify(row["password_hash"], password)
            except (VerificationError, InvalidHashError, TypeError):
                return None
            if not verified or not row["is_active"]:
                return None
            if self.password_hasher.check_needs_rehash(row["password_hash"]):
                db.execute(
                    "UPDATE rusa_users SET password_hash=%s, updated_at=now() WHERE id=%s",
                    (self.password_hasher.hash(password), row["id"]),
                )
            db.execute(
                "UPDATE rusa_users SET last_login_at=now() WHERE id=%s",
                (row["id"],),
            )
            row["last_login_at"] = datetime.now(timezone.utc)
        return self._safe_user(row)

    def allow_login_attempt(
        self,
        bucket: str,
        max_attempts: int = 8,
        window_seconds: int = 900,
    ) -> bool:
        digest = hashlib.sha256(bucket.encode("utf-8")).hexdigest()
        with self._connect() as db:
            row = db.execute(
                """
                INSERT INTO rusa_auth_login_attempts
                    (bucket_key, window_started_at, attempts)
                VALUES (%s, now(), 1)
                ON CONFLICT (bucket_key) DO UPDATE SET
                    attempts = CASE
                        WHEN rusa_auth_login_attempts.window_started_at
                             < now() - (%s * interval '1 second')
                        THEN 1
                        ELSE rusa_auth_login_attempts.attempts + 1
                    END,
                    window_started_at = CASE
                        WHEN rusa_auth_login_attempts.window_started_at
                             < now() - (%s * interval '1 second')
                        THEN now()
                        ELSE rusa_auth_login_attempts.window_started_at
                    END
                RETURNING attempts
                """,
                (digest, window_seconds, window_seconds),
            ).fetchone()
        return bool(row and row["attempts"] <= max_attempts)

    def clear_login_attempts(self, bucket: str) -> None:
        digest = hashlib.sha256(bucket.encode("utf-8")).hexdigest()
        with self._connect() as db:
            db.execute(
                "DELETE FROM rusa_auth_login_attempts WHERE bucket_key=%s",
                (digest,),
            )

    def _create_access_token(self, user: dict[str, Any]) -> str:
        now = datetime.now(timezone.utc)
        claims = {
            "iss": self.ISSUER,
            "aud": self.AUDIENCE,
            "sub": user["id"],
            "tenant_id": user["tenant_id"],
            "email": user["email"],
            "role": user["role"],
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=self.access_token_seconds)).timestamp()),
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(claims, self.signing_secret, algorithm="HS256")

    def issue_session(self, user: dict[str, Any]) -> dict[str, Any]:
        refresh_token = secrets.token_urlsafe(48)
        session_id = str(uuid.uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self.refresh_token_seconds
        )
        token_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO rusa_sessions (id, user_id, refresh_token_hash, expires_at)
                VALUES (%s::uuid, %s::uuid, %s, %s)
                """,
                (session_id, user["id"], token_hash, expires_at),
            )
        return {
            "access_token": self._create_access_token(user),
            "refresh_token": refresh_token,
            "expires_in": self.access_token_seconds,
            "user": user,
        }

    def rotate_refresh_token(self, refresh_token: str) -> dict[str, Any] | None:
        if not isinstance(refresh_token, str) or not 32 <= len(refresh_token) <= 256:
            return None
        old_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
        new_token = secrets.token_urlsafe(48)
        new_hash = hashlib.sha256(new_token.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc)
        with self._connect() as db:
            with db.transaction():
                row = db.execute(
                    """
                    SELECT s.id AS session_id, s.expires_at, s.revoked_at,
                           u.id, u.tenant_id, u.email, u.display_name, u.role,
                           u.is_active, u.created_at, u.updated_at, u.last_login_at
                    FROM rusa_sessions AS s
                    JOIN rusa_users AS u ON u.id=s.user_id
                    WHERE s.refresh_token_hash=%s
                    FOR UPDATE OF s
                    """,
                    (old_hash,),
                ).fetchone()
                if (
                    row is None
                    or row["revoked_at"] is not None
                    or row["expires_at"] <= now
                    or not row["is_active"]
                ):
                    return None
                db.execute(
                    """
                    UPDATE rusa_sessions
                    SET refresh_token_hash=%s, expires_at=%s, last_used_at=now()
                    WHERE id=%s::uuid
                    """,
                    (
                        new_hash,
                        now + timedelta(seconds=self.refresh_token_seconds),
                        row["session_id"],
                    ),
                )
        user = self._safe_user(row)
        return {
            "access_token": self._create_access_token(user),  # type: ignore[arg-type]
            "refresh_token": new_token,
            "expires_in": self.access_token_seconds,
            "user": user,
        }

    def revoke_refresh_token(self, refresh_token: str | None) -> bool:
        if not isinstance(refresh_token, str) or not 32 <= len(refresh_token) <= 256:
            return False
        token_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE rusa_sessions
                SET revoked_at=now()
                WHERE refresh_token_hash=%s AND revoked_at IS NULL
                """,
                (token_hash,),
            )
        return cursor.rowcount == 1

    def decode_access_token(self, token: str) -> dict[str, Any] | None:
        try:
            claims = jwt.decode(
                token,
                self.signing_secret,
                algorithms=["HS256"],
                issuer=self.ISSUER,
                audience=self.AUDIENCE,
                options={
                    "require": [
                        "exp",
                        "iat",
                        "sub",
                        "tenant_id",
                        "email",
                        "role",
                        "jti",
                    ]
                },
            )
        except jwt.InvalidTokenError:
            return None
        if claims.get("role") not in _ROLES:
            return None
        try:
            validate_tenant_id(claims.get("tenant_id"))
            uuid.UUID(claims.get("sub", ""))
        except (IdentityError, ValueError, TypeError, AttributeError):
            return None
        return claims

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        try:
            normalized_id = str(uuid.UUID(user_id))
        except (ValueError, TypeError, AttributeError):
            return None
        with self._connect() as db:
            row = db.execute(
                """
                SELECT id, tenant_id, email, display_name, role, is_active,
                       created_at, updated_at, last_login_at
                FROM rusa_users WHERE id=%s::uuid
                """,
                (normalized_id,),
            ).fetchone()
        return self._safe_user(row)

    def list_users(
        self,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        tenant_id = validate_tenant_id(tenant_id)
        if not 1 <= limit <= 100 or offset < 0:
            raise IdentityError("User pagination parameters are invalid")
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT id, tenant_id, email, display_name, role, is_active,
                       created_at, updated_at, last_login_at
                FROM rusa_users
                WHERE tenant_id=%s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (tenant_id, limit, offset),
            ).fetchall()
        return [self._safe_user(row) for row in rows]  # type: ignore[list-item]

    def count_users(self, tenant_id: str) -> dict[str, int]:
        tenant_id = validate_tenant_id(tenant_id)
        with self._connect() as db:
            row = db.execute(
                """
                SELECT COUNT(*)::int AS total,
                       COUNT(*) FILTER (WHERE is_active)::int AS active,
                       COUNT(*) FILTER (WHERE role='admin' AND is_active)::int AS admins
                FROM rusa_users WHERE tenant_id=%s
                """,
                (tenant_id,),
            ).fetchone()
        return {
            "total": int(row["total"]),
            "active": int(row["active"]),
            "admins": int(row["admins"]),
        }

    def update_user(
        self,
        tenant_id: str,
        user_id: str,
        *,
        role: str | None = None,
        is_active: bool | None = None,
    ) -> dict[str, Any] | None:
        tenant_id = validate_tenant_id(tenant_id)
        if role is not None and role not in _ROLES:
            raise IdentityError("Role must be user or admin")
        try:
            normalized_id = str(uuid.UUID(user_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise IdentityError("User identifier is invalid") from exc
        with self._connect() as db:
            with db.transaction():
                # Serialize administrator membership changes per tenant to prevent
                # concurrent demotions from removing the final active admin.
                db.execute(
                    """
                    SELECT id
                    FROM rusa_users
                    WHERE tenant_id=%s AND role='admin' AND is_active
                    ORDER BY id
                    FOR UPDATE
                    """,
                    (tenant_id,),
                ).fetchall()
                row = db.execute(
                    """
                    SELECT id, tenant_id, email, display_name, role, is_active,
                           created_at, updated_at, last_login_at
                    FROM rusa_users
                    WHERE tenant_id=%s AND id=%s::uuid
                    FOR UPDATE
                    """,
                    (tenant_id, normalized_id),
                ).fetchone()
                if row is None:
                    return None
                next_role = role if role is not None else row["role"]
                next_active = is_active if is_active is not None else row["is_active"]
                removes_admin = (
                    row["role"] == "admin"
                    and row["is_active"]
                    and (next_role != "admin" or not next_active)
                )
                if removes_admin:
                    admins = db.execute(
                        """
                        SELECT COUNT(*)::int AS total
                        FROM rusa_users
                        WHERE tenant_id=%s AND role='admin' AND is_active
                        """,
                        (tenant_id,),
                    ).fetchone()["total"]
                    if admins <= 1:
                        raise IdentityError("The last active administrator cannot be disabled or demoted")
                updated = db.execute(
                    """
                    UPDATE rusa_users
                    SET role=%s, is_active=%s, updated_at=now()
                    WHERE id=%s::uuid AND tenant_id=%s
                    RETURNING id, tenant_id, email, display_name, role, is_active,
                              created_at, updated_at, last_login_at
                    """,
                    (next_role, next_active, normalized_id, tenant_id),
                ).fetchone()
                if not next_active:
                    db.execute(
                        "UPDATE rusa_sessions SET revoked_at=now() "
                        "WHERE user_id=%s::uuid AND revoked_at IS NULL",
                        (normalized_id,),
                    )
        return self._safe_user(updated)
