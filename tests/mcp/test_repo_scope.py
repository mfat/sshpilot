"""Path-safety unit tests for the dev-MCP repository scope."""

import pytest

from sshpilot.mcp import _scope
from sshpilot.mcp._scope import RepoScope, ScopeError, SSHPILOT_MCP_ROOT


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "README.md").write_text("hello\n")
    (root / "sub").mkdir()
    return root


def test_root_must_be_a_directory(tmp_path):
    with pytest.raises(ScopeError):
        RepoScope(tmp_path / "missing")
    file = tmp_path / "file.txt"
    file.write_text("x")
    with pytest.raises(ScopeError):
        RepoScope(file)


def test_discover_pinned(repo, monkeypatch):
    monkeypatch.setenv(SSHPILOT_MCP_ROOT, str(repo))
    scope = RepoScope.discover()
    assert scope.root == repo.resolve()


def test_discover_from_cwd(repo, monkeypatch):
    subdir = repo / "src"
    subdir.mkdir()
    monkeypatch.delenv(SSHPILOT_MCP_ROOT, raising=False)
    monkeypatch.chdir(subdir)
    scope = RepoScope.discover()
    assert scope.root == repo.resolve()


def test_discover_resolves_up_to_project_marker(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text("")
    deep = root / "a" / "b"
    deep.mkdir(parents=True)
    monkeypatch.delenv(SSHPILOT_MCP_ROOT, raising=False)
    monkeypatch.chdir(deep)
    assert RepoScope.discover().root == root.resolve()


def test_discover_requires_root(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.delenv(SSHPILOT_MCP_ROOT, raising=False)
    # The system temporary directory can itself live inside a checkout (or be
    # a Git root in a shared agent environment), so explicitly simulate a
    # marker-free ancestor chain for this negative-path test.
    monkeypatch.setattr(_scope, "_GIT_MARKER", ".missing-git-marker")
    monkeypatch.setattr(_scope, "_PROJECT_MARKERS", ())
    monkeypatch.chdir(empty)
    with pytest.raises(ScopeError):
        RepoScope.discover()


def test_resolve_relative_paths(repo):
    scope = RepoScope(repo)
    assert scope.resolve("README.md") == (repo / "README.md").resolve()
    assert scope.resolve("sub/../README.md") == (repo / "README.md").resolve()


def test_resolve_accepts_root_directory(repo):
    scope = RepoScope(repo)
    assert scope.resolve(".") == repo.resolve()


def test_resolve_rejects_traversal_and_absolute(repo):
    scope = RepoScope(repo)
    for bad in ("../escape", "sub/../../../escape", "/etc/passwd", ".."):
        with pytest.raises(ScopeError):
            scope.resolve(bad)


def test_resolve_rejects_absolute_even_within_root(repo):
    scope = RepoScope(repo)
    inside = repo / "README.md"
    with pytest.raises(ScopeError):
        scope.resolve(str(inside))


def test_resolve_rejects_null_byte(repo):
    scope = RepoScope(repo)
    with pytest.raises(ScopeError):
        scope.resolve("README\x00.md")


def test_resolve_rejects_non_string(repo):
    scope = RepoScope(repo)
    with pytest.raises(TypeError):
        scope.resolve(123)  # type: ignore[arg-type]


def test_resolve_rejects_symlink_escape(repo, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (repo / "link.txt").symlink_to(outside)
    (repo / "sub" / "link").symlink_to(outside.parent, target_is_directory=True)
    scope = RepoScope(repo)
    with pytest.raises(ScopeError):
        scope.resolve("link.txt")
    with pytest.raises(ScopeError):
        scope.resolve("sub/link/secret.txt")


def test_is_within(repo, tmp_path):
    scope = RepoScope(repo)
    assert scope.is_within(repo / "README.md")
    assert scope.is_within(repo)
    assert not scope.is_within(tmp_path / "outside.txt")


def test_read_text_and_missing(repo):
    scope = RepoScope(repo)
    assert scope.read_text("README.md") == "hello\n"
    with pytest.raises(ScopeError):
        scope.read_text("missing.txt")
    with pytest.raises(ScopeError):
        scope.read_text("sub")  # directory is not a file


def test_read_text_cap(repo):
    scope = RepoScope(repo)
    (repo / "big.txt").write_text("x" * 1000)
    with pytest.raises(ScopeError):
        scope.read_text("big.txt", max_bytes=100)
    assert scope.read_text("big.txt", max_bytes=2000) == "x" * 1000
    with pytest.raises(ValueError):
        scope.read_text("README.md", max_bytes=0)
