import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../llm-project/serve")),
)

from app import app  # noqa: E402, I001


ADMIN_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
TENANT = "tenant-ui-test"
ORIGIN = {"Origin": "http://testserver"}


def make_user(user_id: str, role: str) -> dict:
    return {
        "id": user_id,
        "tenant_id": TENANT,
        "email": role + "@example.test",
        "display_name": role.title() + " User",
        "role": role,
        "is_active": True,
        "created_at": "2026-11-06T00:00:00+00:00",
        "updated_at": "2026-11-06T00:00:00+00:00",
        "last_login_at": None,
    }


class FakeIdentityStore:
    def __init__(self):
        self.users = {
            ADMIN_ID: make_user(ADMIN_ID, "admin"),
            USER_ID: make_user(USER_ID, "user"),
        }
        self.attempts = {}
        self.refreshes = {}
        self.revoked = []

    def allow_login_attempt(self, bucket, max_attempts=8, window_seconds=900):
        self.attempts[bucket] = self.attempts.get(bucket, 0) + 1
        return self.attempts[bucket] <= max_attempts

    def clear_login_attempts(self, bucket):
        self.attempts.pop(bucket, None)

    def authenticate(self, tenant_id, email, password):
        for user in self.users.values():
            if (
                user["tenant_id"] == tenant_id
                and user["email"] == email.strip().lower()
                and password == "correct-password"
                and user["is_active"]
            ):
                return user
        return None

    def issue_session(self, user):
        token = "admin-token" if user["role"] == "admin" else "user-token"
        refresh = "refresh-token-for-" + user["id"]
        self.refreshes[refresh] = user["id"]
        return {
            "access_token": token,
            "refresh_token": refresh,
            "expires_in": 900,
            "user": user,
        }

    def rotate_refresh_token(self, token):
        user_id = self.refreshes.pop(token, None)
        if not user_id:
            return None
        return self.issue_session(self.users[user_id])

    def revoke_refresh_token(self, token):
        self.revoked.append(token)
        return self.refreshes.pop(token, None) is not None

    def decode_access_token(self, token):
        user = self.users[ADMIN_ID] if token == "admin-token" else self.users[USER_ID] if token == "user-token" else None
        if user is None:
            return None
        return {
            "sub": user["id"],
            "tenant_id": user["tenant_id"],
            "role": user["role"],
        }

    def get_user(self, user_id):
        return self.users.get(user_id)

    def count_users(self, tenant_id):
        users = [u for u in self.users.values() if u["tenant_id"] == tenant_id]
        return {
            "total": len(users),
            "active": sum(1 for u in users if u["is_active"]),
            "admins": sum(1 for u in users if u["role"] == "admin" and u["is_active"]),
        }

    def list_users(self, tenant_id, limit=50, offset=0):
        users = [u for u in self.users.values() if u["tenant_id"] == tenant_id]
        return users[offset : offset + limit]

    def create_user(self, tenant_id, email, display_name, password, role="user"):
        user = make_user(str(uuid.uuid4()), role)
        user.update(
            tenant_id=tenant_id,
            email=email.strip().lower(),
            display_name=display_name,
            role=role,
        )
        self.users[user["id"]] = user
        return user

    def update_user(self, tenant_id, user_id, *, role=None, is_active=None):
        user = self.users.get(user_id)
        if user is None or user["tenant_id"] != tenant_id:
            return None
        if role is not None:
            user["role"] = role
        if is_active is not None:
            user["is_active"] = is_active
        return user


class FakeAuditLog:
    def __init__(self):
        self.calls = []

    def record(self, event, task_id, actor, details=None, tenant_id="default"):
        self.calls.append((event, task_id, actor, details or {}, tenant_id))


