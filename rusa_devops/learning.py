"""Strict DevOps-agent SFT dataset validation; action contract is proposal-only."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

ALLOWED_ACTIONS = {
    "devops_get_telemetry": {"metric_name", "resource_id", "limit"},
    "devops_get_model_run": {"run_id"},
    "devops_list_incidents": {"limit"},
    "search_knowledge": {"query", "collection", "limit"},
    "propose_capacity_recommendation": {"predicted_values", "current_capacity"},
}
REQUIRED_ACTION_ARGUMENTS = {
    "devops_get_telemetry": {"metric_name", "resource_id"},
    "devops_get_model_run": {"run_id"},
    "search_knowledge": {"query"},
    "propose_capacity_recommendation": {"predicted_values", "current_capacity"},
}


class DevOpsDatasetError(ValueError):
    pass


def validate_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != {"id", "agent_role", "messages", "response"}:
        raise DevOpsDatasetError("record must contain id, agent_role, messages, and response only")
    if not isinstance(record["id"], str) or not record["id"].strip():
        raise DevOpsDatasetError("record id is invalid")
    if record["agent_role"] != "devops":
        raise DevOpsDatasetError("agent_role must be 'devops'")
    messages = record["messages"]
    if not isinstance(messages, list) or len(messages) < 2:
        raise DevOpsDatasetError("messages must contain at least system and user")
    for item in messages:
        if not isinstance(item, dict) or set(item) != {"role", "content"}:
            raise DevOpsDatasetError("each message must contain role and content only")
        if item["role"] not in {"system", "user", "assistant"} or not isinstance(item["content"], str) or not item["content"].strip():
            raise DevOpsDatasetError("message role/content is invalid")
    if messages[0]["role"] != "system" or messages[-1]["role"] != "user":
        raise DevOpsDatasetError("messages must start with system and end with user")
    response = record["response"]
    if not isinstance(response, dict) or set(response) - {"message", "actions", "done", "blocked", "blockers"}:
        raise DevOpsDatasetError("response contains unsupported fields")
    if not isinstance(response.get("message"), str) or not response["message"].strip():
        raise DevOpsDatasetError("response.message is required")
    if type(response.get("done")) is not bool or not isinstance(response.get("actions"), list):
        raise DevOpsDatasetError("response requires boolean done and an actions array")
    if len(response["actions"]) > 8:
        raise DevOpsDatasetError("response exceeds the eight-action runtime limit")
    blocked = response.get("blocked", False)
    if type(blocked) is not bool:
        raise DevOpsDatasetError("response.blocked must be boolean")
    blockers = response.get("blockers", [])
    if not isinstance(blockers, list) or any(not isinstance(item, str) for item in blockers):
        raise DevOpsDatasetError("response.blockers must be an array of strings")
    if blocked and (not response["done"] or response["actions"]):
        raise DevOpsDatasetError("blocked responses must be terminal and contain no actions")
    if not response["done"] and not response["actions"] and not blocked:
        raise DevOpsDatasetError("non-terminal responses require an action")
    for index, action in enumerate(response["actions"]):
        if not isinstance(action, dict) or set(action) != {"name", "arguments"}:
            raise DevOpsDatasetError(f"actions[{index}] is invalid")
        name, arguments = action["name"], action["arguments"]
        if name not in ALLOWED_ACTIONS:
            raise DevOpsDatasetError(f"action {name!r} is not allowed")
        if not isinstance(arguments, dict) or set(arguments) - ALLOWED_ACTIONS[name]:
            raise DevOpsDatasetError(f"arguments for {name} are invalid")
        if not REQUIRED_ACTION_ARGUMENTS.get(name, set()).issubset(arguments):
            raise DevOpsDatasetError(f"arguments for {name} are incomplete")
        if name in {"devops_get_telemetry", "devops_get_model_run"}:
            required_string = "run_id" if name == "devops_get_model_run" else "metric_name"
            if not isinstance(arguments.get(required_string), str) or not arguments[required_string].strip():
                raise DevOpsDatasetError(f"{name} requires a non-empty {required_string}")
            if name == "devops_get_telemetry":
                if not isinstance(arguments.get("resource_id"), str) or not arguments["resource_id"].strip():
                    raise DevOpsDatasetError("devops_get_telemetry requires resource_id")
                if "limit" in arguments and (type(arguments["limit"]) is not int or not 1 <= arguments["limit"] <= 200):
                    raise DevOpsDatasetError("telemetry limit must be between 1 and 200")
        elif name == "devops_list_incidents":
            if "limit" in arguments and (type(arguments["limit"]) is not int or not 1 <= arguments["limit"] <= 200):
                raise DevOpsDatasetError("incident limit must be between 1 and 200")
        elif name == "search_knowledge":
            if not isinstance(arguments.get("query"), str) or not arguments["query"].strip() or len(arguments["query"]) > 2000:
                raise DevOpsDatasetError("search_knowledge query is invalid")
            if "limit" in arguments and (type(arguments["limit"]) is not int or not 1 <= arguments["limit"] <= 10):
                raise DevOpsDatasetError("search_knowledge limit must be between 1 and 10")
        elif name == "propose_capacity_recommendation":
            values = arguments.get("predicted_values")
            capacity = arguments.get("current_capacity")
            if not isinstance(values, list) or not 1 <= len(values) <= 10080:
                raise DevOpsDatasetError("predicted_values must contain 1-10080 values")
            if any(type(value) not in {int, float} or not math.isfinite(float(value)) for value in values):
                raise DevOpsDatasetError("predicted_values must contain finite numbers")
            if type(capacity) not in {int, float} or not math.isfinite(float(capacity)) or capacity <= 0:
                raise DevOpsDatasetError("current_capacity must be a positive finite number")
    return record


def fingerprint(record: dict[str, Any]) -> str:
    content = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    records = []
    ids: set[str] = set()
    hashes: set[str] = set()
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = validate_record(json.loads(line))
        except (json.JSONDecodeError, DevOpsDatasetError) as exc:
            raise DevOpsDatasetError(f"{source}:{line_number}: {exc}") from exc
        digest = fingerprint(record)
        if record["id"] in ids or digest in hashes:
            raise DevOpsDatasetError(f"{source}:{line_number}: duplicate ID or content")
        ids.add(record["id"])
        hashes.add(digest)
        records.append(record)
    if not records:
        raise DevOpsDatasetError(f"dataset is empty: {source}")
    return records


def validate_splits(train: list[dict[str, Any]], evaluation: list[dict[str, Any]], *, require_minimum: bool = True) -> None:
    train_ids = {item["id"] for item in train}
    eval_ids = {item["id"] for item in evaluation}
    train_hashes = {fingerprint(item) for item in train}
    eval_hashes = {fingerprint(item) for item in evaluation}
    if train_ids & eval_ids or train_hashes & eval_hashes:
        raise DevOpsDatasetError("train/eval split leakage detected")
    for label, records in (("train", train), ("eval", evaluation)):
        if require_minimum and len(records) < (100 if label == "train" else 30):
            raise DevOpsDatasetError(f"{label} split does not meet the minimum example count")
        blocked = sum(bool(item["response"].get("blocked", False)) for item in records)
        if label == "eval" and len(records) > 1 and (blocked == 0 or blocked == len(records)):
            raise DevOpsDatasetError("evaluation split must contain blocked and unblocked examples")
