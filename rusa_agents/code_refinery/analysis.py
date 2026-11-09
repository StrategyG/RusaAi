"""Deterministic, read-only AST quality analysis for changed Python sources.

This module never imports or executes analyzed code. Scores are heuristics, not
proof of correctness; mandatory syntax/security checks remain separate gates.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

MAX_FILES = 2_000
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_SOURCE_BYTES = 100_000_000
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "dist", "build", ".tox", "__pycache__"}
WEIGHTS = {
    "type_safety": 0.15,
    "documentation": 0.10,
    "error_handling": 0.15,
    "code_quality": 0.15,
    "complexity": 0.10,
    "security": 0.20,
    "testability": 0.15,
}
SECRET_VALUE = re.compile(
    r"(?:sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)
SENSITIVE_ASSIGNMENT = re.compile(r"(?:api[_-]?key|secret|password|token|credential)", re.I)
RISKY_CALLS = {"open", "int", "float", "json.loads", "Path.read_text", "subprocess.run"}
DANGEROUS_CALLS = {
    "eval", "exec", "compile", "__import__", "os.system", "os.popen",
    "pickle.loads", "marshal.loads", "ctypes.CDLL", "ctypes.PyDLL",
    "multiprocessing.Process", "socket.socket",
}


@dataclass(frozen=True)
class Finding:
    severity: str
    category: str
    message: str
    path: str
    line: int | None = None
    recommendation: str | None = None


@dataclass(frozen=True)
class QualityReport:
    overall_score: float
    metrics: dict[str, float]
    files_analyzed: int
    test_files_present: bool
    syntax_valid: bool
    critical_security_findings: int
    blocking_findings: int
    min_score: float
    passed: bool
    not_applicable: bool
    findings: tuple[Finding, ...]

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["findings"] = [asdict(item) for item in self.findings]
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        payload["report_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return payload


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _function_nodes(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _annotation_score(functions: list[ast.FunctionDef | ast.AsyncFunctionDef]) -> float:
    total = 0
    annotated = 0
    for function in functions:
        total += 1
        annotated += function.returns is not None
        arguments = (
            list(function.args.posonlyargs)
            + list(function.args.args)
            + list(function.args.kwonlyargs)
        )
        if function.args.vararg is not None:
            arguments.append(function.args.vararg)
        if function.args.kwarg is not None:
            arguments.append(function.args.kwarg)
        for argument in arguments:
            if argument.arg not in {"self", "cls"}:
                total += 1
                annotated += argument.annotation is not None
    return 100.0 if total == 0 else 100.0 * annotated / total


def _documentation_score(tree: ast.AST) -> float:
    documented = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            documented.append(bool(ast.get_docstring(node)))
    return 100.0 if not documented else 100.0 * sum(documented) / len(documented)


def _complexity(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    score = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler)):
            score += 1
        elif isinstance(child, ast.BoolOp):
            score += max(0, len(child.values) - 1)
        elif isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            score += sum(len(generator.ifs) + 1 for generator in child.generators)
        elif isinstance(child, ast.Match):
            score += len(child.cases)
    return score


def _nesting_depth(node: ast.AST) -> int:
    maximum = 0

    def visit(current: ast.AST, depth: int) -> None:
        nonlocal maximum
        maximum = max(maximum, depth)
        nested = isinstance(
            current,
            (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try),
        )
        for child in ast.iter_child_nodes(current):
            visit(child, depth + (1 if nested else 0))

    visit(node, 0)
    return maximum


def _analyze_one(path: str, source: str) -> tuple[dict[str, float], list[Finding], bool]:
    findings: list[Finding] = []
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return (
            {key: 0.0 for key in WEIGHTS},
            [Finding("critical", "syntax", exc.msg, path, exc.lineno, "Fix the syntax error.")],
            False,
        )

    functions = _function_nodes(tree)
    metrics: dict[str, float] = {}
    source_path = PurePosixPath(path)
    is_test_source = (
        any(part in {"test", "tests"} for part in source_path.parts)
        or source_path.name.startswith("test_")
    )
    metrics["type_safety"] = 100.0 if is_test_source else _annotation_score(functions)
    metrics["documentation"] = 100.0 if is_test_source else _documentation_score(tree)

    risky_without_guard = 0
    for function in functions:
        calls = {
            _call_name(node.func)
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
        }
        if calls.intersection(RISKY_CALLS) and not any(
            isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try)))
            for node in ast.walk(function)
        ):
            risky_without_guard += 1
            findings.append(
                Finding(
                    "warning", "error_handling",
                    f"Function '{function.name}' uses an operation that may fail without a local try/except.",
                    path, function.lineno,
                    "Handle expected I/O, parsing, conversion, or subprocess errors explicitly.",
                )
            )
    metrics["error_handling"] = max(0.0, 100.0 - min(100.0, risky_without_guard * 10.0))

    named_nodes = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    naming_total = len(named_nodes)
    naming_ok = 0
    for node in named_nodes:
        valid = (
            re.fullmatch(r"[A-Z][A-Za-z0-9]*", node.name)
            if isinstance(node, ast.ClassDef)
            else re.fullmatch(r"__[A-Za-z0-9_]+__|[a-z_][a-z0-9_]*", node.name)
        )
        naming_ok += bool(valid)
        if not valid:
            findings.append(
                Finding("info", "naming", f"Name '{node.name}' does not follow the expected convention.",
                        path, node.lineno, "Use CapWords for classes and snake_case for functions.")
            )
    naming_score = 100.0 if naming_total == 0 else 100.0 * naming_ok / naming_total

    complexities = [_complexity(node) for node in functions]
    if not complexities:
        complexity_score = 100.0
    else:
        points = []
        for value in complexities:
            points.append(100.0 if value <= 5 else 80.0 if value <= 10 else 60.0 if value <= 15 else 30.0)
            if value > 10:
                function = next(node for node in functions if _complexity(node) == value)
                findings.append(
                    Finding("warning", "complexity",
                            f"Function '{function.name}' has cyclomatic complexity {value}.",
                            path, function.lineno, "Split branches into smaller, independently testable functions.")
                )
        complexity_score = sum(points) / len(points)

    metrics["complexity"] = complexity_score

    long_functions = [
        node for node in functions
        if (getattr(node, "end_lineno", node.lineno) - node.lineno + 1) > 50
    ]
    deep_functions = [node for node in functions if _nesting_depth(node) > 4]
    lines = source.splitlines()
    code_penalty = min(40.0, len(long_functions) * 5.0 + len(deep_functions) * 5.0)
    if len(lines) > 500:
        code_penalty += min(30.0, (len(lines) - 500) / 25.0)
    maintainability_score = max(0.0, 100.0 - code_penalty)
    metrics["code_quality"] = (naming_score + maintainability_score) / 2.0

    security_penalty = 0.0
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and SECRET_VALUE.search(node.value):
            findings.append(
                Finding("critical", "security", "Credential-like literal detected.", path,
                        getattr(node, "lineno", None),
                        "Remove the credential and rotate it if it was real.")
            )
            security_penalty += 100.0
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        dangerous = name in DANGEROUS_CALLS
        shell_true = name.startswith("subprocess.") and any(
            keyword.arg == "shell" and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        )
        if dangerous or shell_true:
            findings.append(
                Finding(
                    "error", "security",
                    f"Potentially dangerous dynamic execution or process primitive: {name}.",
                    path, node.lineno,
                    "Remove the primitive or isolate and explicitly justify it behind a reviewed boundary.",
                )
            )
            security_penalty += 25.0

    # Detect obvious credential assignments without printing the assigned value.
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            targets = [_call_name(target) for target in node.targets]
            if any(SENSITIVE_ASSIGNMENT.search(target) for target in targets) and len(node.value.value) >= 16:
                if SECRET_VALUE.search(node.value.value):
                    continue
                findings.append(
                    Finding("warning", "security", "Possible hard-coded secret assignment.", path,
                            node.lineno, "Load credentials from the approved secret manager.")
                )
                security_penalty += 10.0
    metrics["security"] = max(0.0, 100.0 - min(100.0, security_penalty))
    metrics["testability"] = 100.0  # Replaced with repository-level evidence below.
    return metrics, findings, True


def analyze_sources(
    sources: Mapping[str, str],
    *,
    test_files_present: bool = False,
    min_score: float = 85.0,
) -> QualityReport:
    """Analyze an explicit mapping of relative file paths to source text."""
    if not 0.0 <= min_score <= 100.0:
        raise ValueError("min_score must be between 0 and 100")
    if len(sources) > MAX_FILES:
        raise ValueError(f"At most {MAX_FILES} source files may be analyzed")

    findings: list[Finding] = []
    file_metrics: list[dict[str, float]] = []
    syntax_valid = True
    total_source_bytes = 0
    for raw_path, source in sorted(sources.items()):
        path = PurePosixPath(raw_path)
        if path.is_absolute() or ".." in path.parts or path.suffix.lower() != ".py":
            raise ValueError(f"Unsafe or unsupported source path: {raw_path}")
        if not isinstance(source, str):
            raise TypeError(f"Source for {raw_path} must be text")
        source_bytes = len(source.encode("utf-8"))
        total_source_bytes += source_bytes
        if total_source_bytes > MAX_TOTAL_SOURCE_BYTES:
            raise ValueError("Total source inventory exceeds the 100 MB analysis limit")
        if source_bytes > MAX_FILE_BYTES:
            findings.append(
                Finding("critical", "input", "Source file exceeds the 1 MB analysis limit.", raw_path)
            )
            syntax_valid = False
            continue
        metrics, file_findings, valid = _analyze_one(raw_path, source)
        file_metrics.append(metrics)
        findings.extend(file_findings)
        syntax_valid = syntax_valid and valid

    if not file_metrics:
        metrics = {key: 100.0 for key in WEIGHTS}
        metrics["testability"] = 100.0 if test_files_present else 60.0
        not_applicable = True
    else:
        metrics = {
            key: sum(item[key] for item in file_metrics) / len(file_metrics)
            for key in WEIGHTS
        }
        metrics["testability"] = 100.0 if test_files_present else 60.0
        not_applicable = False

    overall = sum(metrics[key] * weight for key, weight in WEIGHTS.items())
    critical = sum(item.severity == "critical" and item.category == "security" for item in findings)
    blocking = sum(
        item.severity in {"critical", "error"} and item.category == "security"
        for item in findings
    )
    if any(item.severity == "critical" and item.category == "syntax" for item in findings):
        syntax_valid = False
    passed = syntax_valid and blocking == 0 and overall >= min_score
    return QualityReport(
        overall_score=round(overall, 2),
        metrics={key: round(value, 2) for key, value in metrics.items()},
        files_analyzed=len(file_metrics),
        test_files_present=test_files_present,
        syntax_valid=syntax_valid,
        critical_security_findings=critical,
        blocking_findings=blocking,
        min_score=float(min_score),
        passed=passed,
        not_applicable=not_applicable,
        findings=tuple(findings[:500]),
    )


def analyze_workspace(
    workspace: str | Path,
    *,
    paths: list[str] | None = None,
    min_score: float = 85.0,
) -> QualityReport:
    """Read and analyze Python files without importing or executing them."""
    root = Path(workspace).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("workspace must be a directory")
    test_root = root / "tests"
    test_files_present = test_root.is_dir() and any(
        item.is_file() and not item.is_symlink()
        for item in test_root.rglob("test_*.py")
    )

    selected: list[Path] = []
    if paths is None:
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
            for name in files:
                if name.endswith(".py"):
                    selected.append(Path(current) / name)
                    if len(selected) > MAX_FILES:
                        raise ValueError(f"Workspace contains more than {MAX_FILES} Python files")
    else:
        for raw in paths:
            relative = PurePosixPath(raw)
            if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() != ".py":
                raise ValueError(f"Unsafe or unsupported source path: {raw}")
            candidate = root.joinpath(*relative.parts)
            if candidate.is_symlink():
                raise ValueError(f"Symlink source files are not analyzed: {raw}")
            if candidate.is_file():
                selected.append(candidate)

    sources: dict[str, str] = {}
    total_source_bytes = 0
    for candidate in sorted(set(selected)):
        if candidate.is_symlink():
            continue
        resolved = candidate.resolve(strict=True)
        if root not in resolved.parents:
            raise ValueError("Source file escapes the assigned workspace")
        source_size = resolved.stat().st_size
        total_source_bytes += source_size
        if total_source_bytes > MAX_TOTAL_SOURCE_BYTES:
            raise ValueError("Total source inventory exceeds the 100 MB analysis limit")
        if source_size > MAX_FILE_BYTES:
            raise ValueError(
                f"Source file exceeds the 1 MB analysis limit: {resolved.relative_to(root).as_posix()}"
            )
        sources[resolved.relative_to(root).as_posix()] = resolved.read_text(encoding="utf-8")
    return analyze_sources(
        sources, test_files_present=test_files_present, min_score=min_score
    )
