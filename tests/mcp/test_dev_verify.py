"""Phase 3: test selection from evidence and honest verify_change states.

A throw-away Git checkout with a tiny package and real pytest files: the
tests genuinely pass, fail, skip and time out, and the runner reports each.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sshpilot.mcp._scope import RepoScope
from sshpilot.mcp.dev import execution
from sshpilot.mcp.dev.engine import DevIntelligence, EngineError

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE = '''def add(a, b):
    return a + b


def untested():
    return "no test imports this"
'''
TEST_CORE = '''from demo_app.core import add


def test_add():
    assert add(1, 2) == 3
'''
CONFTEST = '''import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
'''
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "conftest.py", CONFTEST)
    _write(root, "src/demo_app/__init__.py", "")
    _write(root, "src/demo_app/core.py", CORE)
    _write(root, "tests/demo/test_core.py", TEST_CORE)
    env = {**os.environ, **GIT_ENV}
    for argv in (["git", "init", "-q"], ["git", "add", "."], ["git", "commit", "-qm", "base"]):
        subprocess.run(argv, cwd=root, check=True, env=env)
    return root


@pytest.fixture()
def service(repo, tmp_path):
    svc = DevIntelligence(RepoScope(repo), index_dir=tmp_path / "index")
    yield svc
    svc.close()


def _change_add(repo: Path, body: str = "return a + b  # changed") -> None:
    text = (repo / "src/demo_app/core.py").read_text().replace("return a + b", body)
    (repo / "src/demo_app/core.py").write_text(text)


def _check(result: dict, name: str) -> dict:
    return next(check for check in result["checks"] if check["name"] == name)


def test_static_mode_never_claims_verification(service, repo):
    _change_add(repo)
    result = service.verify_change(base="HEAD", mode="static")
    assert result["overall"] == "static-only"
    assert result["checks_passed"] is False
    assert result["coverage_confirmed"] is False
    tests = _check(result, "focused_tests")
    assert (tests["executed"], tests["state"]) == (False, "not_run")
    assert result["test_selection"]["selected_files"] == ["tests/demo/test_core.py"]
    assert _check(result, "python_syntax")["state"] == "passed"


def test_covered_change_selects_and_runs_matching_test(service, repo, tmp_path, monkeypatch):
    _change_add(repo)
    monkeypatch.chdir(tmp_path)  # caller CWD outside the checkout
    result = service.verify_change(base="HEAD", mode="focused")
    tests = _check(result, "focused_tests")
    assert tests["state"] == "passed", tests
    assert tests["counts"] == {"passed": 1, "failed": 0, "errors": 0, "skipped": 0}
    assert result["test_selection"]["evidence"][0] == {
        "test": "tests/demo/test_core.py::test_add",
        "evidence": "calls",
        "target": "src/demo_app/core.py::add",
    }
    assert result["overall"] == "checks-passed"
    assert result["checks_passed"] is True
    assert result["coverage_confirmed"] is False  # never claimed without coverage data
    assert result["verification_gaps"] == []
    assert not (repo / ".pytest_cache").exists()


def test_failing_test_is_reported_as_failed(service, repo):
    _change_add(repo, "return a - b")
    result = service.verify_change(base="HEAD", mode="focused")
    tests = _check(result, "focused_tests")
    assert tests["state"] == "failed"
    assert tests["counts"]["failed"] == 1
    assert tests["failures"][0]["test"].endswith("test_add")
    assert result["overall"] == "failed"
    assert result["checks_passed"] is False


def test_skipped_tests_stay_skipped(service, repo):
    _write(repo, "tests/demo/test_core.py", "import pytest\nfrom demo_app.core import add\n\n\n@pytest.mark.skip(reason='later')\ndef test_add():\n    assert add(1, 2) == 3\n")
    _change_add(repo)
    result = service.verify_change(base="HEAD", mode="focused")
    tests = _check(result, "focused_tests")
    assert tests["state"] == "skipped"
    assert tests["counts"]["skipped"] == 1 and tests["counts"]["passed"] == 0
    assert result["overall"] == "incomplete"
    assert any("skipped" in gap for gap in result["verification_gaps"])


def test_mixed_passed_and_skipped_is_not_a_full_pass(service, repo):
    """3 passed + 2 skipped must leave a gap: the skipped tests may be the
    ones that exercise the change."""
    body = "import pytest\nfrom demo_app.core import add\n\n\n"
    for n in range(3):
        body += f"def test_add_{n}():\n    assert add({n}, 1) == {n + 1}\n\n\n"
    for n in range(2):
        body += f"@pytest.mark.skip(reason='flaky')\ndef test_add_skipped_{n}():\n    assert add(1, 1) == 2\n\n\n"
    _write(repo, "tests/demo/test_core.py", body)
    _change_add(repo)
    result = service.verify_change(base="HEAD", mode="focused")
    tests = _check(result, "focused_tests")
    assert tests["counts"] == {"passed": 3, "failed": 0, "errors": 0, "skipped": 2}
    assert tests["state"] == "passed"  # the run itself had no failures
    assert len(tests["skipped_tests"]) == 2
    assert result["checks_passed"] is True
    assert result["overall"] == "incomplete"
    assert any("2 selected test(s) were skipped" in gap for gap in result["verification_gaps"])


def test_import_alone_is_not_test_evidence(service, repo):
    _write(repo, "tests/demo/test_core.py", "from demo_app.core import add, untested\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    text = (repo / "src/demo_app/core.py").read_text().replace('"no test imports this"', '"changed"')
    (repo / "src/demo_app/core.py").write_text(text)
    result = service.verify_change(base="HEAD", mode="focused")
    assert result["test_selection"]["unmapped_changed_symbols"] == ["src/demo_app/core.py::untested"]
    assert result["overall"] == "incomplete"
    assert any("an import alone does not count" in gap for gap in result["verification_gaps"])


def test_timeout_is_explicit(service, repo):
    _write(repo, "tests/demo/test_core.py", "import time\nfrom demo_app.core import add\n\n\ndef test_add():\n    time.sleep(30)\n")
    _change_add(repo)
    result = service.verify_change(base="HEAD", mode="focused", test_timeout=3)
    tests = _check(result, "focused_tests")
    assert tests["state"] == "unknown"
    assert tests["timed_out"] is True
    assert result["overall"] == "incomplete"


def test_unmatched_change_requests_broader_coverage(service, repo):
    text = (repo / "src/demo_app/core.py").read_text().replace('"no test imports this"', '"changed"')
    (repo / "src/demo_app/core.py").write_text(text)
    result = service.verify_change(base="HEAD", mode="focused")
    selection = result["test_selection"]
    assert selection["unmapped_changed_symbols"] == ["src/demo_app/core.py::untested"]
    assert selection["broad_run_recommended"] is True
    # The module-level import still selects the file as weak evidence; it
    # runs and passes, but the unmapped symbol keeps the verdict open.
    assert selection["selected_files"] == ["tests/demo/test_core.py"]
    assert _check(result, "focused_tests")["state"] == "passed"
    assert result["overall"] == "incomplete"
    assert result["checks_passed"] is True  # what ran passed ...
    assert any("no direct test evidence" in gap for gap in result["verification_gaps"])


def test_no_changes_is_not_a_pass(service):
    result = service.verify_change(base="HEAD", mode="focused")
    assert result["overall"] == "incomplete"
    assert any("no focused tests" in gap or "not verified" in gap for gap in result["verification_gaps"])


def test_syntax_error_fails_verification(service, repo):
    (repo / "src/demo_app/core.py").write_text("def add(a, b:\n    return a\n")
    result = service.verify_change(base="HEAD", mode="static")
    assert _check(result, "python_syntax")["state"] == "failed"
    assert result["overall"] == "failed"


def test_input_validation(service):
    with pytest.raises(EngineError):
        service.verify_change(mode="everything")
    with pytest.raises(EngineError):
        service.verify_change(mode="static", suites=["api"])
    with pytest.raises(EngineError):
        service.verify_change(mode="focused", suites=["../../etc"])
    with pytest.raises(EngineError):
        service.verify_change(base="--exec=evil", mode="static")


def test_run_test_files_rejects_unsafe_targets(repo):
    scope = RepoScope(repo)
    for bad in (["../x.py"], ["src/demo_app/core.py"], ["tests/../conftest.py"], ["-k", "x"], []):
        with pytest.raises((execution.ExecutionError, Exception)):
            execution.run_test_files(scope, bad)


def test_api_artifact_change_triggers_contract_checks(tmp_path):
    """On the real checkout, touching the generated schema must run the
    drift and artifact checks (read-only: nothing is edited)."""
    from sshpilot.mcp.dev.engine.graph import Graph
    from sshpilot.mcp.dev.engine.verify import verify_change

    svc = DevIntelligence(RepoScope(REPO_ROOT), index_dir=tmp_path / "index", prefixes=["src/sshpilot/api", "src/sshpilot/daemon", "src/sshpilot/core", "tests/api"])
    try:
        svc.index.refresh()
        graph = Graph(svc.index)
        from sshpilot.mcp.dev.engine import impact as impact_module

        original = impact_module.collect_changes

        def fake_changes(graph, base, paths):
            return {"docs/api/generated/schema.json": None}, ["simulated change"]

        impact_module.collect_changes = fake_changes
        try:
            result = verify_change(graph, svc.freshness, base="HEAD", mode="static")
        finally:
            impact_module.collect_changes = original
        drift = _check(result, "api_drift")
        artifacts = _check(result, "api_artifacts")
        assert drift["executed"] is True and drift["state"] == "passed"
        assert artifacts["executed"] is True and artifacts["state"] == "passed"
        assert result["overall"] == "static-only"
    finally:
        svc.close()


def test_cli_verify_change_exit_codes(repo, tmp_path):
    _change_add(repo)
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    base = [sys.executable, "-m", "sshpilot.mcp.dev.cli", "--root", str(repo), "--index-dir", str(tmp_path / "cli-index"), "--compact", "verify-change", "--base", "HEAD"]
    static = subprocess.run([*base, "--mode", "static"], capture_output=True, text=True, env=env, cwd=tmp_path, timeout=300)
    assert static.returncode == 1  # static-only is not a pass
    assert json.loads(static.stdout)["overall"] == "static-only"
    focused = subprocess.run([*base, "--mode", "focused"], capture_output=True, text=True, env=env, cwd=tmp_path, timeout=300)
    assert focused.returncode == 0, focused.stdout[-2000:] + focused.stderr[-2000:]
    assert json.loads(focused.stdout)["overall"] == "checks-passed"
