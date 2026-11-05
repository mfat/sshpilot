"""Phase 1: the incremental index, resolution labels and bounded context.

Uses a small synthetic checkout so every expectation is exact; the real
checkout is exercised in test_dev_intelligence_repo.py.
"""

import os
import sqlite3
import threading
from pathlib import Path

import pytest

from sshpilot.mcp._scope import RepoScope, ScopeError
from sshpilot.mcp.dev.engine import DevIntelligence, EngineError
from sshpilot.mcp.dev.engine import index as index_module
from sshpilot.mcp.dev.engine.extract import extract_source, module_name_for
from sshpilot.mcp.dev.engine.graph import Graph, resolve_reference
from sshpilot.mcp.dev.engine.index import DevIndex

PKG = "src/sshpilot/demo"

ALPHA = '''"""Alpha module."""
from sshpilot.demo.beta import helper as beta_helper
from . import beta


def compute(value):
    """Compute things."""
    return beta_helper(value) + beta.other()


class Widget:
    def __init__(self):
        self.button.connect("clicked", self._on_clicked)

    def _on_clicked(self, *args):
        return self.refresh()

    def refresh(self):
        return compute(1)

    def dynamic(self, name, thing):
        getattr(self, name)()
        return thing.mystery_call()
'''

BETA = '''
def helper(value):
    return value * 2


def other():
    return 3


class Widget:
    """Same class name as alpha.Widget: must not collide."""

    def refresh(self):
        return helper(2)

    def refresh(self):  # duplicate qualname in one file
        return other()
'''

TEST_ALPHA = '''
from sshpilot.demo.alpha import compute
import sshpilot.demo.beta


def test_compute():
    assert compute(1) == 7


def test_beta_module():
    assert sshpilot.demo.beta.other() == 3
'''


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    _write(root, f"{PKG}/__init__.py", "")
    _write(root, f"{PKG}/alpha.py", ALPHA)
    _write(root, f"{PKG}/beta.py", BETA)
    _write(root, "tests/demo/test_alpha.py", TEST_ALPHA)
    return root


@pytest.fixture()
def service(repo, tmp_path):
    svc = DevIntelligence(RepoScope(repo), index_dir=tmp_path / "index")
    yield svc
    svc.close()


def _graph(service) -> Graph:
    service.index.refresh()
    return Graph(service.index)


def test_module_names():
    assert module_name_for("src/sshpilot/demo/alpha.py") == "sshpilot.demo.alpha"
    assert module_name_for("src/sshpilot/demo/__init__.py") == "sshpilot.demo"
    assert module_name_for("tests/mcp/test_x.py") == "tests.mcp.test_x"


def test_first_index_then_warm_reuse(service):
    cold = service.index.refresh()
    assert cold["reindexed"] == 4 and cold["scanned"] == 4 and cold["removed"] == 0
    warm = service.index.refresh()
    assert warm["reindexed"] == 0 and warm["rehashed"] == 0
    freshness = service.freshness()
    assert freshness["files_indexed"] == 4
    assert freshness["files_by_status"] == {"ok": 4}
    assert freshness["index_schema"] == index_module.SCHEMA_VERSION


def test_unchanged_queries_do_not_reextract(service, monkeypatch):
    service.index.refresh()
    calls = []
    original = index_module.extract_source

    def counting(path, source):
        calls.append(path)
        return original(path, source)

    monkeypatch.setattr(index_module, "extract_source", counting)
    service.get_context("compute")
    service.inspect_impact(paths=[f"{PKG}/alpha.py"])
    assert calls == []


def test_modified_file_is_reindexed_alone(service, repo):
    service.index.refresh()
    _write(repo, f"{PKG}/beta.py", "\n\n\n" + BETA)  # shift every line by 3
    stats = service.index.refresh()
    assert stats["reindexed"] == 1
    assert stats["reindexed_paths"] == [f"{PKG}/beta.py"]
    graph = Graph(service.index)
    helper = graph.symbol(f"{PKG}/beta.py::helper")
    assert helper["line"] == 5


