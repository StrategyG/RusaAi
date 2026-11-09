"""Alias-aware static detection of dangerous Python execution and network primitives."""
from __future__ import annotations

import ast

DANGEROUS_CALLS = frozenset({
    "eval", "exec", "compile", "__import__", "builtins.eval", "builtins.exec",
    "builtins.compile", "builtins.__import__",
    "importlib.import_module", "runpy.run_module", "runpy.run_path",
    "os.system", "os.popen", "os.execv", "os.execve", "os.execl",
    "os.execlp", "os.execle", "os.execvp", "os.execvpe",
    "os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnlpe",
    "os.spawnv", "os.spawnve", "os.spawnvp", "os.spawnvpe",
    "subprocess.Popen", "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell", "pickle.loads", "marshal.loads",
    "ctypes.CDLL", "ctypes.PyDLL", "multiprocessing.Process",
    "socket.socket", "socket.create_connection", "urllib.request.urlopen",
    "requests.request", "requests.get", "requests.post",
    "httpx.request", "httpx.get", "httpx.post",
})
DANGEROUS_SUBPROCESS_METHODS = frozenset({
    "subprocess.run", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "subprocess.getoutput",
    "subprocess.getstatusoutput",
})
WILDCARD_SENSITIVE_MODULES = frozenset({
    "os", "subprocess", "builtins", "importlib", "runpy",
    "pickle", "marshal", "ctypes", "multiprocessing", "socket",
})


def _import_aliases(tree: ast.AST) -> tuple[dict[str, str], set[str]]:
    aliases: dict[str, str] = {}
    wildcard_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                bound = item.asname or item.name.split(".", 1)[0]
                aliases[bound] = item.name if item.asname else item.name.split(".", 1)[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                if item.name == "*":
                    if node.module in WILDCARD_SENSITIVE_MODULES:
                        wildcard_modules.add(node.module)
                    continue
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    return aliases, wildcard_modules


def _canonical_name(
    node: ast.AST,
    aliases: dict[str, str],
    wildcard_modules: set[str],
) -> str:
    if isinstance(node, ast.Name):
        if node.id in aliases:
            return aliases[node.id]
        matches = [
            f"{module}.{node.id}"
            for module in wildcard_modules
            if f"{module}.{node.id}" in DANGEROUS_CALLS
            or f"{module}.{node.id}" in DANGEROUS_SUBPROCESS_METHODS
        ]
        return matches[0] if len(matches) == 1 else node.id
    if isinstance(node, ast.Attribute):
        prefix = _canonical_name(node.value, aliases, wildcard_modules)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def dangerous_calls(tree: ast.AST) -> list[tuple[str, int | None]]:
    """Return canonical dangerous call names and line numbers, resolving import aliases."""
    aliases, wildcard_modules = _import_aliases(tree)
    findings: list[tuple[str, int | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _canonical_name(node.func, aliases, wildcard_modules)
        dangerous = name in DANGEROUS_CALLS
        shell_enabled = name in DANGEROUS_SUBPROCESS_METHODS and any(
            keyword.arg == "shell"
            and not (
                isinstance(keyword.value, ast.Constant)
                and keyword.value.value is False
            )
            for keyword in node.keywords
        )
        if name == "getattr" and len(node.args) >= 2:
            attribute = node.args[1]
            if isinstance(attribute, ast.Constant) and isinstance(attribute.value, str):
                target = _canonical_name(node.args[0], aliases, wildcard_modules)
                dangerous = f"{target}.{attribute.value}" in DANGEROUS_CALLS
        if dangerous or shell_enabled:
            findings.append((name, getattr(node, "lineno", None)))
    return sorted(set(findings), key=lambda item: (item[1] or 0, item[0]))
