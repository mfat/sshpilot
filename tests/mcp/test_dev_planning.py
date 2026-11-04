"""Phase 4: plan_feature is grounded in indexed patterns and real paths."""

from pathlib import Path

import pytest

from sshpilot.mcp._scope import RepoScope
from sshpilot.mcp.dev.engine import DevIntelligence, EngineError

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFIXES = ["src/sshpilot/api", "src/sshpilot/daemon", "src/sshpilot/core", "tests/api"]
API_SURFACES = {
    "request/result models",
    "client contract",
    "client capability tables",
    "capability enum",
    "daemon dispatch",
    "wire codec",
    "generated artifacts",
    "user-visible text",
    "contract tests",
    "docs",
}


@pytest.fixture(scope="module")
def repo_service(tmp_path_factory):
    svc = DevIntelligence(RepoScope(REPO_ROOT), index_dir=tmp_path_factory.mktemp("index"), prefixes=PREFIXES)
    yield svc
    svc.close()


def test_api_addition_lists_every_required_surface(repo_service):
    result = repo_service.plan_feature("Add a daemon API to rename a port forward", budget=64000)
    assert result["api_involvement"] == "explicit"
    surfaces = {o["surface"] for o in result["obligations"]}
    assert API_SURFACES <= surfaces
    for obligation in result["obligations"]:
        assert "missing_paths" not in obligation, obligation
        for path in obligation.get("paths", []):
            assert (REPO_ROOT / path.rstrip("/")).exists(), path
    analogue = next(o for o in result["obligations"] if o["surface"] == "copy-the-pattern-of")["pattern"]
    assert analogue["client_method"].endswith("_forward")
    assert analogue["wire_methods"] and analogue["handlers"]
    assert all("forward" in p["matched"] or "rename" in p["matched"] or "port" in p["matched"] for p in result["api_patterns"])
    assert {"api", "architecture"} <= set(result["test_plan"]["suites"])


def test_unknown_request_is_bounded_and_uncertain(repo_service):
    result = repo_service.plan_feature("quantum flux capacitor")
    assert result["api_involvement"] == "unknown"
    assert result["patterns"] == [] and result["obligations"] == []
    assert any("no existing pattern" in u for u in result["uncertainties"])
    assert result["budget"]["used_bytes"] <= result["budget"]["limit_bytes"]


def test_anchor_symbol_carries_its_api_chain(repo_service):
    result = repo_service.plan_feature(
        "make opening a session report progress",
        symbol="src/sshpilot/daemon/dispatch.py::RequestDispatcher._handle_open_session",
        budget=20000,
    )
    assert result["anchor"]["id"].endswith("_handle_open_session")
    assert any(edge["from"] == "wire:sessions.open" for edge in result["anchor"]["api_chain"])


def test_input_validation(repo_service):
    with pytest.raises(EngineError):
        repo_service.plan_feature("")
    with pytest.raises(EngineError):
        repo_service.plan_feature("x", subsystem="kernel")
    with pytest.raises(EngineError):
        repo_service.plan_feature("x" * 5000)


def test_frontend_only_change_has_no_daemon_obligations(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    pkg = root / "src" / "sshpilot"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "sidebar_groups.py").write_text(
        "class SidebarGroupRow:\n    def collapse_group(self):\n        self.expanded = False\n"
    )
    svc = DevIntelligence(RepoScope(root), index_dir=tmp_path / "index")
    try:
        result = svc.plan_feature("Let users collapse sidebar groups and show a tooltip", subsystem="gtk", budget=20000)
    finally:
        svc.close()
    assert result["api_involvement"] == "none-detected"
    surfaces = {o["surface"] for o in result["obligations"]}
    assert "daemon dispatch" not in surfaces and "client contract" not in surfaces
    assert "user-visible text" in surfaces  # "tooltip" is UI text
    assert result["patterns"][0]["id"] == "src/sshpilot/sidebar_groups.py::SidebarGroupRow"
