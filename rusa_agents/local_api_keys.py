"""Persistent, hash-only API keys for the single-user local desktop installation.

This store is opt-in. Hosted/multi-tenant deployments continue to use the
existing centrally provisioned RUSA_API_KEYS_JSON registry.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from rusa_agents.config import KNOWN_API_ROLES, ApiPrincipal

_KEY_PREFIX = "rusa_sk_"
_ALLOWED_EDITOR_ROLES = {"developer", "read_only"}


class LocalSetupAlreadyInitialized(RuntimeError):
    """Raised when the one-time local owner setup has already completed."""


class LocalApiKeyStore:
    def __init__(self, database_path: str | Path):
        self.path = Path(database_path).expanduser().absolute()
        if self.path.is_symlink():
            raise ValueError("Local API key database must not be a symlink")
        if not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, mode=0o700)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _secure_database_file(self) -> None:
        if self.path.exists():
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                # Windows ACLs are managed by the installer; chmod is best-effort.
                pass

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS local_api_keys (
                    key_id TEXT PRIMARY KEY,
                    key_sha256 TEXT NOT NULL UNIQUE,
                    subject TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    roles_json TEXT NOT NULL,
                    label TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT,
                    is_bootstrap INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS local_install_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    initialized_at TEXT NOT NULL
                )
                """
            )
        self._secure_database_file()

    @staticmethod
    def _new_key() -> str:
        return _KEY_PREFIX + secrets.token_urlsafe(32)

    @staticmethod
    def _digest(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def is_initialized(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM local_install_state WHERE singleton=1"
            ).fetchone()
        return row is not None

    def initialize_owner(self) -> dict:
        raw_key = self._new_key()
        key_id = str(uuid4())
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM local_install_state WHERE singleton=1"
            ).fetchone()
            if exists:
                connection.rollback()
                raise LocalSetupAlreadyInitialized("Local RUSA AI setup has already completed")
            connection.execute(
                """
                INSERT INTO local_api_keys
                (key_id,key_sha256,subject,tenant_id,roles_json,label,created_at,is_bootstrap)
                VALUES (?,?,?,?,?,?,?,1)
                """,
                (
                    key_id, self._digest(raw_key), "local-owner", "local",
                    json.dumps(["tenant_admin"]), "Local owner", now,
                ),
            )
            connection.execute(
                "INSERT INTO local_install_state(singleton,initialized_at) VALUES (1,?)",
                (now,),
            )
            connection.commit()
        self._secure_database_file()
        return {"key_id": key_id, "api_key": raw_key, "label": "Local owner", "created_at": now}

    def authenticate(self, raw_key: str | None) -> ApiPrincipal | None:
        if (
            not isinstance(raw_key, str)
            or len(raw_key) > 256
            or not raw_key.startswith(_KEY_PREFIX)
        ):
            return None
        digest = self._digest(raw_key)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT subject,tenant_id,roles_json
                FROM local_api_keys
                WHERE key_sha256=? AND revoked_at IS NULL
                """,
                (digest,),
            ).fetchone()
        if row is None:
            return None
        try:
            roles = json.loads(row["roles_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(roles, list)
            or not roles
            or any(not isinstance(role, str) or role not in KNOWN_API_ROLES for role in roles)
        ):
            return None
        return ApiPrincipal(
            subject=row["subject"], tenant_id=row["tenant_id"], roles=tuple(roles)
        )

    def create_editor_key(self, *, tenant_id: str, label: str, roles: list[str]) -> dict:
        clean_label = label.strip()
        if not clean_label or len(clean_label) > 80:
            raise ValueError("API key label must contain 1–80 characters")
        normalized_roles = sorted(set(roles))
        if not normalized_roles or not set(normalized_roles) <= _ALLOWED_EDITOR_ROLES:
            raise ValueError("Editor API keys may only use developer or read_only roles")
        if not tenant_id.strip() or len(tenant_id) > 128:
            raise ValueError("Invalid tenant identifier")
        raw_key, key_id, now = self._new_key(), str(uuid4()), self._now()
        subject = "local-editor:" + re.sub(r"[^A-Za-z0-9_.-]+", "-", clean_label)[:64]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute(
                "SELECT 1 FROM local_install_state WHERE singleton=1"
            ).fetchone():
                connection.rollback()
                raise RuntimeError("Local setup has not been initialized")
            connection.execute(
                """
                INSERT INTO local_api_keys
                (key_id,key_sha256,subject,tenant_id,roles_json,label,created_at,is_bootstrap)
                VALUES (?,?,?,?,?,?,?,0)
                """,
                (
                    key_id, self._digest(raw_key), subject, tenant_id,
                    json.dumps(normalized_roles), clean_label, now,
                ),
            )
            connection.commit()
        self._secure_database_file()
        return {"key_id": key_id, "api_key": raw_key, "label": clean_label, "created_at": now}

    def list_keys(self, tenant_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT key_id,label,subject,roles_json,created_at,revoked_at,is_bootstrap
                FROM local_api_keys WHERE tenant_id=? ORDER BY created_at DESC
                """,
                (tenant_id,),
            ).fetchall()
        return [
            {
                "key_id": row["key_id"],
                "label": row["label"],
                "subject": row["subject"],
                "roles": json.loads(row["roles_json"]),
                "created_at": row["created_at"],
                "revoked_at": row["revoked_at"],
                "is_bootstrap": bool(row["is_bootstrap"]),
            }
            for row in rows
        ]

    def revoke_key(self, *, key_id: str, tenant_id: str) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT is_bootstrap,roles_json FROM local_api_keys
                WHERE key_id=? AND tenant_id=? AND revoked_at IS NULL
                """,
                (key_id, tenant_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            roles = json.loads(row["roles_json"])
            if "tenant_admin" in roles:
                active_admins = connection.execute(
                    """
                    SELECT COUNT(*) FROM local_api_keys
                    WHERE tenant_id=? AND revoked_at IS NULL
                      AND roles_json LIKE '%tenant_admin%'
                    """,
                    (tenant_id,),
                ).fetchone()[0]
                if active_admins <= 1:
                    connection.rollback()
                    raise ValueError("Cannot revoke the last active tenant administrator key")
            connection.execute(
                "UPDATE local_api_keys SET revoked_at=? WHERE key_id=? AND tenant_id=?",
                (self._now(), key_id, tenant_id),
            )
            connection.commit()
        self._secure_database_file()
        return True


@lru_cache(maxsize=4)
def get_local_api_key_store(database_path: str) -> LocalApiKeyStore:
    """Reuse the stateless SQLite store wrapper without holding a DB connection."""
    return LocalApiKeyStore(database_path)
