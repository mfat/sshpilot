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
* output is captured and capped;
* every subprocess result includes an explicit ``success`` boolean.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
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
        text[:max_bytes]
        + "\n...[output truncated by sshpilot-mcp-dev]..."
    )


def _output_text(value) -> str:
    """Normalize subprocess output, including timeout exception payloads."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _subprocess_env() -> dict[str, str]:
    """Return a test/check environment without MCP server control inputs."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("SSHPILOT_MCP_")
    }
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _run(
    argv: List[str],
    timeout: float = EXECUTION_TIMEOUT_SECONDS,
    cwd: Optional[Path] = None,
) -> dict:
    """Run fixed *argv* with captured, capped output.

    ``cwd`` must be the repository root for every repository-relative argv
    (pytest targets are relative); callers pass ``scope.root`` so results do
    not depend on the MCP client's working directory.
    """
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_subprocess_env(),
            cwd=str(cwd) if cwd is not None else None,
        )
    except subprocess.TimeoutExpired as error:
        return {
            "success": False,
            "returncode": None,
            "stdout": _cap(_output_text(error.stdout)),
            "stderr": _cap(_output_text(error.stderr)),
            "timed_out": True,
            "duration_seconds": round(time.monotonic() - started, 3),
        }

    return {
        "success": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": _cap(_output_text(proc.stdout)),
        "stderr": _cap(_output_text(proc.stderr)),
        "timed_out": False,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def run_tests(scope: RepoScope, suite: str, path: Optional[str] = None) -> dict:
    """Run a selected pytest suite (architecture/api/core/daemon/mcp/cli).

    ``path`` optionally targets a single repository-relative test file below
    ``tests/`` so a focused run can stay fast. The result includes ``success``
    and ``timed_out`` status fields.
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
    result = _run([_python(), "-m", "pytest", "-q", target], cwd=scope.root)
    result["command"] = f"python -m pytest -q {target}"
    result["suite"] = suite
    return result


#: Upper bound on test files one focused run may select.
MAX_FOCUSED_FILES = 25


def _validate_test_file(scope: RepoScope, path: str) -> str:
    if not isinstance(path, str) or not path.startswith("tests/") or not path.endswith(".py"):
        raise ExecutionError(f"focused test targets must be tests/**.py files: {path!r}")
    if ".." in path.split("/") or path.startswith("-") or "\x00" in path:
        raise ExecutionError(f"invalid test path: {path!r}")
    resolved = scope.resolve(path)
    if scope.resolve("tests") not in resolved.parents or not resolved.is_file():
        raise ExecutionError(f"test file not found under tests/: {path!r}")
    return path


def _junit_outcomes(xml_path: Path) -> Optional[dict]:
    """Per-test outcomes from a pytest JUnit XML report (None if absent)."""
    import xml.etree.ElementTree as ElementTree

    if not xml_path.is_file():
        return None
    try:
        root = ElementTree.parse(str(xml_path)).getroot()
    except ElementTree.ParseError:
        return None
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    failures: List[dict] = []
    skipped: List[str] = []
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        test_id = f"{classname}::{name}" if classname else name
        outcome = "passed"
        detail = ""
        for child in case:
            if child.tag in ("failure", "error"):
                outcome = "failed" if child.tag == "failure" else "errors"
                detail = (child.get("message") or child.text or "")[:300]
                break
            if child.tag == "skipped":
                outcome = "skipped"
                detail = (child.get("message") or "")[:200]
        counts[outcome] += 1
        if outcome in ("failed", "errors") and len(failures) < 10:
            failures.append({"test": test_id, "outcome": outcome, "message": detail})
        elif outcome == "skipped" and len(skipped) < 10:
            skipped.append(test_id)
    return {"counts": counts, "failures": failures, "skipped": skipped}


def run_test_files(
    scope: RepoScope,
    paths: List[str],
    timeout: float = EXECUTION_TIMEOUT_SECONDS,
) -> dict:
    """Run selected test files in one pytest process with per-test outcomes.

    Targets must be existing ``tests/**.py`` files. pytest runs in the
    repository root with its cache provider disabled (nothing is written into
    the checkout) and a JUnit report in a private temporary directory, so
    passed / failed / skipped are counted from pytest itself, not inferred.
    """
    import tempfile

    if not paths:
        raise ExecutionError("no test files selected")
    if len(paths) > MAX_FOCUSED_FILES:
        raise ExecutionError(f"at most {MAX_FOCUSED_FILES} test files per focused run")
    targets = [_validate_test_file(scope, path) for path in dict.fromkeys(paths)]
    with tempfile.TemporaryDirectory(prefix="sshpilot-mcp-junit-") as directory:
        report = Path(directory) / "junit.xml"
        argv = [
            _python(),
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={report}",
            "-o",
            "junit_family=xunit2",
            "--",
            *targets,
        ]
        result = _run(argv, timeout=timeout, cwd=scope.root)
        outcomes = _junit_outcomes(report)
    result["command"] = "python -m pytest -q -p no:cacheprovider -- " + " ".join(targets)
    result["targets"] = targets
    result["outcomes"] = outcomes
    return result


def check_api_artifacts(scope: RepoScope) -> dict:
    """Validate generated API artifacts; result includes ``success``."""
    script = scope.resolve("scripts/generate_api_artifacts.py")
    if not script.is_file():
        raise ExecutionError("scripts/generate_api_artifacts.py not found")
    result = _run([_python(), str(script), "--check"], cwd=scope.root)
    result["command"] = "python scripts/generate_api_artifacts.py --check"
    return result


def run_lint(scope: RepoScope, path: Optional[str] = None) -> dict:
    """Run ruff over ``src/`` or an allowed path; result includes ``success``."""
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
    result = _run([_python(), "-m", "ruff", "check", *targets], cwd=scope.root)
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