class FakeQueue:
    def __init__(self):
        self.enqueued = []
        self.records = {}

    def enqueue(self, tenant_id, task_id, payload, idempotency_key):
        self.enqueued.append((tenant_id, task_id, payload, idempotency_key))
        record = {
            "task_id": task_id,
            "tenant_id": tenant_id,
            "state": "queued",
            "attempts": 0,
            "max_attempts": 3,
            "payload": payload,
            "result": None,
            "last_error": None,
            "created_at": "2026-11-06T00:00:00+00:00",
            "updated_at": "2026-11-06T00:00:00+00:00",
        }
        self.records[task_id] = record
        return {"task_id": task_id, "state": "queued"}

    def get(self, tenant_id, task_id):
        record = self.records.get(task_id)
        if record is None or record["tenant_id"] != tenant_id:
            return None
        return record

    def stats(self, tenant_id, user_id=None):
        return {
            "tenant_id": tenant_id,
            "counts": {"queued": 0, "running": 0, "succeeded": 0, "failed": 0, "dead_letter": 0, "cancelled": 0},
            "oldest_queued_age_seconds": 0,
        }

    def list_tasks(self, tenant_id, user_id=None, limit=50, offset=0):
        records = [r for r in self.records.values() if r["tenant_id"] == tenant_id]
        if user_id:
            records = [r for r in records if r["payload"].get("user_id") == user_id]
        return records[offset : offset + limit]

    def list_dead_letters(self, tenant_id, limit=50, user_id=None):
        return []

    def cancel(self, tenant_id, task_id):
        record = self.get(tenant_id, task_id)
        if record is None or record["state"] not in {"queued", "running"}:
            return False
        record["state"] = "cancelled"
        return True

    def retry_dead_letter(self, tenant_id, task_id):
        return False


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("RUSA_API_KEY", "test-service-key-that-is-long-enough-123456")
    monkeypatch.delenv("RUSA_API_KEY_TENANTS_JSON", raising=False)
    monkeypatch.setenv("RUSA_AGENT_ENABLED", "true")
    monkeypatch.setenv("RUSA_TENANT_ID", TENANT)
    monkeypatch.setenv("RUSA_AUTH_ENABLED", "false")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RUSA_MODEL_PATH", raising=False)
    with TestClient(app) as test_client:
        app.state.identity_store = FakeIdentityStore()
        app.state.task_queue = FakeQueue()
        app.state.audit_log = FakeAuditLog()
        yield test_client
        app.state.identity_store = None
        app.state.task_queue = None
        app.state.audit_log = None
        app.state.backend = None


def test_login_issues_short_lived_access_token_and_httponly_refresh_cookie(client):
    response = client.post(
        "/v1/auth/login",
        headers=ORIGIN,
        json={"email": "user@example.test", "password": "correct-password"},
    )
    assert response.status_code == 200
    assert response.json()["token_type"] == "Bearer"
    assert response.json()["user"]["role"] == "user"
    cookie = response.headers["set-cookie"].lower()
    assert "rusa_refresh=" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie


def test_login_rejects_cross_origin_requests(client):
    response = client.post(
        "/v1/auth/login",
        headers={"Origin": "https://attacker.example"},
        json={"email": "user@example.test", "password": "correct-password"},
    )
    assert response.status_code == 403


def test_login_is_scoped_to_configured_tenant(client, monkeypatch):
    monkeypatch.setenv("RUSA_TENANT_ID", "different-tenant")
    response = client.post(
        "/v1/auth/login",
        headers=ORIGIN,
        json={"email": "user@example.test", "password": "correct-password"},
    )
    assert response.status_code == 401


def test_bearer_user_can_read_own_profile(client):
    response = client.get("/v1/auth/me", headers={"Authorization": "Bearer user-token"})
    assert response.status_code == 200
    assert response.json()["user"]["id"] == USER_ID


def test_user_cannot_access_admin_user_management(client):
    response = client.get("/v1/admin/users", headers={"Authorization": "Bearer user-token"})
    assert response.status_code == 403


