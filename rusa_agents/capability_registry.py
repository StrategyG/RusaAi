"""Validated, fail-closed loader for the versioned Agent/Worker runtime registry."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import jsonschema


class RegistryValidationError(ValueError):
    """The registry document or its cross-record invariants are invalid."""


class RegistryNotActivatedError(RuntimeError):
    """Execution was requested while the registry or role is not approved."""


@dataclass(frozen=True)
class RoleDefinition:
    role_id: str
    display_name: str
    identity_type: str
    parent_role_id: str | None
    registration_status: str
    runtime_status: str
    activation_state: str
    mission: str
    capability_chunk_ids: tuple[str, ...]

    @property
    def scope(self) -> str:
        return "agent" if self.identity_type == "core_agent" else "worker"

    @property
    def dispatchable(self) -> bool:
        return (
            self.registration_status == "registered"
            and self.runtime_status == "verified"
            and self.activation_state == "active"
        )


class AgentWorkerRegistry:
    """Immutable runtime view of the Agent/Worker role manifest.

    Registry membership is descriptive only. It never grants tool, shell,
    filesystem, network, browser, model, or secret permissions.
    """

    DEFAULT_REGISTRY_PATH = (
        Path(__file__).resolve().parent / "data" / "agent_worker_registry.v1.json"
    )
    DEFAULT_SCHEMA_PATH = (
        Path(__file__).resolve().parent / "data" / "agent_worker_registry.v1.schema.json"
    )

    def __init__(self, data: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
        self._data = json.loads(json.dumps(data))
        self._schema = json.loads(json.dumps(schema))
        self._validate()
        self.roles = {
            item["role_id"]: RoleDefinition(
                role_id=item["role_id"],
                display_name=item["display_name"],
                identity_type=item["identity_type"],
                parent_role_id=item["parent_role_id"],
                registration_status=item["registration_status"],
                runtime_status=item["runtime_status"],
                activation_state=item["activation_state"],
                mission=item["mission"],
                capability_chunk_ids=tuple(item["capability_chunk_ids"]),
            )
            for item in self._data["roles"]
        }

    @classmethod
    def load(
        cls,
        registry_path: str | Path | None = None,
        schema_path: str | Path | None = None,
    ) -> "AgentWorkerRegistry":
        registry_file = Path(registry_path or cls.DEFAULT_REGISTRY_PATH)
        schema_file = Path(schema_path or cls.DEFAULT_SCHEMA_PATH)
        try:
            data = json.loads(registry_file.read_text(encoding="utf-8"))
            schema = json.loads(schema_file.read_text(encoding="utf-8"))
        except OSError as exc:
            raise RegistryValidationError(f"Registry artifact is unavailable: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RegistryValidationError(f"Registry JSON is invalid: {exc}") from exc
        return cls(data, schema)

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any], schema: Mapping[str, Any]
    ) -> "AgentWorkerRegistry":
        return cls(data, schema)

    def _validate(self) -> None:
        try:
            jsonschema.Draft202012Validator.check_schema(self._schema)
            validator = jsonschema.Draft202012Validator(
                self._schema, format_checker=jsonschema.FormatChecker()
            )
            errors = sorted(
                validator.iter_errors(self._data),
                key=lambda error: tuple(str(part) for part in error.absolute_path),
            )
        except jsonschema.SchemaError as exc:
            raise RegistryValidationError(f"Registry schema is invalid: {exc.message}") from exc
        if errors:
            first = errors[0]
            location = "/".join(str(part) for part in first.absolute_path) or "<root>"
            raise RegistryValidationError(f"{location}: {first.message}")

        roles = self._data["roles"]
        role_ids = [item["role_id"] for item in roles]
        if len(role_ids) != len(set(role_ids)):
            raise RegistryValidationError("role_id values must be unique")
        role_map = {item["role_id"]: item for item in roles}
        identity_counts = Counter(item["identity_type"] for item in roles)
        expected = {
            "core_agent": self._data["counts"]["agents"],
            "aicode_worker": self._data["counts"]["aicode_workers"],
            "devops_lifecycle_profile": self._data["counts"]["devops_lifecycle_profiles"],
        }
        if dict(identity_counts) != expected:
            raise RegistryValidationError("Role identity counts do not match the registry counts")
        if expected["core_agent"] != 10 or expected["aicode_worker"] != 6:
            raise RegistryValidationError("The v1 registry requires 10 agents and 6 AICode workers")
        if expected["devops_lifecycle_profile"] != 8:
            raise RegistryValidationError("The v1 registry requires 8 DevOps lifecycle profiles")
        if self._data["counts"]["workers_total"] != (
            expected["aicode_worker"] + expected["devops_lifecycle_profile"]
        ):
            raise RegistryValidationError("workers_total is inconsistent")
        for item in roles:
            parent = item["parent_role_id"]
            if parent is not None and parent not in role_map:
                raise RegistryValidationError(
                    f"{item['role_id']} references unknown parent role {parent}"
                )
            if item["identity_type"] == "devops_lifecycle_profile" and parent != "devops":
                raise RegistryValidationError(
                    f"{item['role_id']} must remain a profile under the DevOps agent"
                )
            if item["identity_type"] != "devops_lifecycle_profile" and parent is not None:
                raise RegistryValidationError(
                    f"{item['role_id']} must not have a fixed parent role"
                )
        if self._data["status"] == "active":
            inactive = [
                item["role_id"] for item in roles
                if not (
                    item["registration_status"] == "registered"
                    and item["runtime_status"] == "verified"
                    and item["activation_state"] == "active"
                )
            ]
            if inactive:
                raise RegistryValidationError(
                    "An active registry cannot contain inactive roles: " + ", ".join(inactive)
                )

    @property
    def status(self) -> str:
        return self._data["status"]

    @property
    def registry_id(self) -> str:
        return self._data["registry_id"]

    @property
    def raw_data(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._data))

    def get_role(self, role_id: str) -> RoleDefinition:
        try:
            return self.roles[role_id]
        except KeyError as exc:
            raise KeyError(f"Unknown Agent/Worker role: {role_id}") from exc

    def list_roles(self, scope: str | None = None) -> tuple[RoleDefinition, ...]:
        if scope not in {None, "agent", "worker"}:
            raise ValueError("scope must be 'agent', 'worker', or None")
        roles = tuple(self.roles.values())
        if scope is None:
            return roles
        return tuple(role for role in roles if role.scope == scope)

    def is_dispatchable(self, role: RoleDefinition | str) -> bool:
        definition = self.get_role(role) if isinstance(role, str) else role
        return self.status == "active" and definition.dispatchable

    def require_dispatchable(self, role_id: str) -> RoleDefinition:
        role = self.get_role(role_id)
        if not self.is_dispatchable(role):
            raise RegistryNotActivatedError(
                f"Role {role_id!r} is not approved and active for execution"
            )
        return role

    def summary(self) -> dict[str, Any]:
        return {
            "registry_id": self.registry_id,
            "status": self.status,
            "role_count": len(self.roles),
            "agents": sum(role.identity_type == "core_agent" for role in self.roles.values()),
            "aicode_workers": sum(
                role.identity_type == "aicode_worker" for role in self.roles.values()
            ),
            "devops_lifecycle_profiles": sum(
                role.identity_type == "devops_lifecycle_profile"
                for role in self.roles.values()
            ),
            "dispatchable_roles": sum(
                self.is_dispatchable(role) for role in self.roles.values()
            ),
            "execution_enabled": self.status == "active"
            and all(self.is_dispatchable(role) for role in self.roles.values()),
        }
