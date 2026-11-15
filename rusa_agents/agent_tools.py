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
        "search_external_rag",
        "list_mcp_tools", "call_mcp_tool",
    }),
    WorkerRole.ARCHITECT: frozenset({
        "list_files", "read_file", "git_diff",
        "search_knowledge", "get_document", "list_collections",
        "search_external_rag",
        "list_mcp_tools", "call_mcp_tool",
    }),
    WorkerRole.DEVELOPER: frozenset({
        "list_files", "read_file", "write_file", "git_diff",
        "search_knowledge", "get_document", "list_collections",
        "search_external_rag",
        "list_mcp_tools", "call_mcp_tool",
    }),
    WorkerRole.DEBUGGER: frozenset({
        "list_files", "read_file", "run_check", "git_diff",
        "search_knowledge", "get_document", "list_collections",
        "search_external_rag",
        "list_mcp_tools", "call_mcp_tool",
    }),
    WorkerRole.TESTER: frozenset({
        "list_files", "read_file", "run_check", "git_diff",
        "search_knowledge", "get_document", "list_collections",
        "search_external_rag",
        "list_mcp_tools", "call_mcp_tool",
    }),
    WorkerRole.SECURITY: frozenset({
        "list_files", "read_file", "run_check", "git_diff",
        "search_knowledge", "get_document", "list_collections",
        "search_external_rag",
        "list_mcp_tools", "call_mcp_tool",
    }),
}

ROLE_CHECK_PROFILES = {
    WorkerRole.TESTER: frozenset({"pytest"}),
    WorkerRole.DEBUGGER: frozenset({"pytest", "compile", "ruff"}),
    WorkerRole.SECURITY: frozenset({"bandit", "ruff", "compile"}),
}
