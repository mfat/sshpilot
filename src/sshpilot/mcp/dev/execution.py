"""Controlled local execution for the dev MCP server.

Step 5 of the MCP plan: selected pytest suites, API-artifact validation, and
lint — always through a strict allowlist. This is never an arbitrary shell.

Invariants:

* every command is fixed argv against the ``PYTHON`` that is running this
  server (no ``PATH``-resolved executables, no shell, no option injection),
* test paths must resolve inside the repository and match an allowlisted
  pytest suite,
* ruff runs only over repository paths under ``src/``, ``tests/``, or
  ``scripts/``,
* the API-artifact check is exactly ``generate_api_artifacts.py --check``,
* output is captured and capped.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import List, Optional

from .._scope import RepoScope

#: Short names for pytest suites that may be selected (repository-relative).
ALLOWED_TEST_SUITES = frozenset(
    {
        "architecture",
        "api",
        "core",
        "daemon",
        "mcp",
        "cli",
        "integration",
    }
)

#: Ruff targets are restricted to these repository prefixes.
ALLOWED_LINT_PREFIXES = ("src/", "tests/", "scripts/")

#: Anything longer than this is assumed to be wedged/hung.
EXECUTION_TIMEOUT_SECONDS = 180
#: Cap on captured stdout.
MAX_OUTPUT_BYTES = 256 * 1024


class ExecutionError(RuntimeError):
    """Raised when a controlled execution cannot be run or fails validation."""


def _python() -> str:
    return sys.executable


def _resolve_suite(scope: RepoScope, suite: str) -> str:
    """Validate and resolve a pytest suite to a repository-relative path."""
    if suite not in ALLOWED_TEST_SUITES:
        raise ExecutionError(
            f"not an allowed test suite: {suite!r}; allowed: "
            + ", ".join(sorted(ALLOWED_TEST_SUITES))
        )
    path = f"tests/{suite}"
    resolved = scope.resolve(path)
    if not resolved.is_dir():
        raise ExecutionError(f"test suite directory not found: {path}")
    return path


def _cap(text: str, max_bytes: int = MAX_OUTPUT_BYTES) -> str:
    if len(text.encode("utf-8", errors="replace")) <= max_bytes:
        return text
    return (
        text[:MAX_OUTPUT_BYTES]
        + "\n...[output truncated by sshpilot-mcp-dev]..."
    )


def _run(argv: List[str], timeout: float = EXECUTION_TIMEOUT_SECONDS) -> dict:
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    return {
        "returncode": proc.returncode,
        "stdout": _cap(proc.stdout or ""),
        "stderr": _cap(proc.stderr or ""),
        "timed_out": False,
    }


def run_tests(scope: RepoScope, suite: str, path: Optional[str] = None) -> dict:
    """Run a selected pytest suite (architecture/api/core/daemon/mcp/cli).

    ``path`` optionally targets a single repository-relative test file below
    ``tests/`` so a focused run can stay fast.
    """
    target = _resolve_suite(scope, suite)
    if path is not None:
        if not path.startswith("tests/"):
            raise ExecutionError("test path must be under tests/")
        if ".." in path.split("/"):
            raise ExecutionError("test path must not traverse directories")
        resolved = scope.resolve(path)
        tests_root = scope.resolve("tests")
        if tests_root not in resolved.parents:
            raise ExecutionError("test path must stay under tests/")
        if not resolved.is_file():
            raise ExecutionError(f"test file not found: {path}")
        target = path
    if not scope.is_within(scope.root / target):
        raise ExecutionError("test suite resolves outside the repository")
    result = _run([_python(), "-m", "pytest", "-q", target])
    result["command"] = f"python -m pytest -q {target}"
    result["suite"] = suite
    return result


def check_api_artifacts(scope: RepoScope) -> dict:
    """Validate the generated API artifacts are current (read-only check)."""
    script = scope.resolve("scripts/generate_api_artifacts.py")
    if not script.is_file():
        raise ExecutionError("scripts/generate_api_artifacts.py not found")
    result = _run([_python(), str(script), "--check"])
    result["command"] = "python scripts/generate_api_artifacts.py --check"
    return result


def run_lint(scope: RepoScope, path: Optional[str] = None) -> dict:
    """Run ruff over ``src/`` (default) or an allowed repository path."""
    if path is None:
        targets = [str(scope.root / "src")]
    else:
        if not path.startswith(ALLOWED_LINT_PREFIXES):
            raise ExecutionError(
                f"lint path must start with one of {sorted(ALLOWED_LINT_PREFIXES)}"
            )
        resolved = scope.resolve(path)
        if not (resolved.is_dir() or resolved.is_file()):
            raise ExecutionError(f"lint target not found: {path}")
        targets = [str(resolved)]
    result = _run([_python(), "-m", "ruff", "check", *targets])
    result["command"] = "python -m ruff check <targets>"
    return result


def run_controlled(scope: RepoScope, action: str, **params) -> dict:
    """Dispatch a controlled action by name (never arbitrary shells)."""
    if action == "pytest":
        return run_tests(scope, params["suite"])
    if action == "check-api-artifacts":
        return check_api_artifacts(scope)
    if action == "lint":
        return run_lint(scope, params.get("path"))
    raise ExecutionError(f"unknown action: {action!r}")