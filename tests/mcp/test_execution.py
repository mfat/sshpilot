"""Tests for dev-MCP controlled local execution."""

from pathlib import Path

import pytest

from sshpilot.mcp._scope import RepoScope
from sshpilot.mcp.dev import execution


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def repo():
    return RepoScope(REPO_ROOT)


def test_run_tests_smallest_suite(repo):
    result = execution.run_tests(
        repo, "mcp", path="tests/mcp/test_repo_scope.py"
    )
    assert result["returncode"] == 0
    assert result["suite"] == "mcp"
    assert "pytest" in result["command"]
    assert result["timed_out"] is False


def test_run_tests_rejects_not_allowlisted(repo):
    with pytest.raises(execution.ExecutionError):
        execution.run_tests(repo, "datadir")
    with pytest.raises(execution.ExecutionError):
        execution.run_tests(repo, "mcp", path="tests/../AGENTS.md")


def test_run_tests_rejects_missing_suite(repo):
    with pytest.raises(execution.ExecutionError):
        execution.run_tests(repo, "does_not_exist")


def test_check_api_artifacts(repo):
    result = execution.check_api_artifacts(repo)
    assert result["returncode"] == 0
    assert "generate_api_artifacts.py" in result["command"]


def test_run_lint_default(repo):
    result = execution.run_lint(repo)
    assert result["command"] == "python -m ruff check <targets>"
    assert "returncode" in result


def test_run_lint_path_allowlist(repo):
    result = execution.run_lint(repo, "src/sshpilot/mcp")
    assert "returncode" in result
    with pytest.raises(execution.ExecutionError):
        execution.run_lint(repo, "docs")
    with pytest.raises(execution.ExecutionError):
        execution.run_lint(repo, "../outside")


def test_run_controlled_dispatch(repo):
    assert execution.run_controlled(repo, "lint")["returncode"] in (0, 1)
    with pytest.raises(execution.ExecutionError):
        execution.run_controlled(repo, "nuke")
    with pytest.raises(execution.ExecutionError):
        execution.run_controlled(repo, "pytest", suite="datadir")