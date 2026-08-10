"""Unit tests for dev-MCP repository intelligence (search, symbols, git)."""

import subprocess
from pathlib import Path

import pytest

from sshpilot.mcp._scope import RepoScope, ScopeError
from sshpilot.mcp.dev import search, symbols, test_discovery
from sshpilot.mcp.dev._git import GitError, GitInspector


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "README.md").write_text("hello pilot\nsearch me\n")
    (root / "src").mkdir()
    (root / "src" / "widget.py").write_text(
        "class Widget:\n"
        "    def spin(self):\n"
        "        return 1\n"
        "\n"
        "def helper(value):\n"
        "    return value\n"
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_widget.py").write_text("def test_spin():\n    pass\n")
    (root / "venv").mkdir()
    (root / "venv" / "junk.py").write_text("def should_not_scan():\n    pass\n")
    (root / "data.bin").write_bytes(b"\x00\x01\x02binary\x00")
    return root


def test_list_directory(repo):
    scope = RepoScope(repo)
    entries = {entry["name"]: entry for entry in scope.list_directory(".")}
    assert set(entries) == {".git", "README.md", "data.bin", "src", "tests", "venv"}
    assert entries["src"]["is_dir"] is True
    assert entries["README.md"]["is_file"] is True


def test_list_directory_confined(repo):
    scope = RepoScope(repo)
    with pytest.raises(ScopeError):
        scope.list_directory("../outside")
    with pytest.raises(ScopeError):
        scope.list_directory("README.md")  # file, not a directory


def test_iter_files_skips_artifacts_and_confines(repo, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1")
    (repo / "link.py").symlink_to(outside)
    scope = RepoScope(repo)
    files = {str(relative) for relative in scope.iter_files()}
    assert "src/widget.py" in files
    assert "tests/test_widget.py" in files
    assert "venv/junk.py" not in files  # artifact directory skipped
    assert "link.py" not in files  # symlink escape rejected
    assert "data.bin" in files  # binary files are still listed


def test_iter_text_within_skips_binary(repo):
    scope = RepoScope(repo)
    texts = {str(key): value for key, value in scope.iter_text_within()}
    assert "README.md" in texts
    assert "src/widget.py" in texts
    assert "data.bin" not in texts
    assert "venv/junk.py" not in texts


def test_search_finds_matches(repo):
    scope = RepoScope(repo)
    result = search.search_files(scope, "helper")
    assert result["truncated"] is False
    matches = {(m["file"], m["line"]) for m in result["matches"]}
    assert ("src/widget.py", 5) in matches
    assert "def helper(value)" in result["matches"][0]["snippet"]


def test_search_case_and_prefix(repo):
    scope = RepoScope(repo)
    assert search.search_files(scope, "HELPER")["total"] == 0
    assert search.search_files(scope, "HELPER", ignore_case=True)["total"] >= 1
    scoped = search.search_files(scope, "search me")
    assert scoped["matches"][0]["file"] == "README.md"
    # path_prefix restricts the walk
    assert search.search_files(scope, "def", path_prefix="src")["total"] >= 2
    assert search.search_files(scope, "def", path_prefix="tests")["total"] == 1
    with pytest.raises(ScopeError):
        search.search_files(scope, "x", path_prefix="../escape")


def test_search_rejects_bad_input(repo):
    scope = RepoScope(repo)
    with pytest.raises(ValueError):
        search.search_files(scope, "(")  # invalid regex
    with pytest.raises(ValueError):
        search.search_files(scope, "")
    with pytest.raises(ValueError):
        search.search_files(scope, "x", max_results=0)


def test_search_max_results_caps(repo):
    scope = RepoScope(repo)
    result = search.search_files(scope, "def", max_results=1)
    assert result["truncated"] is True
    assert result["total"] == 1


def test_find_symbols_definitions(repo):
    scope = RepoScope(repo)
    classes = symbols.find_symbols(scope, "Widget")
    assert [s for s in classes["symbols"] if s["name"] == "Widget"] == [
        {"file": "src/widget.py", "kind": "class", "line": 1, "name": "Widget"}
    ]
    # method detection requires the class context
    methods = {s["name"]: s for s in symbols.find_symbols(scope, "spin")["symbols"]}
    assert methods["spin"]["kind"] == "method"
    functions = {s["name"]: s for s in symbols.find_symbols(scope, "helper")["symbols"]}
    assert functions["helper"]["kind"] == "function"


def test_find_symbols_exact_and_kind(repo):
    scope = RepoScope(repo)
    result = symbols.find_symbols(scope, "spin", partial=True)
    assert {s["name"] for s in result["symbols"]} == {"spin", "test_spin"}
    # exact search must not match substrings
    assert symbols.find_symbols(scope, "Widget", partial=False)["total"] == 1
    assert symbols.find_symbols(scope, "idget", partial=False)["total"] == 0
    only_classes = symbols.find_symbols(scope, "spin", kind="class")
    assert only_classes["total"] == 0
    only_methods = symbols.find_symbols(scope, "spin", kind="method")
    assert only_methods["total"] == 1
    with pytest.raises(ValueError):
        symbols.find_symbols(scope, "x", kind="bogus")


def test_find_symbols_skips_unparseable(repo):
    (repo / "src" / "broken.py").write_text("def broken(:\n")
    scope = RepoScope(repo)
    assert symbols.find_symbols(scope, "broken")["total"] == 0
    assert symbols.find_symbols(scope, "helper")["total"] == 1


def test_find_test_files(repo):
    scope = RepoScope(repo)
    result = test_discovery.find_test_files(scope)
    assert "tests/test_widget.py" in result["test_files"]
    assert result["total"] >= 1
    scoped = test_discovery.find_test_files(scope, path_prefix="src")
    assert scoped["total"] == 0
    with pytest.raises(ValueError):
        test_discovery.find_test_files(scope, path_prefix="../escape")


@pytest.fixture()
def git_repo(tmp_path):
    root = tmp_path / "gitrepo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "app.py").write_text("def run():\n    return 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "initial commit")
    (root / "app.py").write_text("def run():\n    return 2\n")
    return root


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_git_status_log_diff(git_repo):
    inspector = GitInspector(git_repo)
    assert inspector.is_git_repo() is True

    status = inspector.status()
    assert status["branch"] == "main"
    assert any(entry["path"] == "app.py" for entry in status["entries"])
    assert status["total"] >= 1

    log = inspector.log(limit=5)
    assert log["total"] == 1
    assert log["commits"][0]["subject"] == "initial commit"

    diff = inspector.diff(base="HEAD", path="app.py")
    assert "+    return 2" in diff["diff"]
    assert diff["truncated"] is False


def test_git_diff_stat_only(git_repo):
    inspector = GitInspector(git_repo)
    diff = inspector.diff(stat_only=True)
    assert "app.py" in diff["diff"]


def test_git_rejects_invalid_input(git_repo):
    inspector = GitInspector(git_repo)
    with pytest.raises(GitError):
        inspector.diff(base="-oops")  # option injection guard
    with pytest.raises(GitError):
        inspector.diff(base="")
    with pytest.raises(GitError):
        inspector.diff(path="\x00")
    with pytest.raises(GitError):
        inspector.log(limit=0)
    with pytest.raises(GitError):
        inspector.log(limit=1000)


def test_git_not_a_repo(tmp_path):
    inspector = GitInspector(tmp_path)
    assert inspector.is_git_repo() is False
    with pytest.raises(GitError):
        inspector.status()