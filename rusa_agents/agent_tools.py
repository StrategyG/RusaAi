"""Shared, deterministic tool permissions for autonomous workers.

Keep this registry as the single source of truth for model prompts, training-data
validation, and runtime worker authorization. The tool gateway remains the final
enforcement point for every invocation.
"""
from __future__ import annotations

from .contracts import WorkerRole

ROLE_TOOL_ALLOWLIST = {
    WorkerRole.ANALYST: frozenset({
        "list_files", "read_file", "git_diff",
        "search_knowledge", "get_document", "list_collections",
    }),
    WorkerRole.ARCHITECT: frozenset({
        "list_files", "read_file", "git_diff",
        "search_knowledge", "get_document", "list_collections",
    }),
    WorkerRole.DEVELOPER: frozenset({
        "list_files", "read_file", "write_file", "git_diff",
        "search_knowledge", "get_document", "list_collections",
    }),
    WorkerRole.DEBUGGER: frozenset({
        "list_files", "read_file", "run_check", "git_diff",
        "search_knowledge", "get_document", "list_collections",
    }),
    WorkerRole.TESTER: frozenset({
        "list_files", "read_file", "run_check", "git_diff",
        "search_knowledge", "get_document", "list_collections",
    }),
    WorkerRole.SECURITY: frozenset({
        "list_files", "read_file", "run_check", "git_diff",
        "search_knowledge", "get_document", "list_collections",
    }),
}

ROLE_PERMISSION_ALLOWLIST = {
    WorkerRole.ANALYST: frozenset({"repo_read"}),
    WorkerRole.ARCHITECT: frozenset({"repo_read"}),
    WorkerRole.DEVELOPER: frozenset({"repo_read", "workspace_write"}),
    WorkerRole.DEBUGGER: frozenset({"repo_read", "test_run"}),
    WorkerRole.TESTER: frozenset({"repo_read", "test_run"}),
    WorkerRole.SECURITY: frozenset({"repo_read", "test_run", "audit_read"}),
}

ROLE_CHECK_PROFILES = {
    WorkerRole.TESTER: frozenset({"pytest"}),
    WorkerRole.DEBUGGER: frozenset({"pytest", "compile", "ruff"}),
    WorkerRole.SECURITY: frozenset({"bandit", "ruff", "compile"}),
}
