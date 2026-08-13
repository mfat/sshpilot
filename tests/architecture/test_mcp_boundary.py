"""Architecture guards for the MCP servers (src/sshpilot/mcp/**).

The MCP package is a headless tool layer: neither a GTK frontend nor an
SSH/daemon service, so it is excluded from the GTK frontend boundary scans
(see ``_INTERNAL`` / ``INTERNAL``). These tests codify the MCP-specific
invariants from ``docs/mcp/AGENTS.md`` instead:

* MCP modules stay GTK/GI-free and frontend-free; they may only reach
  ``sshpilot.api`` (the daemon client surface) and the ``mcp`` SDK.
* The dev server runs read-only Git inspection only, through a strict
  whitelist, and is the only dev module allowed to touch ``subprocess``.
* The dev server never spawns SSH binaries or touches secrets, and exposes
  typed tools rather than a free-form action dispatch.
"""

import ast
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src" / "sshpilot" / "mcp"

#: External dependency roots an MCP module may import (own package resolved
#: to ``sshpilot.mcp``; ``sshpilot.api`` is reserved for the runtime server's
#: DaemonClient surface).
ALLOWED_TOP_LEVEL = {"sshpilot.mcp", "sshpilot.api", "mcp"}

#: Read-only Git commands the dev MCP is allowed to run. Authoritative
#: whitelist; the server must not grow mutation commands.
EXPECTED_READONLY_GIT = frozenset({"status", "log", "diff", "rev-parse"})

#: Git verbs that mutating a repository; none may appear in the whitelist.
GIT_MUTATION_TOKENS = frozenset(
    {
        "commit",
        "push",
        "pull",
        "reset",
        "checkout",
        "merge",
        "rebase",
        "stash",
        "clean",
        "restore",
        "rm",
        "mv",
        "tag",
        "branch",
        "fetch",
        "clone",
        "cherry-pick",
        "apply",
        "revert",
    }
)

EXPECTED_TOOLS = frozenset(
    {
        "repo_info",
        "read_text_file",
        "list_directory",
        "search_source",
        "find_symbol",
        "git_status",
        "git_log",
        "git_diff",
        "find_tests",
    }
)


def _modules() -> list[tuple[Path, str]]:
    found = []
    for path in sorted(SOURCE.rglob("*.py")):
        rel = "/".join(path.relative_to(SOURCE).parts)
        if "__pycache__" in rel:
            continue
        found.append((path, rel))
    return found


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _resolve_imports(path: Path, rel: str) -> list[str]:
    parts = rel.split("/")[:-1]
    pkg = ("sshpilot", "mcp") + tuple(parts)
    resolved = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            resolved.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg[: max(1, len(pkg) - (node.level - 1))]
                if node.module:
                    resolved.append(".".join(base) + "." + node.module)
                else:
                    resolved.append(".".join(base))
            else:
                resolved.append(node.module or "")
    return resolved


def _stdlib_names() -> frozenset:
    if hasattr(sys, "stdlib_module_names"):
        return frozenset(sys.stdlib_module_names)
    return frozenset(
        {
            "os",
            "sys",
            "subprocess",
            "re",
            "ast",
            "pathlib",
            "typing",
            "io",
            "json",
            "dataclasses",
            "enum",
            "collections",
            "functools",
            "itertools",
            "importlib",
            "contextlib",
            "tempfile",
        }
    )


def test_mcp_modules_are_gtk_and_frontend_free():
    stdlib = _stdlib_names()
    violations = []
    for path, rel in _modules():
        for full in _resolve_imports(path, rel):
            if full.startswith("gi") or full.startswith("gi.repository"):
                violations.append(f"{rel}: GTK/GI import {full}")
                continue
            if full.startswith("sshpilot"):
                package = ".".join(full.split(".")[:2])
                if package not in ALLOWED_TOP_LEVEL:
                    violations.append(
                        f"{rel}: imports frontend/service package {full}"
                    )
            elif full.split(".")[0] not in stdlib and not full.startswith("mcp."):
                violations.append(f"{rel}: import outside allowlist {full}")
    assert not violations, (
        "MCP module leaked a frontend/GTK/service dependency:\n"
        + "\n".join(violations)
    )


def test_only_dev_execution_modules_may_use_subprocess():
    """Only the git helper and the controlled-execution module may use
    subprocess. Everything else in the dev MCP must stay headless; SSH/SCP/SFTP
    work goes through DaemonClient in the runtime server."""
    allowed = {"dev/_git.py", "dev/execution.py"}
    violations = []
    for path, rel in _modules():
        if rel in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for marker in ("import subprocess", "from subprocess"):
            if marker in text:
                violations.append(f"{rel}: {marker}")
    assert not violations, (
        "only the read-only git helper (dev/_git.py) and the controlled "
        "execution module (dev/execution.py) may use subprocess in the dev MCP"
    )


def test_execution_allowlists_are_authoritative():
    """Controlled execution stays argv-only, allowlisted, and non-arbitrary."""
    from sshpilot.mcp.dev import execution

    assert frozenset(execution.ALLOWED_TEST_SUITES) == frozenset(
        {
            "architecture",
            "api",
            "core",
            "daemon",
            "mcp",
            "cli",
            "integration",
        }
    ), "the pytest suite allowlist drifted; it must stay explicit."
    assert frozenset(execution.ALLOWED_LINT_PREFIXES) == frozenset(
        {"src/", "tests/", "scripts/"}
    ), "the lint path allowlist drifted."

    script = (SOURCE / "dev" / "execution.py").read_text(encoding="utf-8")
    assert "shell=True" not in script, "controlled execution must never use a shell"
    assert "os.exec" not in script and "exec(" not in script.replace("exec(", "exec (")
    for banned in ("curl", "wget", "apt", "ssh", "scp", "sftp", "git"):
        assert f'"{banned}"' not in script and f"'{banned}'" not in script, (
            f"controlled execution must not invoke {banned}"
        )


def test_readonly_git_whitelist_is_authoritative():
    from sshpilot.mcp.dev._git import _SAFE_GIT_ARGS

    assert frozenset(_SAFE_GIT_ARGS) == EXPECTED_READONLY_GIT, (
        "the Git whitelist drifted; it must stay exactly the read-only commands."
    )


def test_git_whitelist_has_no_mutation_and_no_shell():
    from sshpilot.mcp.dev._git import _SAFE_GIT_ARGS

    assert not (frozenset(_SAFE_GIT_ARGS) & GIT_MUTATION_TOKENS)
    script = (SOURCE / "dev" / "_git.py").read_text(encoding="utf-8")
    assert "shell=True" not in script, "git inspection must never use a shell"


def test_dev_server_exposes_typed_tools():
    """Dev tools are typed per-operation tools, never a free-form action."""
    decorated = set()
    for node in ast.walk(_tree(SOURCE / "dev" / "server.py")):
        if isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                name = target.attr if isinstance(target, ast.Attribute) else ""
                if name == "tool":
                    decorated.add(node.name)
    assert EXPECTED_TOOLS <= decorated
    assert not (
        {name for name in decorated}
        & {"run", "execute_action", "dispatch", "sftp_action"}
    )