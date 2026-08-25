"""Tests for dev-MCP architecture intelligence."""

from pathlib import Path

import pytest

from sshpilot.mcp._scope import RepoScope
from sshpilot.mcp.dev import architecture


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def repo():
    return RepoScope(REPO_ROOT)


def test_frontend_neutrality_clean(repo):
    result = architecture.check_frontend_neutrality(repo)
    assert result["check"] == "frontend-neutrality"
    assert result["clean"] is True
    assert result["violations"] == []


def test_frontend_neutrality_detects_gi(tmp_path):
    root = tmp_path / "repo"
    (root / "src" / "sshpilot" / "core").mkdir(parents=True)
    (root / "src" / "sshpilot" / "core" / "bad.py").write_text(
        "import gi\nfrom gi.repository import Gtk\n"
    )
    scope = RepoScope(root)
    result = architecture.check_frontend_neutrality(scope)
    assert result["clean"] is False
    paths = {v["file"] for v in result["violations"]}
    assert any(p.endswith("core/bad.py") for p in paths)


def test_review_public_api_reports_surface(repo):
    result = architecture.review_public_api(repo)
    assert result["format"] == "sshpilot-api-schema-v1"
    assert result["client_methods"] >= 100
    assert result["daemon_methods"] >= 100
    assert set(result["client_status"]) <= {
        "implemented",
        "daemon-only",
        "schema-only",
    }
    assert result["models_defined"] >= 100
    assert "OpenSessionRequest" in result["models"]
    assert result["models_defined"] == len(result["models"])
    assert all(not name.startswith("_") for name in result["models"])
    assert "_validate_integer" not in result["models"]
    assert "validate_config_patch" not in result["models"]


def test_trace_interaction_scope_single(repo):
    result = architecture.trace_interaction_scope(repo, "respond_to_interaction")
    assert result["total"] == 1
    entry = result["methods"][0]
    assert entry["method"] == "respond_to_interaction"
    assert entry["requirement"] == "interactions.respond"
    assert entry["wire_methods"] == ["interactions.respond"]


def test_trace_interaction_scope_all(repo):
    result = architecture.trace_interaction_scope(repo)
    names = {m["method"] for m in result["methods"]}
    assert names == set(architecture.INTERACTION_METHODS)
    assert result["total"] == len(architecture.INTERACTION_METHODS)


def test_trace_interaction_scope_rejects_unknown(repo):
    with pytest.raises(architecture.ArchitectureError):
        architecture.trace_interaction_scope(repo, "open_session")


def test_plan_api_change_reports_authoritative_surfaces(repo):
    result = architecture.plan_api_change(repo, "open_session")
    assert result["check"] == "api-change-plan"
    assert result["trace"]["name"] == "open_session"
    assert result["trace"]["wire_methods"]
    assert {entry["surface"] for entry in result["checklist"]} == {
        "typed-api",
        "daemon-dispatch",
        "capabilities",
        "contracts-and-docs",
    }
    assert "check_api_drift" in result["validation"]


def test_plan_api_change_rejects_unknown_method(repo):
    with pytest.raises(architecture.ArchitectureError):
        architecture.plan_api_change(repo, "not_a_real_method")


def test_recommend_tests_for_api_and_mcp_paths(repo):
    result = architecture.recommend_tests(
        repo,
        ["src/sshpilot/api/daemon_client.py", "src/sshpilot/mcp/dev/server.py"],
    )
    assert result["check"] == "test-recommendations"
    assert {"api", "mcp", "architecture"} <= set(result["suites"])
    assert {"check_api_drift", "validate_api_artifacts"} <= set(result["checks"])
    assert "tests/mcp/test_dev_server_smoke.py" in result["focused_tests"]


@pytest.mark.parametrize(
    "path",
    ["../outside.py", "/tmp/outside.py", "C:\\outside.py", "./", ""],
)
def test_recommend_tests_rejects_unsafe_paths(repo, path):
    with pytest.raises(architecture.ArchitectureError):
        architecture.recommend_tests(repo, [path])


def test_validate_change_combines_checks_and_recommendations(repo, monkeypatch):
    monkeypatch.setattr(
        architecture,
        "review_commit",
        lambda scope, base="HEAD": {
            "check": "commit-review",
            "base": base,
            "changed_total": 1,
            "by_layer": {"core": ["src/sshpilot/core/identity_service.py"]},
            "boundary_crossed": False,
            "api_surface_changed": False,
            "generated_artifacts_changed": False,
            "diff_stat": "",
            "recommend_api_drift_check": False,
        },
    )
    result = architecture.validate_change(repo)
    assert result["check"] == "change-validation"
    assert result["clean"] is True
    assert result["api_drift"] is None
    assert result["api_artifacts"] is None
    assert {"architecture", "core"} <= set(result["recommended_tests"]["suites"])


def test_review_commit_classifies_layers(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src" / "sshpilot" / "gtk").mkdir(parents=True)
    (root / "src" / "sshpilot" / "core").mkdir(parents=True)
    (root / "src" / "sshpilot" / "api").mkdir(parents=True)
    (root / "src" / "sshpilot" / "gtk" / "win.py").write_text("")
    (root / "src" / "sshpilot" / "core" / "svc.py").write_text("")
    (root / "src" / "sshpilot" / "api" / "client.py").write_text("")
    import subprocess

    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
        ["add", "."],
        ["commit", "-q", "-m", "initial"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True)
    (root / "src" / "sshpilot" / "core" / "svc.py").write_text("changed\n")
    (root / "src" / "sshpilot" / "gtk" / "extra.py").write_text("")
    (root / "src" / "sshpilot" / "api" / "client.py").write_text("changed\n")
    scope = RepoScope(root)
    result = architecture.review_commit(scope)
    assert result["by_layer"]["frontend"]
    assert result["by_layer"]["core"]
    assert result["by_layer"]["api"]
    assert result["boundary_crossed"] is True


def test_review_commit_classifies_changes_committed_after_base(tmp_path):
    root = tmp_path / "repo"
    path = root / "src" / "sshpilot" / "mcp" / "dev" / "server.py"
    path.parent.mkdir(parents=True)
    path.write_text("before\n")
    import subprocess

    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
        ["add", "."],
        ["commit", "-q", "-m", "initial"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True)
    path.write_text("after\n")
    subprocess.run(["git", "-C", str(root), "commit", "-qam", "change"], check=True)

    result = architecture.review_commit(RepoScope(root), base="HEAD~1")

    assert result["changed_total"] == 1
    assert result["by_layer"] == {"mcp": ["src/sshpilot/mcp/dev/server.py"]}


def test_classify_path():
    assert architecture._classify_path("src/sshpilot/core/x.py") == "core"
    assert architecture._classify_path("src/sshpilot/gtk/x.py") == "frontend"
    assert architecture._classify_path("docs/api/generated/schema.json") == "generated-artifacts"
    assert architecture._classify_path("tests/x.py") == "tests"
    assert architecture._classify_path("scripts/generate_api.py") == "scripts"
    assert architecture._classify_path("src/sshpilot/mcp/dev/x.py") == "mcp"
