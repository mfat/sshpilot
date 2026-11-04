"""V2 tools against the real checkout (API/daemon subset of the index).

The index is restricted to the API, daemon and core packages plus their tests
so the cold build stays a few seconds; the domain extractors only need those.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sshpilot.mcp._scope import RepoScope
from sshpilot.mcp.dev.engine import DevIntelligence

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFIXES = ["src/sshpilot/api", "src/sshpilot/daemon", "src/sshpilot/core", "tests/api"]
HANDLER = "src/sshpilot/daemon/dispatch.py::RequestDispatcher._handle_open_session"


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    svc = DevIntelligence(
        RepoScope(REPO_ROOT),
        index_dir=tmp_path_factory.mktemp("index"),
        prefixes=PREFIXES,
    )
    yield svc
    svc.close()


def test_handler_context_follows_the_wire_protocol(service):
    result = service.get_context("_handle_open_session", budget=12000)
    assert result["status"] == "ok"
    assert result["symbol"]["id"] == HANDLER
    chain = {(edge["from"], edge["kind"], edge["to"]) for edge in result["api_chain"]}
    assert ("wire:sessions.open", "wire_to_handler", "sshpilot.daemon.dispatch.RequestDispatcher._handle_open_session") in chain
    assert ("sshpilot.api.daemon_client.DaemonClient.open_session", "api_to_wire", "wire:sessions.open") in chain
    assert all(edge["confidence"] == "verified_static" for edge in result["api_chain"])
    assert "def _handle_open_session" in result["source"]["text"]
    assert result["freshness"]["files_by_status"].get("ok", 0) > 100


def test_wire_node_context(service):
    result = service.get_context("wire:sessions.open")
    assert result["status"] == "ok"
    assert result["handler"]["id"] == HANDLER


def test_handler_impact_reaches_client_methods(service):
    result = service.inspect_impact(paths=["src/sshpilot/daemon/dispatch.py"], depth=1, budget=64000)
    summary = next(risk for risk in result["risks"] if risk["kind"] == "wire-protocol-surface")
    assert summary["count"] >= 100  # every dispatched wire method
    assert result["wire_surfaces"], "the strongest entries survive trimming"
    kinds = {risk["kind"] for risk in result["risks"]}
    assert "api-registration-source" in kinds
    assert any(d["via"]["kind"] == "wire-protocol" for d in result["dependents"])
    assert result["tests"]["broad_run_recommended"] is True
    assert "api" in result["tests"]["suites_fallback"]
    assert result["budget"]["used_bytes"] <= 64000


def test_single_handler_impact_names_its_client(service):
    graph_symbol = "src/sshpilot/daemon/dispatch.py::RequestDispatcher._handle_open_session"
    from sshpilot.mcp.dev.engine.graph import Graph

    service.index.refresh()
    graph = Graph(service.index)
    dependents, _ = graph.reverse_dependencies([graph.symbol(graph_symbol)], depth=2)
    ids = {d["id"]: d for d in dependents}
    client = ids["src/sshpilot/api/daemon_client.py::DaemonClient.open_session"]
    assert client["via"] == {"kind": "wire-protocol", "of": graph_symbol, "wire": "wire:sessions.open"}
    assert client["confidence"] == "verified_static"
    assert "src/sshpilot/api/client.py::SshPilotClient.open_session" in ids


def test_default_budget_is_compact(service):
    result = service.inspect_impact(paths=["src/sshpilot/daemon/dispatch.py"])
    encoded = json.dumps(result).encode()
    assert len(encoded) <= result["budget"]["limit_bytes"] or result["budget"]["over_budget"]
    assert result["budget"]["truncated"] is True
    assert result["budget"]["omitted"]


def _cli(args, index_dir, cwd):
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src"), "SSHPILOT_MCP_INDEX_PREFIXES": ",".join(PREFIXES)}
    env.pop("SSHPILOT_MCP_ROOT", None)
    return subprocess.run(
        [sys.executable, "-m", "sshpilot.mcp.dev.cli", "--root", str(REPO_ROOT), "--index-dir", str(index_dir), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=300,
    )


def test_cli_get_context_and_status_from_another_directory(tmp_path):
    index_dir = tmp_path / "cli-index"
    status = _cli(["--compact", "index-status"], index_dir, tmp_path)
    assert status.returncode == 0, status.stderr
    payload = json.loads(status.stdout)
    assert payload["tool"] == "index_status" and payload["refresh"]["reindexed"] > 100
    warm = json.loads(_cli(["--compact", "index-status"], index_dir, tmp_path).stdout)
    assert warm["refresh"]["reindexed"] == 0
    context = _cli(["get-context", "wire:sessions.open"], index_dir, tmp_path)
    assert context.returncode == 0, context.stderr
    assert json.loads(context.stdout)["handler"]["id"] == HANDLER


def test_cli_rejects_bad_input(tmp_path):
    result = _cli(["get-context", "../../etc/passwd:1"], tmp_path / "idx", tmp_path)
    assert result.returncode == 2
    assert "escapes" in result.stderr