def test_same_stat_rewrite_is_caught_before_serving_lines(service, repo):
    service.index.refresh()
    path = repo / PKG / "beta.py"
    before = path.stat()
    path.write_text(BETA.replace("value * 2", "value * 3"), encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    result = service.get_context(f"{PKG}/beta.py::helper")
    assert result["status"] == "ok"
    assert "value * 3" in result["source"]["text"]


def test_deleted_and_renamed_files_disappear(service, repo):
    service.index.refresh()
    (repo / PKG / "beta.py").rename(repo / PKG / "gamma.py")
    stats = service.index.refresh()
    assert stats["removed"] == 1 and stats["reindexed"] == 1
    graph = Graph(service.index)
    assert graph.symbol(f"{PKG}/beta.py::helper") is None
    assert graph.symbol(f"{PKG}/gamma.py::helper") is not None
    rows = service.index.db.execute("SELECT COUNT(*) FROM symbols WHERE path=?", (f"{PKG}/beta.py",)).fetchone()
    assert rows[0] == 0


def test_duplicate_names_do_not_collide(service):
    graph = _graph(service)
    widgets, how = resolve_reference(graph, "Widget")
    assert how == "name"
    assert {w["id"] for w in widgets} == {f"{PKG}/alpha.py::Widget", f"{PKG}/beta.py::Widget"}
    ids = [s["id"] for s in graph.in_file(f"{PKG}/beta.py") if s["name"] == "refresh"]
    assert ids == [f"{PKG}/beta.py::Widget.refresh", f"{PKG}/beta.py::Widget.refresh@2"]
    ambiguous = service.get_context("Widget")
    assert ambiguous["status"] == "ambiguous"
    assert len(ambiguous["candidates"]) == 2


def test_static_resolution_and_labels(service):
    graph = _graph(service)
    compute = graph.symbol(f"{PKG}/alpha.py::compute")
    callees = {(e.get("target") or e.get("target_name")): e["confidence"] for e in graph.outgoing(compute)}
    assert callees[f"{PKG}/beta.py::helper"] == "verified_static"  # import alias
    assert callees[f"{PKG}/beta.py::other"] == "verified_static"  # module attribute
    refresh = graph.symbol(f"{PKG}/alpha.py::Widget.refresh")
    incoming = graph.incoming(refresh)["resolved"]
    assert [(e["source"], e["confidence"]) for e in incoming] == [
        (f"{PKG}/alpha.py::Widget._on_clicked", "heuristic")
    ]


def test_dynamic_calls_are_never_certain(service):
    graph = _graph(service)
    dynamic = graph.symbol(f"{PKG}/alpha.py::Widget.dynamic")
    edges = graph.outgoing(dynamic)
    assert edges, "dynamic calls must still be reported"
    assert {e["confidence"] for e in edges} == {"unresolved"}
    assert {e["mechanism"] for e in edges} == {"getattr-dynamic", "attribute-call"}


def test_gtk_signal_handler_is_heuristic_edge(service):
    graph = _graph(service)
    handler = graph.symbol(f"{PKG}/alpha.py::Widget._on_clicked")
    incoming = graph.incoming(handler)["resolved"]
    assert [(e["kind"], e["confidence"]) for e in incoming] == [("connects_signal", "heuristic")]


def test_reverse_dependencies_are_bounded_and_ordered(service):
    graph = _graph(service)
    helper = graph.symbol(f"{PKG}/beta.py::helper")
    dependents, truncated = graph.reverse_dependencies([helper], depth=3)
    assert not truncated
    ids = [d["id"] for d in dependents]
    assert ids[0] == f"{PKG}/alpha.py::<module>" or f"{PKG}/alpha.py::compute" in ids
    assert f"{PKG}/alpha.py::Widget.refresh" in ids  # compute <- refresh
    assert [d["depth"] for d in dependents] == sorted(d["depth"] for d in dependents)
    again, _ = graph.reverse_dependencies([helper], depth=3)
    assert again == dependents  # deterministic
    limited, truncated = graph.reverse_dependencies([helper], depth=3, limit=1)
    assert truncated and len(limited) == 1


def test_related_tests_use_static_evidence(service):
    result = service.get_context(f"{PKG}/alpha.py::compute", budget=20000)
    tests = {entry["test"]: entry["evidence"] for entry in result["related_tests"]}
    assert tests["tests/demo/test_alpha.py::test_compute"] == "calls"
    beta = service.get_context(f"{PKG}/beta.py::other", budget=20000)
    evidence = {entry["test"]: entry for entry in beta["related_tests"]}
    assert evidence["tests/demo/test_alpha.py::test_beta_module"]["evidence"] == "calls"


def test_parse_failure_is_reported(service, repo):
    _write(repo, f"{PKG}/broken.py", "def broken(:\n    pass\n")
    service.index.refresh()
    errors = service.index.parse_errors()
    assert [e["path"] for e in errors] == [f"{PKG}/broken.py"]
    assert errors[0]["status"] == "syntax_error"
    impact = service.inspect_impact(paths=[f"{PKG}/broken.py"])
    assert impact["analysis_complete"] is False
    assert any("broken.py" in warning for warning in impact["warnings"])


def test_symlink_escape_is_denied(repo, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("def secret():\n    return 1\n")
    (repo / PKG / "link.py").symlink_to(outside)
    svc = DevIntelligence(RepoScope(repo), index_dir=tmp_path / "index2")
    try:
        svc.index.refresh()
        assert f"{PKG}/link.py" not in svc.index.candidate_files()
        assert Graph(svc.index).symbol(f"{PKG}/link.py::secret") is None
        with pytest.raises((EngineError, ScopeError)):
            svc.get_context("../outside.py:1")
        with pytest.raises((EngineError, ScopeError)):
            svc.inspect_impact(paths=["../outside.py"])
    finally:
        svc.close()


def test_bounded_output_reports_truncation(service, repo):
    body = "".join(f"    step_{n} = compute({n})  # a reasonably long line of code\n" for n in range(60))
    _write(repo, f"{PKG}/long.py", "from sshpilot.demo.alpha import compute\n\n\ndef long_function():\n" + body)
    small = service.get_context(f"{PKG}/long.py::long_function", budget=800)
    assert small["budget"]["limit_bytes"] == 800
    assert small["budget"]["used_bytes"] <= 800 or small["budget"]["over_budget"] is True
    large = service.get_context(f"{PKG}/long.py::long_function", budget=20000)
    assert large["budget"]["truncated"] is False
    assert len(large["source"]["text"]) > len(small["source"]["text"])
    assert small["source"].get("truncated") is True
    assert small["source"]["next_lines_ref"].startswith(f"{PKG}/long.py::long_function?lines=")
    page = service.get_context(small["source"]["next_lines_ref"], budget=20000)
    assert page["source"]["lines"][0] == small["source"]["lines"][1] + 1


def test_reference_forms(service):
    by_line = service.get_context(f"{PKG}/alpha.py:16")
    assert by_line["symbol"]["id"] == f"{PKG}/alpha.py::Widget._on_clicked"
    dotted = service.get_context("sshpilot.demo.alpha.compute")
    assert dotted["symbol"]["id"] == f"{PKG}/alpha.py::compute"
    missing = service.get_context("does_not_exist_anywhere")
    assert missing["status"] == "not_found"
    with pytest.raises(EngineError):
        service.get_context("")


def test_exclusion_policy(repo, tmp_path, monkeypatch):
    _write(repo, f"{PKG}/.env.py", "TOKEN = 'x'\n")
    _write(repo, f"{PKG}/private_settings.py", "PASSWORD = 'x'\n")
    monkeypatch.setenv(index_module.EXCLUDE_ENV, "*/private_*.py")
    index = DevIndex(RepoScope(repo), index_dir=tmp_path / "index3")
    try:
        files = index.candidate_files()
        assert f"{PKG}/.env.py" not in files
        assert f"{PKG}/private_settings.py" not in files
        assert f"{PKG}/alpha.py" in files
    finally:
        index.close()


def test_index_never_stores_source_text(service, repo):
    _write(repo, f"{PKG}/creds.py", 'def connect():\n    password = "hunter2-very-secret"\n    return password\n')
    service.index.refresh()
    raw = service.index.path.read_bytes()
    for wal in service.index.path.parent.glob("*-wal"):
        raw += wal.read_bytes()
    assert b"hunter2-very-secret" not in raw


def test_index_lives_outside_the_checkout(repo, monkeypatch, tmp_path):
    monkeypatch.delenv(index_module.INDEX_DIR_ENV, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    index = DevIndex(RepoScope(repo))
    try:
        assert repo.resolve() not in index.path.resolve().parents
        assert str(index.path).startswith(str(tmp_path / "cache"))
    finally:
        index.close()


def test_two_clients_share_one_index_safely(repo, tmp_path):
    directory = tmp_path / "shared"
    first = DevIndex(RepoScope(repo), index_dir=directory)
    second = DevIndex(RepoScope(repo), index_dir=directory)
    results = []
    errors = []

    def run(index):
        try:
            results.append(index.refresh())
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [threading.Thread(target=run, args=(index,)) for index in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(r["reindexed"] for r in results) == [0, 4]
    count = sqlite3.connect(str(first.path)).execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert count == 4
    first.close()
    second.close()


def test_impact_maps_hunks_to_symbols(repo, tmp_path):
    import subprocess

    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    import shutil

    shutil.rmtree(repo / ".git")
    for argv in (["git", "init", "-q"], ["git", "add", "."], ["git", "commit", "-qm", "base"]):
        subprocess.run(argv, cwd=repo, check=True, env=env)
    text = (repo / PKG / "beta.py").read_text().replace("return 3", "return 4")
    (repo / PKG / "beta.py").write_text(text)
    _write(repo, f"{PKG}/new_module.py", "def fresh():\n    return 1\n")
    svc = DevIntelligence(RepoScope(repo), index_dir=tmp_path / "index4")
    try:
        result = svc.inspect_impact(base="HEAD", budget=20000)
        changed = {entry["path"]: entry for entry in result["changed"]}
        assert changed[f"{PKG}/beta.py"]["symbols"] == ["other"]
        assert changed[f"{PKG}/beta.py"]["mode"] == "hunks"
        assert changed[f"{PKG}/new_module.py"]["symbols"] == ["fresh"]
        dependents = {d["id"] for d in result["dependents"]}
        assert f"{PKG}/alpha.py::compute" in dependents
        assert "tests/demo/test_alpha.py" in result["tests"]["focused_files"]
        assert result["freshness"]["git_head"]
        with pytest.raises(EngineError):
            svc.inspect_impact(base="--output=/tmp/x")
        with pytest.raises(EngineError):
            svc.inspect_impact(base="no-such-ref")
    finally:
        svc.close()


def test_removed_function_is_flagged_as_dangling(service, repo):
    service.index.refresh()
    _write(repo, f"{PKG}/beta.py", BETA.replace("def other():\n    return 3\n", ""))
    result = service.inspect_impact(paths=[f"{PKG}/beta.py"], budget=20000)
    dangling = [risk for risk in result["risks"] if risk["kind"] == "unresolved-references-into-changed-module"]
    assert dangling
    names = {ref["name"] for ref in dangling[0]["references"]}
    assert "sshpilot.demo.beta.other" in names


def test_extract_handles_odd_constructs():
    source = "class A:\n    def __call__(self):\n        return self()\n\nmatch 1:\n    case 1:\n        pass\n"
    result = extract_source("src/sshpilot/x.py", source)
    assert result.status == "ok"


def test_index_timing_is_reported(service):
    stats = service.index.refresh()
    assert stats["duration_ms"] >= 0
    assert "full" in stats
    rebuilt = service.index_status(rebuild=True)
    assert rebuilt["refresh"]["full"] is True
    assert rebuilt["refresh"]["reindexed"] == 4


# --- review fixes: files that stop being indexable lose their rows ----------


def _symbols_in(service, path):
    return service.index.db.execute("SELECT COUNT(*) FROM symbols WHERE path=?", (path,)).fetchone()[0]


def _status(service, path):
    return service.index.db.execute("SELECT status FROM files WHERE path=?", (path,)).fetchone()[0]


def test_indexed_file_that_grows_too_large_loses_its_symbols(service, repo, monkeypatch):
    path = f"{PKG}/beta.py"
    service.index.refresh()
    assert _symbols_in(service, path) > 0
    edges_before = service.index.db.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    monkeypatch.setattr(index_module, "MAX_INDEXED_FILE_BYTES", 50)
    _write(repo, path, BETA + "\n# grown\n")
    stats = service.index.refresh()
    assert stats["skipped_too_large"] == [path]
    assert _status(service, path) == "skipped_too_large"
    assert _symbols_in(service, path) == 0
    assert service.index.db.execute("SELECT COUNT(*) FROM edges").fetchone()[0] < edges_before
    assert Graph(service.index).symbol(f"{PKG}/beta.py::helper") is None
    assert [e["path"] for e in service.index.parse_errors()] == [path]
    # Recovery: back under the cap -> indexed again.
    monkeypatch.setattr(index_module, "MAX_INDEXED_FILE_BYTES", 2 * 1024 * 1024)
    _write(repo, path, BETA)
    service.index.refresh()
    assert _status(service, path) == "ok"
    assert Graph(service.index).symbol(f"{PKG}/beta.py::helper") is not None


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read mode-000 files")
def test_indexed_file_that_becomes_unreadable_loses_its_symbols(service, repo):
    path = f"{PKG}/beta.py"
    service.index.refresh()
    assert _symbols_in(service, path) > 0
    target = repo / path
    target.write_text(BETA + "\n# touched\n")  # new stat so refresh re-reads it
    target.chmod(0)
    try:
        stats = service.index.refresh()
        assert stats["unreadable"] == [path]
        assert _status(service, path) == "unreadable"
        assert _symbols_in(service, path) == 0
        impact = service.inspect_impact(paths=[path])
        assert impact["analysis_complete"] is False
        assert any("unreadable" in warning for warning in impact["warnings"])
    finally:
        target.chmod(0o644)
    # Recovery without touching the contents: the -1 stat sentinel forces a re-read.
    service.index.refresh()
    assert _status(service, path) == "ok"
    assert _symbols_in(service, path) > 0


def test_unindexable_domain_input_rebuilds_domain_edges(service, repo, monkeypatch):
    service.index.refresh()
    digest_before = service.index.db.execute("SELECT value FROM meta WHERE key='domain_digest'").fetchone()[0]
    monkeypatch.setattr(index_module, "MAX_INDEXED_FILE_BYTES", 10)
    _write(repo, "src/sshpilot/api/client.py", "class SshPilotClient:\n    pass\n")
    stats = service.index.refresh()
    assert "src/sshpilot/api/client.py" in stats["skipped_too_large"]
    assert stats["domain_rebuilt"] is True
    digest_after = service.index.db.execute("SELECT value FROM meta WHERE key='domain_digest'").fetchone()[0]
    assert digest_after != digest_before


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read mode-000 files")
def test_permission_only_change_invalidates_without_size_or_mtime_change(service, repo):
    """chmod changes neither size nor mtime; ctime is what reveals it."""
    path = f"{PKG}/beta.py"
    service.index.refresh()
    target = repo / path
    before = target.stat()
    target.chmod(0)
    try:
        after = target.stat()
        assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
        stats = service.index.refresh()
        assert stats["unreadable"] == [path]
        assert _symbols_in(service, path) == 0
    finally:
        target.chmod(0o644)
    service.index.refresh()
    assert _status(service, path) == "ok"
    assert _symbols_in(service, path) > 0