def test_admin_can_list_users_and_counts(client):
    response = client.get("/v1/admin/users", headers={"Authorization": "Bearer admin-token"})
    assert response.status_code == 200
    assert response.json()["counts"]["total"] == 2
    assert len(response.json()["items"]) == 2


def test_disabled_user_is_rejected_even_with_a_valid_access_token(client):
    app.state.identity_store.users[USER_ID]["is_active"] = False
    response = client.get("/v1/auth/me", headers={"Authorization": "Bearer user-token"})
    assert response.status_code == 401


def test_task_submission_is_attributed_to_authenticated_user(client):
    task_id = str(uuid.uuid4())
    envelope = {
        "protocol_version": "1.0",
        "task_id": task_id,
        "message_id": str(uuid.uuid4()),
        "type": "task.create",
        "from": {"agent_id": "workspace-user", "role": "human"},
        "to": {"agent_id": "rusa-main", "role": "rusa_orchestrator"},
        "created_at": "2026-11-06T10:00:00Z",
        "task": {
            "objective": "Build a user workspace",
            "acceptance_criteria": ["UI is connected to API"],
            "repository": "StrategyG/RusaAi",
            "base_branch": "main",
            "work_branch": "feature/ui-test",
        },
        "policy": {
            "allowed_actions": ["read_repository"],
            "prohibited_actions": ["deploy"],
            "human_approval_required": ["merge", "deploy"],
        },
    }
    response = client.post(
        "/v1/agent/tasks",
        headers={
            "Authorization": "Bearer user-token",
            "Idempotency-Key": "ui-test-task-key",
        },
        json={"envelope": envelope},
    )
    assert response.status_code == 202
    stored = app.state.task_queue.enqueued[0][2]
    assert stored["user_id"] == USER_ID
    assert stored["tenant_id"] == TENANT


def test_user_task_list_is_scoped_to_owner(client):
    task_id = str(uuid.uuid4())
    app.state.task_queue.records[task_id] = {
        "task_id": task_id,
        "tenant_id": TENANT,
        "state": "queued",
        "attempts": 0,
        "max_attempts": 3,
        "payload": {"task_id": task_id, "tenant_id": TENANT, "user_id": ADMIN_ID, "objective": "admin task"},
        "result": None,
        "last_error": None,
        "created_at": "2026-11-06T00:00:00+00:00",
        "updated_at": "2026-11-06T00:00:00+00:00",
    }
    response = client.get("/v1/agent/tasks", headers={"Authorization": "Bearer user-token"})
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_admin_can_list_tenant_tasks(client):
    task_id = str(uuid.uuid4())
    app.state.task_queue.records[task_id] = {
        "task_id": task_id,
        "tenant_id": TENANT,
        "state": "queued",
        "attempts": 0,
        "max_attempts": 3,
        "payload": {"task_id": task_id, "tenant_id": TENANT, "user_id": USER_ID, "objective": "user task"},
        "result": None,
        "last_error": None,
        "created_at": "2026-11-06T00:00:00+00:00",
        "updated_at": "2026-11-06T00:00:00+00:00",
    }
    response = client.get("/v1/admin/tasks", headers={"Authorization": "Bearer admin-token"})
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["objective"] == "user task"



def test_admin_can_create_user_without_exposing_password(client):
    response = client.post(
        "/v1/admin/users",
        headers={"Authorization": "Bearer admin-token"},
        json={
            "email": "new-user@example.test",
            "display_name": "New Workspace User",
            "password": "a-strong-temporary-password",
            "role": "user",
        },
    )
    assert response.status_code == 201
    user = response.json()["user"]
    assert user["email"] == "new-user@example.test"
    assert user["role"] == "user"
    assert "password" not in user
    assert "password_hash" not in user


def test_refresh_rejects_cross_origin_requests(client):
    response = client.post(
        "/v1/auth/refresh",
        headers={"Origin": "https://attacker.example"},
        cookies={"rusa_refresh": "refresh-token-for-" + USER_ID},
    )
    assert response.status_code == 403
